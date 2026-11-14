"""Run both review variants on frozen files on the approved PC."""
import asyncio
import base64
import hashlib
import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path

from galtalka_agent.comparison_inputs import checked_relative, input_path, read_selected, read_snapshot
from galtalka_agent.client import codex_status
from galtalka_agent.config import AgentError
from galtalka_agent.failures import JobFailure

SCHEMA = {"type": "object", "additionalProperties": False,
    "properties": {"summary": {"type": "string"}, "findings": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "properties": {"path": {"type": "string"}, "line": {"type": "integer"},
            "severity": {"type": "string", "enum": ["high", "medium", "low"]},
            "issue": {"type": "string"}, "evidence": {"type": "string"},
            "suggested_fix": {"type": "string"}},
        "required": ["path", "line", "severity", "issue", "evidence", "suggested_fix"]}}},
    "required": ["summary", "findings"]}
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_FINAL_BYTES = 16 * 1024
MAX_REPORT_BYTES = 240 * 1024


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


def skill_read_confirmed(events, skill_name):
    if not skill_name:
        return False
    manifest = re.compile(re.escape(f".agents/skills/{skill_name}/SKILL.md") + r"(?:\s|$|[;&|'\"])")
    reader = re.compile(r"\b(?:cat|sed|head|tail|less|more|type|Get-Content)\b", re.I)
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") != "command_execution":
            continue
        command = item.get("command")
        if (isinstance(command, str) and manifest.search(command) and reader.search(command)
            and not re.match(r"\s*(?:echo|printf|ls|find)\b", command, re.I)):
            return True
    return False


def verify_current_input(root, manifest):
    """Refuse a frozen input whose original files changed after registration."""
    digest = hashlib.sha256()
    remaining = 64 * 1024
    try:
        for name in sorted(manifest["relative_paths"]):
            content = read_selected(root, checked_relative(name), remaining)
            remaining -= len(content)
            digest.update(name.encode() + b"\0" + content)
    except JobFailure:
        raise JobFailure("INPUT_CHANGED", "등록 이후 원본 입력 파일이 변경됐습니다.") from None
    if digest.hexdigest() != manifest["input_sha256"]:
        raise JobFailure("INPUT_CHANGED", "등록 이후 원본 입력 파일이 변경됐습니다.")


def failed_entry(input_id, code, arm):
    return {"input_id": input_id, "status": "failed", "result": None, "output_text": None,
        "seconds": None, "metrics": {}, "skill_usage_status": "not_expected" if arm == "baseline" else "unconfirmed",
        "failure_code": code}


def append_bounded(runs, arm, entry, payload):
    """Keep the entire worker report below its 256 KiB HTTP limit."""
    runs[arm]["inputs"].append(entry)
    report = {"comparison_id": payload["comparison_id"], "input_sha256": payload["input_sha256"],
        "model_id": payload["model_id"], "runner_version": payload["runner_version"], "runs": runs}
    if len(json.dumps(report, allow_nan=False).encode()) > MAX_REPORT_BYTES:
        runs[arm]["inputs"][-1] = failed_entry(entry["input_id"], "output_limit", arm)


async def stop_process_tree(proc):
    """Stop Codex and its children on cancellation, timeout, or output overflow."""
    if os.name == "nt":
        killer = await asyncio.create_subprocess_exec("taskkill", "/PID", str(proc.pid), "/T", "/F",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await killer.wait()
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    await proc.wait()


async def run_arm(command, workspace, artifact, schema_path, model_id, prompt, candidate, skill_name=None):
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
    process_options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
        else {"start_new_session": True})
    with output.open("wb") as out, stderr.open("wb") as err:
        proc = await asyncio.create_subprocess_exec(*args, cwd=workspace, stdin=asyncio.subprocess.PIPE,
            stdout=out, stderr=err, **process_options)
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
                await stop_process_tree(proc)
            await communicating
        except asyncio.CancelledError:
            await stop_process_tree(proc)
            raise
    seconds = round(time.monotonic() - started, 3)
    result = None
    if output.stat().st_size + stderr.stat().st_size > MAX_OUTPUT_BYTES:
        status = "output_limit"
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
            if skill_read_confirmed(events, skill_name):
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
    seen = set()
    skill_prefix = f".agents/skills/{payload['skill_name']}/"
    for expected in payload["inputs"]:
        if not isinstance(expected, dict) or expected.get("input_id") in seen:
            raise JobFailure("INVALID_INPUT", "비교 입력을 확인해 주세요.")
        seen.add(expected["input_id"])
        actual = read_snapshot(root, expected["input_id"])
        if actual != expected or actual.get("task_type") != "code_review":
            raise JobFailure("INPUT_CHANGED", "저장된 비교 입력이 변경됐습니다.")
        if any(name.startswith(skill_prefix) for name in actual["relative_paths"]):
            raise JobFailure("INPUT_SKILL_COLLISION", "비교 입력이 후보 스킬 경로와 겹칩니다.")
        verify_current_input(root, actual)
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
    for index, expected in enumerate(payload["inputs"]):
        input_id = expected["input_id"]
        try:
            actual = read_snapshot(root, input_id)
            if actual != expected or actual.get("task_type") != "code_review":
                raise JobFailure("INPUT_CHANGED", "저장된 비교 입력이 변경됐습니다.")
            verify_current_input(root, actual)
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
                    arm == "candidate", payload["skill_name"] if arm == "candidate" else None)
                append_bounded(runs, arm, {"input_id": input_id, **entry}, payload)
        except (AgentError, OSError) as error:
            code = error.code if isinstance(error, JobFailure) else "input_run_failed"
            for position, remaining in enumerate(payload["inputs"][index:], start=index):
                for arm in ("baseline", "candidate"):
                    if len(runs[arm]["inputs"]) <= position:
                        append_bounded(runs, arm, failed_entry(remaining["input_id"], code, arm), payload)
            break
    return {"comparison_id": payload["comparison_id"], "input_sha256": fingerprint,
        "model_id": payload["model_id"],
        "runner_version": payload["runner_version"], "runs": runs}
