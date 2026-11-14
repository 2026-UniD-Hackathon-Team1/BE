import asyncio
import base64
import hashlib
import json
import sys
from types import SimpleNamespace
from typing import get_args

import httpx
import pytest

from galtalka_agent.comparison_inputs import freeze
from galtalka_agent.comparison_run import SCHEMA, review_result, run_arm, run_comparison_job
from galtalka_agent.client import AgentClient
from galtalka_agent.config import AgentConfig, save_config
from galtalka_agent.failures import JobFailure
from backend.comparison_contract import ReviewFinding


def test_generated_review_severities_match_server_contract():
    allowed = SCHEMA["properties"]["findings"]["items"]["properties"]["severity"]["enum"]
    assert set(allowed) == set(get_args(ReviewFinding.model_fields["severity"].annotation))
    for severity in allowed:
        assert review_result({"summary": "reviewed", "findings": [{"path": "app.py", "line": 1,
            "severity": severity, "issue": "issue", "evidence": "evidence",
            "suggested_fix": "fix"}]})


def test_pc_comparison_keeps_input_files_local_and_returns_two_results(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text("print('hello')\n")
    input_id = "input_" + "a" * 32
    manifest = freeze({"schema_version": 1, "input_id": input_id,
        "task_type": "code_review", "relative_paths": ["app.py"],
        "confirmed_upload_scope": True}, project)
    bundle = {"files": {"SKILL.md": base64.b64encode(b"Review carefully.\n").decode()}}
    digest = hashlib.sha256(json.dumps(bundle, sort_keys=True, ensure_ascii=True,
        separators=(",", ":")).encode()).hexdigest()
    fake = tmp_path / "fake_codex.py"
    fake.write_text("import json,sys\n"
        "from pathlib import Path\n"
        "args=sys.argv\n"
        "if '--skip-git-repo-check' not in args: sys.exit(4)\n"
        "Path(args[args.index('-o')+1]).write_text(json.dumps({'summary': 'candidate' if (Path.cwd()/'.agents/skills/review/SKILL.md').exists() else 'baseline','findings': []}))\n"
        "print(json.dumps({'type':'item.completed','item':{'type':'command_execution','command':'cat .agents/skills/review/SKILL.md' if (Path.cwd()/'.agents/skills/review/SKILL.md').exists() else 'cat app.py'}}))\n")
    payload = {"schema_version": 1, "comparison_id": "cmp_" + "b" * 32,
        "inputs": [manifest], "model_id": "fixed-model", "runner_version": "test-runner",
        "method_prompt": "Review app.py", "candidate_bundle": bundle,
        "candidate_sha256": digest, "skill_name": "review"}
    payload["input_sha256"] = hashlib.sha256(json.dumps(
        [(manifest["input_id"], manifest["input_sha256"])], separators=(",", ":")).encode()).hexdigest()

    result = asyncio.run(run_comparison_job(payload, project,
        codex_command=[sys.executable, str(fake)]))

    assert result["comparison_id"] == payload["comparison_id"]
    assert result["runs"]["baseline"]["inputs"][0]["result"]["summary"] == "baseline"
    assert result["runs"]["candidate"]["inputs"][0]["result"]["summary"] == "candidate"
    assert result["runs"]["candidate"]["inputs"][0]["skill_usage_status"] == "confirmed"
    assert b"print('hello')" not in json.dumps(result).encode()


def test_heartbeat_reports_codex_only_after_login_check(monkeypatch):
    import galtalka_agent.client as client_module
    monkeypatch.setattr(client_module.shutil, "which", lambda name: "/bin/codex")
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="codex-cli 0.test\n" if "--version" in args else "Logged in\n")

    monkeypatch.setattr(client_module.subprocess, "run", fake_run)
    seen = []

    def respond(request):
        seen.append(json.loads(request.content))
        return httpx.Response(204)

    async def check():
        client = AgentClient("https://example.invalid", "t" * 43, transport=httpx.MockTransport(respond))
        try:
            await client.heartbeat()
        finally:
            await client.close()

    asyncio.run(check())
    assert calls == [["/bin/codex", "--version"], ["/bin/codex", "login", "status"]]
    assert seen == [{"codex_version": "codex-cli 0.test", "codex_ready": True}]


def test_cli_dispatches_comparison_job_to_pc_handler(tmp_path, monkeypatch):
    import galtalka_agent.cli as cli
    import galtalka_agent.client as client_module
    monkeypatch.setattr(client_module.shutil, "which", lambda name: None)
    config_path = tmp_path / "config.json"
    save_config(config_path, AgentConfig("https://example.invalid", tmp_path, "t" * 43))
    seen = []

    async def handler(payload, root):
        seen.append((payload, root))
        return {"comparison_id": "cmp_" + "b" * 32}

    monkeypatch.setattr(cli, "run_comparison_job", handler, raising=False)

    def respond(request):
        if request.url.path.endswith("/heartbeat") or request.url.path.endswith("/progress"):
            return httpx.Response(204)
        if request.url.path.endswith("/result"):
            seen.append(json.loads(request.content))
            return httpx.Response(204)
        return httpx.Response(200, json={"job_id": "job_" + "a" * 32,
            "kind": "run_comparison", "payload": {"schema_version": 1}, "attempt": 1,
            "claim_token": "c" * 43, "lease_expires_at": "2026-11-15T10:00:00+00:00"})

    asyncio.run(cli.run(SimpleNamespace(config=config_path, once=True),
        httpx.MockTransport(respond)))
    assert seen[0] == ({"schema_version": 1}, tmp_path)
    assert seen[1]["status"] == "done"


def test_pc_refuses_runner_version_drift_before_execution(tmp_path, monkeypatch):
    import galtalka_agent.comparison_run as comparison_run
    monkeypatch.setattr(comparison_run, "codex_status", lambda: ("changed-runner", True), raising=False)
    bundle = {"files": {"SKILL.md": base64.b64encode(b"Review carefully.\n").decode()}}
    digest = hashlib.sha256(json.dumps(bundle, sort_keys=True, ensure_ascii=True,
        separators=(",", ":")).encode()).hexdigest()
    inputs = [{"input_id": "input_" + "a" * 32, "task_type": "code_review",
        "relative_paths": ["app.py"], "input_sha256": "b" * 64}]
    fingerprint = hashlib.sha256(json.dumps([(inputs[0]["input_id"], inputs[0]["input_sha256"])],
        separators=(",", ":")).encode()).hexdigest()
    payload = {"schema_version": 1, "comparison_id": "cmp_" + "c" * 32,
        "inputs": inputs, "input_sha256": fingerprint, "model_id": "fixed-model",
        "runner_version": "old-runner", "method_prompt": "Review app.py",
        "candidate_bundle": bundle, "candidate_sha256": digest, "skill_name": "review"}
    with pytest.raises(JobFailure) as error:
        asyncio.run(run_comparison_job(payload, tmp_path))
    assert error.value.code == "RUNNER_CHANGED"


def test_oversized_final_result_is_not_reported(tmp_path):
    script = tmp_path / "large_codex.py"
    script.write_text("import json,sys\n"
        "from pathlib import Path\n"
        "Path(sys.argv[sys.argv.index('-o')+1]).write_text(json.dumps({'summary':'x'*300000,'findings':[]}))\n")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    schema = tmp_path / "schema.json"
    schema.write_text("{}")
    result = asyncio.run(run_arm([sys.executable, str(script)], workspace,
        tmp_path / "artifact", schema, "fixed-model", "Review", False))
    assert result["status"] == "output_limit"
    assert result["result"] is None


def test_fast_oversized_event_log_is_rejected(tmp_path):
    script = tmp_path / "large_log_codex.py"
    script.write_text("import json,sys\n"
        "from pathlib import Path\n"
        "Path(sys.argv[sys.argv.index('-o')+1]).write_text(json.dumps({'summary':'ok','findings':[]}))\n"
        "sys.stdout.write('x'*3000000)\n")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    schema = tmp_path / "schema.json"
    schema.write_text("{}")
    result = asyncio.run(run_arm([sys.executable, str(script)], workspace,
        tmp_path / "artifact", schema, "fixed-model", "Review", False))
    assert result["status"] == "output_limit"
    assert result["result"] is None


def test_changed_original_file_is_rejected_before_codex(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text("before\n")
    manifest = freeze({"schema_version": 1, "input_id": "input_" + "a" * 32,
        "task_type": "code_review", "relative_paths": ["app.py"],
        "confirmed_upload_scope": True}, project)
    (project / "app.py").write_text("after\n")
    bundle = {"files": {"SKILL.md": base64.b64encode(b"Review.\n").decode()}}
    digest = hashlib.sha256(json.dumps(bundle, sort_keys=True, ensure_ascii=True,
        separators=(",", ":")).encode()).hexdigest()
    fingerprint = hashlib.sha256(json.dumps([(manifest["input_id"], manifest["input_sha256"])],
        separators=(",", ":")).encode()).hexdigest()
    payload = {"schema_version": 1, "comparison_id": "cmp_" + "b" * 32,
        "inputs": [manifest], "input_sha256": fingerprint, "model_id": "fixed-model",
        "runner_version": "test", "method_prompt": "Review app.py",
        "candidate_bundle": bundle, "candidate_sha256": digest, "skill_name": "review"}
    with pytest.raises(JobFailure) as error:
        asyncio.run(run_comparison_job(payload, project, codex_command=["unused"] ))
    assert error.value.code == "INPUT_CHANGED"
    assert not (project / ".galtalka" / "comparisons" / payload["comparison_id"]).exists()


def test_input_cannot_overlap_candidate_skill_path(tmp_path):
    project = tmp_path / "project"
    skill = project / ".agents" / "skills" / "review"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("old input\n")
    manifest = freeze({"schema_version": 1, "input_id": "input_" + "a" * 32,
        "task_type": "code_review", "relative_paths": [".agents/skills/review/SKILL.md"],
        "confirmed_upload_scope": True}, project)
    bundle = {"files": {"SKILL.md": base64.b64encode(b"candidate\n").decode()}}
    digest = hashlib.sha256(json.dumps(bundle, sort_keys=True, ensure_ascii=True,
        separators=(",", ":")).encode()).hexdigest()
    fingerprint = hashlib.sha256(json.dumps([(manifest["input_id"], manifest["input_sha256"])],
        separators=(",", ":")).encode()).hexdigest()
    payload = {"schema_version": 1, "comparison_id": "cmp_" + "b" * 32,
        "inputs": [manifest], "input_sha256": fingerprint, "model_id": "fixed-model",
        "runner_version": "test", "method_prompt": "Review",
        "candidate_bundle": bundle, "candidate_sha256": digest, "skill_name": "review"}
    with pytest.raises(JobFailure) as error:
        asyncio.run(run_comparison_job(payload, project, codex_command=["unused"]))
    assert error.value.code == "INPUT_SKILL_COLLISION"


def test_later_snapshot_failure_keeps_completed_first_input(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    manifests = []
    for letter, name in (("a", "first.py"), ("b", "second.py")):
        (project / name).write_text("pass\n")
        manifests.append(freeze({"schema_version": 1, "input_id": "input_" + letter * 32,
            "task_type": "code_review", "relative_paths": [name],
            "confirmed_upload_scope": True}, project))
    second_manifest = project / ".galtalka" / "inputs" / manifests[1]["input_id"] / "manifest.json"
    fake = tmp_path / "fake_codex.py"
    fake.write_text("import json,sys\nfrom pathlib import Path\n"
        "arm='candidate' if (Path.cwd()/'.agents/skills/review/SKILL.md').exists() else 'baseline'\n"
        "Path(sys.argv[sys.argv.index('-o')+1]).write_text(json.dumps({'summary':arm,'findings':[]}))\n"
        "print(json.dumps({'type':'item.completed','item':{'command':'cat .agents/skills/review/SKILL.md' if arm=='candidate' else 'cat first.py'}}))\n"
        f"if arm=='candidate' and (Path.cwd()/'first.py').exists(): Path({str(second_manifest)!r}).unlink()\n")
    bundle = {"files": {"SKILL.md": base64.b64encode(b"Review.\n").decode()}}
    digest = hashlib.sha256(json.dumps(bundle, sort_keys=True, ensure_ascii=True,
        separators=(",", ":")).encode()).hexdigest()
    fingerprint = hashlib.sha256(json.dumps([(item["input_id"], item["input_sha256"])
        for item in manifests], separators=(",", ":")).encode()).hexdigest()
    payload = {"schema_version": 1, "comparison_id": "cmp_" + "c" * 32,
        "inputs": manifests, "input_sha256": fingerprint, "model_id": "fixed-model",
        "runner_version": "test", "method_prompt": "Review",
        "candidate_bundle": bundle, "candidate_sha256": digest, "skill_name": "review"}
    result = asyncio.run(run_comparison_job(payload, project,
        codex_command=[sys.executable, str(fake)]))
    for arm in ("baseline", "candidate"):
        assert [entry["status"] for entry in result["runs"][arm]["inputs"]] == ["completed", "failed"]


def test_three_unicode_results_stay_under_worker_report_limit(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    manifests = []
    for letter in "abc":
        name = letter + ".py"
        (project / name).write_text("pass\n")
        manifests.append(freeze({"schema_version": 1, "input_id": "input_" + letter * 32,
            "task_type": "code_review", "relative_paths": [name],
            "confirmed_upload_scope": True}, project))
    fake = tmp_path / "fake_codex.py"
    fake.write_text("import json,sys\nfrom pathlib import Path\n"
        "Path(sys.argv[sys.argv.index('-o')+1]).write_text(json.dumps({'summary':'한'*5000,'findings':[]},ensure_ascii=False))\n"
        "print(json.dumps({'type':'item.completed','item':{'command':'cat .agents/skills/review/SKILL.md'}}))\n")
    bundle = {"files": {"SKILL.md": base64.b64encode(b"Review.\n").decode()}}
    digest = hashlib.sha256(json.dumps(bundle, sort_keys=True, ensure_ascii=True,
        separators=(",", ":")).encode()).hexdigest()
    fingerprint = hashlib.sha256(json.dumps([(item["input_id"], item["input_sha256"])
        for item in manifests], separators=(",", ":")).encode()).hexdigest()
    payload = {"schema_version": 1, "comparison_id": "cmp_" + "c" * 32,
        "inputs": manifests, "input_sha256": fingerprint, "model_id": "fixed-model",
        "runner_version": "test", "method_prompt": "Review",
        "candidate_bundle": bundle, "candidate_sha256": digest, "skill_name": "review"}
    result = asyncio.run(run_comparison_job(payload, project,
        codex_command=[sys.executable, str(fake)]))
    assert len(json.dumps(result, allow_nan=False).encode()) <= 256 * 1024
    assert result["runs"]["baseline"]["inputs"][0]["status"] == "completed"
    assert any(entry["status"] == "failed" for arm in result["runs"].values() for entry in arm["inputs"])


def test_listing_skill_path_does_not_confirm_skill_use(tmp_path):
    script = tmp_path / "fake_codex.py"
    script.write_text("import json,sys\nfrom pathlib import Path\n"
        "Path(sys.argv[sys.argv.index('-o')+1]).write_text(json.dumps({'summary':'ok','findings':[]}))\n"
        "print(json.dumps({'type':'item.completed','item':{'type':'command_execution','command':'echo .agents/skills/review/SKILL.md'}}))\n")
    workspace = tmp_path / "workspace"
    (workspace / ".agents" / "skills" / "review").mkdir(parents=True)
    (workspace / ".agents" / "skills" / "review" / "SKILL.md").write_text("review")
    schema = tmp_path / "schema.json"
    schema.write_text("{}")
    result = asyncio.run(run_arm([sys.executable, str(script)], workspace,
        tmp_path / "artifact", schema, "fixed-model", "Review", True))
    assert result["status"] == "completed"
    assert result["skill_usage_status"] == "unconfirmed"


def test_cancellation_stops_codex_child_process(tmp_path):
    marker = tmp_path / "child-survived"
    script = tmp_path / "parent_codex.py"
    script.write_text("import subprocess,sys,time\n"
        f"subprocess.Popen([sys.executable,'-c',\"import time;from pathlib import Path;time.sleep(0.8);Path({str(marker)!r}).write_text('alive')\"])\n"
        "time.sleep(10)\n")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    schema = tmp_path / "schema.json"
    schema.write_text("{}")

    async def cancel():
        task = asyncio.create_task(run_arm([sys.executable, str(script)], workspace,
            tmp_path / "artifact", schema, "fixed-model", "Review", False))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(1)

    asyncio.run(cancel())
    assert not marker.exists()
