"""Exercise the PC connection program through one complete B-owned user flow."""
import base64
import functools
import hashlib
import json
import sys

import httpx
from fastapi.testclient import TestClient

from backend.main import create_app
from backend.models import AssetVersion, CandidateAdapter
from backend.seed import seed
from galtalka_agent import cli
from galtalka_agent.comparison_run import run_comparison_job


def test_connect_scan_compare_apply_rollback(database, tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / "app.py").write_text("print('review me')\n", encoding="utf-8")
    (root / ".codex").mkdir()
    (root / ".codex" / "config.toml").write_text('model="fixed-model"\n', encoding="utf-8")
    target = root / ".agents" / "skills" / "review-skill" / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old skill\n")
    config_path = tmp_path / "agent-config.json"
    fake_codex = tmp_path / "fake_codex.py"
    fake_codex.write_text(
        "import json,sys\n"
        "from pathlib import Path\n"
        "skill=Path('.agents/skills/review-skill/SKILL.md')\n"
        "arm='candidate' if skill.exists() and skill.read_bytes()==b'new skill\\n' else 'baseline'\n"
        "Path(sys.argv[sys.argv.index('-o')+1]).write_text(json.dumps({'summary':arm,'findings':[]}))\n"
        "command='cat .agents/skills/review-skill/SKILL.md' if arm=='candidate' else 'cat app.py'\n"
        "print(json.dumps({'type':'item.completed','item':{'type':'command_execution','command':command}}))\n",
        encoding="utf-8")
    monkeypatch.setattr(cli, "run_comparison_job", functools.partial(
        run_comparison_job, codex_command=[sys.executable, str(fake_codex)]))
    monkeypatch.setattr("galtalka_agent.client.codex_status", lambda: ("codex-cli 0.sim", True))

    _, sessions, config = database
    bundle = {"files": {"SKILL.md": base64.b64encode(b"new skill\n").decode()}}
    digest = hashlib.sha256(json.dumps(bundle, sort_keys=True, ensure_ascii=True,
        separators=(",", ":")).encode()).hexdigest()
    with sessions.begin() as session:
        seed(session)
        version = session.get(AssetVersion, "demo_ver_01")
        version.bundle_sha256 = digest
        version.metadata_json = {**version.metadata_json, "bundle": bundle}
        session.add(CandidateAdapter(id="adapter_user_flow", asset_version_id=version.id,
            runner="codex_cli", supported_tasks=["code_review"],
            approved_bundle_sha256=digest, support_level="applicable", reviewed=True,
            runtime={"skill_name": "review-skill"}))

    with TestClient(create_app(database_url=config.attributes["database_url"],
        local_token="test-token", agent_wait_timeout=0, input_wait_timeout=0)) as client:
        web = {"X-Local-Token": "test-token"}

        def dispatch(request):
            response = client.request(request.method, request.url.raw_path.decode(),
                content=request.content, headers=dict(request.headers))
            return httpx.Response(response.status_code, content=response.content,
                headers=response.headers)

        transport = httpx.MockTransport(dispatch)

        def pc_run():
            assert cli.main(["run", "--config", str(config_path), "--once"], transport=transport) == 0

        profile = client.put("/api/profile", headers=web, json={
            "role": "developer", "primary_language": "python", "task_types": ["code_review"]})
        assert profile.status_code == 200, profile.text
        code = client.post("/api/agent/pairing-code", headers=web).json()["code"]
        assert cli.main(["connect", code, "--server", "https://testserver", "--root",
            str(root), "--config", str(config_path)], transport=transport) == 0
        pc_run()
        assert client.get("/api/agent/devices", headers=web).status_code == 200

        scan = client.post("/api/environment/scan", headers=web,
            json={"root": ".", "scopes": ["config", "skills"]})
        assert scan.status_code == 504, scan.text
        pc_run()
        environment = client.get("/api/environment", headers=web,
            params={"job_id": scan.json()["error"]["job_id"]})
        assert environment.status_code == 200, environment.text
        assert environment.json()["model_id"] == "fixed-model"
        assert environment.json()["runner_version"] == "codex-cli 0.sim"

        method = client.post("/api/work-methods", headers=web, json={
            "task_type": "code_review", "prompt_text": "Review app.py"})
        assert method.status_code == 201, method.text
        input_response = client.post("/api/inputs", headers=web,
            json={"relative_paths": ["app.py"], "task_type": "code_review"})
        assert input_response.status_code == 202, input_response.text
        input_id = input_response.json()["input_id"]
        pc_run()
        assert client.get(f"/api/inputs/{input_id}", headers=web).status_code == 200

        comparison = client.post("/api/comparisons", headers=web, json={
            "method_id": method.json()["method_id"], "method_version": method.json()["version"],
            "candidate_version_id": "demo_ver_01", "task_type": "code_review", "input_ids": [input_id],
            "confirmed_upload_scope": True, "idempotency_key": "user-flow"})
        assert comparison.status_code == 202, comparison.text
        comparison_id = comparison.json()["comparison_id"]
        assert client.patch(f"/api/comparisons/{comparison_id}/decision", headers=web,
            json={"choice": "candidate", "reason": "선택"}).status_code == 409
        pc_run()
        detail = client.get(f"/api/comparisons/{comparison_id}", headers=web)
        assert detail.status_code == 200, detail.text
        assert detail.json()["status"] == "done"
        assert {run["arm"]: run["inputs"][0]["result"]["summary"] for run in detail.json()["runs"]} == {
            "baseline": "baseline", "candidate": "candidate"}
        assert next(run for run in detail.json()["runs"] if run["arm"] == "candidate")[
            "skill_usage_status"] == "confirmed"
        choice = client.patch(f"/api/comparisons/{comparison_id}/decision", headers=web,
            json={"choice": "candidate", "reason": "후보 결과 선택"})
        assert choice.status_code == 200, choice.text

        preview = client.post("/api/apply/preview", headers=web,
            json={"comparison_id": comparison_id})
        assert preview.status_code == 202, preview.text
        pc_run()
        preview_detail = client.get(f"/api/apply/preview/{preview.json()['preview_id']}", headers=web)
        assert preview_detail.json()["status"] == "done", preview_detail.text
        applied = client.post("/api/apply", headers=web, json={
            "preview_id": preview.json()["preview_id"],
            "change_set_sha256": preview_detail.json()["change_set_sha256"]})
        assert applied.status_code == 202, applied.text
        pc_run()
        apply_id = applied.json()["apply_id"]
        assert client.get(f"/api/apply/{apply_id}", headers=web).json()["status"] == "applied"
        assert target.read_bytes() == b"new skill\n"
        rollback = client.post(f"/api/apply/{apply_id}/rollback", headers=web,
            json={"confirmed": True})
        assert rollback.status_code == 202, rollback.text
        pc_run()
        assert client.get(f"/api/apply/{apply_id}", headers=web).json()["status"] == "rolled_back"
        assert target.read_bytes() == b"old skill\n"
        events = client.get("/api/history", headers=web).json()
        assert {event["event_type"] for event in events} >= {
            "comparison_decision_recorded", "skill_applied", "skill_rolled_back"}
