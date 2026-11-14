import asyncio
import base64
import hashlib
import json
import sys
from types import SimpleNamespace

import httpx
import pytest

from galtalka_agent.comparison_inputs import freeze
from galtalka_agent.comparison_run import run_arm, run_comparison_job
from galtalka_agent.client import AgentClient
from galtalka_agent.config import AgentConfig, save_config
from galtalka_agent.failures import JobFailure


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
        "print(json.dumps({'type':'item.completed','item':{'type':'command_execution','command':'read .agents/skills/review/SKILL.md' if (Path.cwd()/'.agents/skills/review/SKILL.md').exists() else 'read app.py'}}))\n")
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
