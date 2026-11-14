import asyncio
import base64
import hashlib
import json
import sys

from fastapi.testclient import TestClient

from backend.agent import secret_hash
from backend.main import create_app
from backend.models import AgentDevice, AgentJob, AssetVersion, CandidateAdapter, Environment, WorkMethod, utc_now
from backend.seed import seed
from galtalka_agent.comparison_inputs import snapshot_inputs_job
from galtalka_agent.comparison_run import run_comparison_job

WEB = {"X-Local-Token": "test-token"}
DEVICE = {"Authorization": "Bearer " + "t" * 43}


def test_agent_snapshot_runs_comparison_and_user_can_choose(database, tmp_path):
    engine, sessions, _ = database
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text("print('hello')\n")
    bundle = {"files": {"SKILL.md": base64.b64encode(b"Review carefully.\n").decode()}}
    digest = hashlib.sha256(json.dumps(bundle, sort_keys=True, ensure_ascii=True,
        separators=(",", ":")).encode()).hexdigest()
    with sessions.begin() as session:
        seed(session)
        session.add(AgentDevice(id="device_compare", profile_id="local", token_hash=secret_hash("t" * 43),
            device_name="test", agent_version="test", is_primary=True, codex_ready=True,
            codex_version="test", last_seen_at=utc_now()))
        session.add(Environment(id="env_compare", profile_id="local", device_id="device_compare",
            runner="codex_cli", runner_version="test", model_id="fixed-model",
            installed_skills=[], settings_hash="x", scan_root="agent://device_compare/scan"))
        session.add(WorkMethod(id="wm_compare", version=1, profile_id="local", task_type="code_review",
            prompt_text="Review app.py", is_active=True))
        version = session.get(AssetVersion, "demo_ver_01")
        version.metadata_json = {"bundle": bundle}
        version.bundle_sha256 = digest
        session.add(CandidateAdapter(id="adapter_compare", asset_version_id=version.id,
            runner="codex_cli", supported_tasks=["code_review"],
            approved_bundle_sha256=digest, support_level="comparable", reviewed=True))
    with TestClient(create_app(database_url=engine.url.render_as_string(hide_password=False),
                               local_token="test-token", input_wait_timeout=0)) as client:
        registered = client.post("/api/inputs", headers=WEB,
            json={"relative_paths": ["app.py"], "task_type": "code_review"}).json()
        snapshot_job = client.get("/api/agent/jobs", headers=DEVICE).json()
        manifest = asyncio.run(snapshot_inputs_job(snapshot_job["payload"], project))
        assert client.post(f"/api/agent/jobs/{snapshot_job['job_id']}/result", headers=DEVICE,
            json={"claim_token": snapshot_job["claim_token"], "status": "done", "result": manifest}).status_code == 204
        created = client.post("/api/comparisons", headers=WEB, json={
            "method_id": "wm_compare", "method_version": 1, "candidate_version_id": "demo_ver_01",
            "task_type": "code_review", "input_ids": [registered["input_id"]],
            "confirmed_upload_scope": True, "idempotency_key": "agent-comparison"})
        assert created.status_code == 202, created.text
        comparison_id = created.json()["comparison_id"]
        claim = client.get("/api/agent/jobs", headers=DEVICE).json()
        assert claim["kind"] == "run_comparison"
        assert claim["payload"]["inputs"] == [manifest]
        assert claim["payload"]["input_sha256"] == hashlib.sha256(json.dumps(
            [(manifest["input_id"], manifest["input_sha256"])], separators=(",", ":")).encode()).hexdigest()
        assert "print('hello')" not in json.dumps(claim["payload"])
        fake = tmp_path / "fake_codex.py"
        fake.write_text("import json,sys\n"
            "from pathlib import Path\n"
            "arm='candidate' if (Path.cwd()/'.agents/skills/skill-1/SKILL.md').exists() else 'baseline'\n"
            "Path(sys.argv[sys.argv.index('-o')+1]).write_text(json.dumps({'summary':arm,'findings':[]}))\n"
            "print(json.dumps({'type':'item.completed','item':{'type':'command_execution','command':"
            "'cat .agents/skills/skill-1/SKILL.md' if arm=='candidate' else 'cat app.py'}}))\n")
        result = asyncio.run(run_comparison_job(claim["payload"], project,
            codex_command=[sys.executable, str(fake)]))
        assert result["input_sha256"] == claim["payload"]["input_sha256"]
        assert client.post(f"/api/agent/jobs/{claim['job_id']}/result", headers=DEVICE,
            json={"claim_token": claim["claim_token"], "status": "done", "result": result}).status_code == 204
        detail = client.get(f"/api/comparisons/{comparison_id}", headers=WEB)
        assert detail.status_code == 200, detail.text
        assert detail.json()["status"] == "done"
        assert {run["arm"]: run["inputs"][0]["result"]["summary"] for run in detail.json()["runs"]} == {
            "baseline": "baseline", "candidate": "candidate"}
        assert client.patch(f"/api/comparisons/{comparison_id}/decision", headers=WEB,
            json={"choice": "candidate", "reason": "후보 결과를 선택함"}).status_code == 200
        with sessions.begin() as session:
            session.get(AgentDevice, "device_compare").codex_version = "changed-runner"
        changed = client.post("/api/comparisons", headers=WEB, json={
            "method_id": "wm_compare", "method_version": 1, "candidate_version_id": "demo_ver_01",
            "task_type": "code_review", "input_ids": [registered["input_id"]],
            "confirmed_upload_scope": True, "idempotency_key": "runner-drift"})
        assert changed.status_code == 409
        with sessions.begin() as session:
            session.get(AgentDevice, "device_compare").codex_version = "test"
        second = client.post("/api/comparisons", headers=WEB, json={
            "method_id": "wm_compare", "method_version": 1, "candidate_version_id": "demo_ver_01",
            "task_type": "code_review", "input_ids": [registered["input_id"]],
            "confirmed_upload_scope": True, "idempotency_key": "cancel-before-claim"})
        assert second.status_code == 202
        second_id = second.json()["comparison_id"]
        assert client.post(f"/api/comparisons/{second_id}/cancel", headers=WEB).status_code == 200
        assert client.get("/api/agent/jobs", headers=DEVICE).json() is None
        third = client.post("/api/comparisons", headers=WEB, json={
            "method_id": "wm_compare", "method_version": 1, "candidate_version_id": "demo_ver_01",
            "task_type": "code_review", "input_ids": [registered["input_id"]],
            "confirmed_upload_scope": True, "idempotency_key": "expired-claim"})
        assert third.status_code == 202
        third_id = third.json()["comparison_id"]
        expired = client.get("/api/agent/jobs", headers=DEVICE).json()
        with sessions.begin() as session:
            session.get(AgentJob, expired["job_id"]).lease_expires_at = "2000-01-01T00:00:00.000000Z"
        assert client.get(f"/api/comparisons/{third_id}", headers=WEB).json()["status"] == "failed"
