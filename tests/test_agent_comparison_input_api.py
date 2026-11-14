import asyncio

from fastapi.testclient import TestClient

from backend.agent import secret_hash
from backend.main import create_app
from backend.models import AgentDevice, Environment, utc_now
from backend.seed import seed
from galtalka_agent.comparison_inputs import snapshot_inputs_job

WEB = {"X-Local-Token": "test-token"}
DEVICE = {"Authorization": "Bearer " + "t" * 43}


def test_connected_pc_registers_input_without_server_file_access(database, tmp_path):
    engine, sessions, _ = database
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_bytes(b"print('hello')\n")
    with sessions.begin() as session:
        seed(session)
        session.add(AgentDevice(id="device_inputs", profile_id="local", token_hash=secret_hash("t" * 43),
            device_name="test", agent_version="test", last_seen_at=utc_now()))
        session.add(Environment(id="env_inputs", profile_id="local", device_id="device_inputs",
            runner="codex_cli", runner_version="test", model_id="fixed",
            installed_skills=[], settings_hash="x", scan_root="agent://device_inputs/scan"))
    with TestClient(create_app(database_url=engine.url.render_as_string(hide_password=False),
                               local_token="test-token", input_wait_timeout=0)) as client:
        queued = client.post("/api/inputs", headers=WEB,
            json={"relative_paths": ["app.py"], "task_type": "code_review"})
        assert queued.status_code == 202, queued.text
        input_id, job_id = queued.json()["input_id"], queued.json()["job_id"]
        claim = client.get("/api/agent/jobs", headers=DEVICE).json()
        assert claim["job_id"] == job_id and claim["kind"] == "snapshot_inputs"
        assert claim["payload"]["input_id"] == input_id
        result = asyncio.run(snapshot_inputs_job(claim["payload"], project))
        assert client.post(f"/api/agent/jobs/{job_id}/result", headers=DEVICE,
            json={"claim_token": claim["claim_token"], "status": "done", "result": result}).status_code == 204
        registered = client.get(f"/api/inputs/{input_id}", headers=WEB)
        assert registered.status_code == 200, registered.text
        assert registered.json() == result
        assert "agent://" not in str(registered.json())
