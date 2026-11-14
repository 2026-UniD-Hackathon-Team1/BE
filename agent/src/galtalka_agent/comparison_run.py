"""Run both review variants on frozen files on the approved PC."""
import asyncio
import base64
import hashlib
import json
import re
import time
from pathlib import Path

from galtalka_agent.comparison_inputs import checked_relative, input_path, read_selected, read_snapshot
from galtalka_agent.client import codex_status
from galtalka_agent.failures import JobFailure

SCHEMA = {"type": "object", "additionalProperties": False,
    "properties": {"summary": {"type": "string"}, "findings": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "properties": {key: {"type": "integer" if key == "line" else "string"}
            for key in ("path", "line", "severity", "issue", "evidence", "suggested_fix")},
        "required": ["path", "line", "severity", "issue", "evidence", "suggested_fix"]}}},
    "required": ["summary", "findings"]}
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_FINAL_BYTES = 16 * 1024


def bundle_files(bundle, expected_hash):
    """Verify the exact reviewed bytes before creating a candidate workspace."""
    if not isinstance(bundle, dict) or not isinstance(bundle.get("files"), dict):
        raise JobFailure("INVALID_CANDIDATE", "후보 스킬 파일을 확인해 주세요.")
    digest = hashlib.sha256(json.dumps(bundle, sort_keys=True, ensure_ascii=True,
        separators=(",", ":")).encode()).hexdigest()
    if digest != expected_hash or "SKILL.md" not in bundle["files"]:
        raise JobFailure("CANDIDATE_CHANGED", "승인된 후보 스킬 버전이 다릅니다.")
    files = {}
    for name, encoded in bundle["files"].items():
        relative = checked_relative(name)
        if not isinstance(encoded, str):
            raise JobFailure("INVALID_CANDIDATE", "후보 스킬 파일을 확인해 주세요.")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, base64.binascii.Error):
            raise JobFailure("INVALID_CANDIDATE", "후보 스킬 파일을 확인해 주세요.") from None
        files[relative] = content
    return files


def review_result(value):
    if not isinstance(value, dict) or set(value) != {"summary", "findings"}:
        return False
    if not isinstance(value["summary"], str) or not isinstance(value["findings"], list):
        return False
    keys = {"path", "line", "severity", "issue", "evidence", "suggested_fix"}
    return all(isinstance(item, dict) and set(item) == keys
        and isinstance(item["line"], int) and item["line"] >= 1
        and item["severity"] in {"high", "medium", "low"}
        and all(isinstance(item[key], str) for key in keys - {"line"})
        for item in value["findings"])


async def run_arm(command, workspace, artifact, schema_path, model_id, prompt, candidate):
    artifact.mkdir(parents=True, exist_ok=False)
    output = artifact / "events.jsonl"
    stderr = artifact / "stderr.log"
    final = artifact / "final.json"
    started = time.monotonic()
    args = [*command, "exec", "--json", "--ephemeral", "--ignore-user-config",
        "--skip-git-repo-check",
        "--sandbox", "read-only", "--model", model_id, "--output-schema", str(schema_path),
        "-o", str(final), "-C", str(workspace), "-"]
    status = "failed"
    with output.open("wb") as out, stderr.open("wb") as err:
        proc = await asyncio.create_subprocess_exec(*args, cwd=workspace, stdin=asyncio.subprocess.PIPE,
            stdout=out, stderr=err)
        communicating = asyncio.create_task(proc.communicate(prompt.encode("utf-8")))
        try:
            while not communicating.done():
                await asyncio.wait({communicating}, timeout=0.2)
                if output.stat().st_size + stderr.stat().st_size > MAX_OUTPUT_BYTES:
                    status = "output_limit"
                    break
                if time.monotonic() - started > 180:
                    status = "timeout"
                    break
            if status in {"timeout", "output_limit"}:
                proc.kill()
            await communicating
        except asyncio.CancelledError:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
            raise
    seconds = round(time.monotonic() - started, 3)
    result = None
    if final.is_file() and final.stat().st_size > MAX_FINAL_BYTES:
        status = "output_limit"
    if status not in {"timeout", "output_limit"} and proc.returncode == 0 and final.is_file():
        try:
            result = json.loads(final.read_text(encoding="utf-8"))
            if not review_result(result):
                result = None
        except (OSError, ValueError):
            pass
        if result is not None:
            status = "completed"
    usage = "not_expected" if not candidate else "unconfirmed"
    if candidate and status == "completed":
        try:
            events = (json.loads(line) for line in output.read_text(encoding="utf-8").splitlines() if line)
            if any(".agents/skills/" in str(event.get("item", {}).get("command", ""))
                for event in events if isinstance(event, dict) and event.get("type") == "item.completed"):
                usage = "confirmed"
        except (OSError, ValueError):
            pass
    return {"status": status, "result": result, "output_text": result["summary"] if result else None,
        "seconds": seconds, "metrics": {}, "skill_usage_status": usage,
        "failure_code": None if status == "completed" else status}


async def run_comparison_job(payload, project_root, *, codex_command=None):
    """Return bounded UI data while keeping selected file bytes and JSONL on the PC."""
    if (not isinstance(payload, dict) or payload.get("schema_version") != 1
        or not re.fullmatch(r"cmp_[a-f0-9]{32}", str(payload.get("comparison_id")))
        or not isinstance(payload.get("inputs"), list) or not 1 <= len(payload["inputs"]) <= 3
        or not isinstance(payload.get("method_prompt"), str) or not payload["method_prompt"].strip()
        or not isinstance(payload.get("model_id"), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,79}", payload["model_id"])
        or not isinstance(payload.get("runner_version"), str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", str(payload.get("skill_name")))):
        raise JobFailure("INVALID_COMPARISON", "비교 작업 내용을 확인해 주세요.")
    root = Path(project_root).resolve(strict=True)
    files = bundle_files(payload.get("candidate_bundle"), payload.get("candidate_sha256"))
    fingerprint = hashlib.sha256(json.dumps([(item.get("input_id"), item.get("input_sha256"))
        for item in payload["inputs"]], separators=(",", ":")).encode()).hexdigest()
    if payload.get("input_sha256") != fingerprint:
        raise JobFailure("INPUT_CHANGED", "비교 입력 묶음이 변경됐습니다.")
    if codex_command is None:
        version, ready = await asyncio.to_thread(codex_status)
        if not ready or version != payload["runner_version"]:
            raise JobFailure("RUNNER_CHANGED", "Codex 설치 또는 로그인 상태가 변경됐습니다. PC 환경을 다시 확인해 주세요.")
    base = root / ".galtalka" / "comparisons"
    for part in (root / ".galtalka", base):
        if part.is_symlink() or part.exists() and not part.is_dir():
            raise JobFailure("INVALID_COMPARISON_ROOT", "비교 결과 보관 경로를 확인해 주세요.")
    base.mkdir(parents=True, exist_ok=True)
    target = base / payload["comparison_id"]
    if target.exists() or target.is_symlink():
        raise JobFailure("COMPARISON_ALREADY_RUN", "이 비교는 PC에서 이미 실행됐습니다.")
    target.mkdir()
    schema_path = target / "review.schema.json"
    schema_path.write_text(json.dumps(SCHEMA), encoding="utf-8")
    runs = {"baseline": {"inputs": []}, "candidate": {"inputs": []}}
    seen = set()
    for expected in payload["inputs"]:
        if not isinstance(expected, dict) or expected.get("input_id") in seen:
            raise JobFailure("INVALID_INPUT", "비교 입력을 확인해 주세요.")
        input_id = expected.get("input_id")
        seen.add(input_id)
        actual = read_snapshot(root, input_id)
        if actual != expected or actual.get("task_type") != "code_review":
            raise JobFailure("INPUT_CHANGED", "저장된 비교 입력이 변경됐습니다.")
        snapshot = input_path(root, input_id) / "files"
        for arm in ("baseline", "candidate"):
            workspace = target / input_id / arm / "workspace"
            workspace.mkdir(parents=True)
            for name in actual["relative_paths"]:
                relative = checked_relative(name)
                content = read_selected(snapshot, relative, 64 * 1024)
                destination = workspace / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
            prompt = payload["method_prompt"]
            if arm == "candidate":
                skill_root = workspace / ".agents" / "skills" / payload["skill_name"]
                for name, content in files.items():
                    destination = skill_root / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(content)
                prompt += f"\nUse the instructions in .agents/skills/{payload['skill_name']}/SKILL.md for this review."
            entry = await run_arm(codex_command or ["codex"], workspace,
                target / input_id / arm / "artifacts", schema_path, payload["model_id"], prompt,
                arm == "candidate")
            runs[arm]["inputs"].append({"input_id": input_id, **entry})
    return {"comparison_id": payload["comparison_id"], "input_sha256": fingerprint,
        "model_id": payload["model_id"],
        "runner_version": payload["runner_version"], "runs": runs}
