from __future__ import annotations

import hashlib
import base64
import json
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from backend.runtime.codex_runner import RunResult, run_codex_once


@dataclass
class InputSnapshot:
    directory: Path
    sha256: str


@dataclass
class ArmResult:
    status: str
    exit_code: int | None
    seconds: float
    stdout: str
    stderr: str
    output_path: Path
    input_sha256: str
    final_result: dict | None
    skill_usage_status: str


@dataclass
class ComparisonResult:
    baseline: ArmResult
    candidate: ArmResult
    input_sha256: str
    model_id: str
    runner_version: str


def create_input_snapshot(root: Path, selected_paths: list[Path]) -> InputSnapshot:
    root = root.resolve(strict=True)
    snapshot = root.parent / f".galatalkka-input-{uuid.uuid4().hex}"
    snapshot.mkdir(mode=0o700)
    digest = hashlib.sha256()
    try:
        for selected in sorted((Path(path) for path in selected_paths), key=lambda p: p.as_posix()):
            if selected.is_absolute() or ".." in selected.parts:
                raise ValueError(f"input path is outside approved root: {selected}")
            source = root / selected
            if source.is_symlink():
                raise ValueError(f"symlink inputs are not allowed: {selected}")
            resolved = source.resolve(strict=True)
            if not resolved.is_relative_to(root) or not resolved.is_file():
                raise ValueError(f"input path is outside approved root: {selected}")
            content = resolved.read_bytes()
            relative = selected.as_posix()
            digest.update(relative.encode() + b"\0" + content)
            destination = snapshot / selected
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        return InputSnapshot(snapshot, digest.hexdigest())
    except Exception:
        shutil.rmtree(snapshot, ignore_errors=True)
        raise


def _run_arm(
    name: str,
    snapshot: InputSnapshot,
    bundle: Path | None,
    output_root: Path,
    command: list[str],
    timeout: int,
    max_output_bytes: int,
    model_id: str,
    method_prompt: str,
) -> ArmResult:
    workspace = output_root / "workspaces" / name
    shutil.copytree(snapshot.directory, workspace)
    if bundle:
        shutil.copytree(bundle, workspace / ".agents" / "skills" / bundle.name)
    output_path = output_root / "artifacts" / name / "output.jsonl"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    final_path = output_path.parent / "final.json"
    started = time.monotonic()
    args = [*command, "exec", "--json", "--ephemeral", "--ignore-user-config", "--sandbox", "read-only", "--model", model_id, "--output-schema", str(Path(__file__).resolve().parents[2] / "experiments/skill_eval/review.schema.json"), "-o", str(final_path), "-C", str(workspace), "-"]
    result: RunResult = run_codex_once(
        args,
        cwd=workspace,
        prompt=method_prompt,
        stdout_path=output_path,
        timeout=timeout,
        max_output_bytes=max_output_bytes,
    )
    final_result = None
    if result.status == "completed" and final_path.exists():
        try:
            final_result = json.loads(final_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            result.status = "failed"
    else:
        result.status = "failed" if result.status == "completed" else result.status
    skill_status = "not_expected" if name == "baseline" else "unconfirmed"
    if name == "candidate" and result.status == "completed":
        try:
            events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
            if any(event.get("type") == "item.completed" and
                event.get("item", {}).get("type", "command_execution") == "command_execution" and
                ".agents/skills/" in event.get("item", {}).get("command", "")
                for event in events):
                skill_status = "confirmed"
        except ValueError:
            result.status = "failed"
    return ArmResult(
        result.status,
        result.exit_code,
        round(time.monotonic() - started, 3),
        result.stdout,
        result.stderr,
        output_path,
        snapshot.sha256,
        final_result,
        skill_status,
    )


def execute_comparison(
    *,
    root: Path,
    selected_paths: list[Path],
    candidate_bundle: Path,
    approved_bundle_sha256: str,
    model_id: str,
    runner_version: str,
    output_root: Path,
    codex_command: list[str],
    method_prompt: str,
    timeout: int = 180,
    max_output_bytes: int = 2 * 1024 * 1024,
    expected_input_sha256: str | None = None,
) -> ComparisonResult:
    candidate_bundle = candidate_bundle.resolve(strict=True)
    if not method_prompt.strip():
        raise ValueError("method prompt is required")
    if any(item.is_symlink() for item in candidate_bundle.rglob("*")):
        raise ValueError("candidate bundle contains symlink")
    files = {
        item.relative_to(candidate_bundle).as_posix(): base64.b64encode(item.read_bytes()).decode("ascii")
        for item in sorted(candidate_bundle.rglob("*")) if item.is_file()
    }
    encoded = json.dumps({"files": files}, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
    if hashlib.sha256(encoded).hexdigest() != approved_bundle_sha256:
        raise ValueError("approved candidate hash does not match bundle")

    output_root.mkdir(parents=True, exist_ok=False)
    snapshot = create_input_snapshot(root, selected_paths)
    try:
        if expected_input_sha256 is not None and snapshot.sha256 != expected_input_sha256:
            raise ValueError("comparison input changed")
        baseline = _run_arm(
            "baseline", snapshot, None, output_root, codex_command, timeout, max_output_bytes, model_id, method_prompt
        )
        candidate = _run_arm(
            "candidate", snapshot, candidate_bundle, output_root, codex_command, timeout, max_output_bytes, model_id, method_prompt
        )
        return ComparisonResult(baseline, candidate, snapshot.sha256, model_id, runner_version)
    finally:
        shutil.rmtree(snapshot.directory, ignore_errors=True)
