"""The Railway API queues PC-side skill changes and records confirmed results."""
import asyncio
import base64
import hashlib
import json

from fastapi.testclient import TestClient
from sqlalchemy import select

from agent.src.galtalka_agent.skill_switch import apply_job, rollback_job
from galtalka_agent.config import AgentError
from backend.agent import secret_hash
from backend.main import create_app
from backend.models import (AgentDevice, ApplyRecord, AssetVersion, CandidateAdapter,
                            Comparison, Run, Snapshot, WorkMethod, utc_now)
from backend.seed import seed

WEB = {"X-Local-Token": "test-token"}
BEARER = {"Authorization": "Bearer " + "t" * 43}


def prepared(database):
    engine, sessions, _ = database
    files = {"SKILL.md": base64.b64encode(b"new skill\n").decode()}
    bundle = {"files": files}
    digest = hashlib.sha256(json.dumps(bundle, sort_keys=True,
        ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
    with sessions.begin() as session:
        seed(session)
        session.add(AgentDevice(id="device_test", profile_id="local", token_hash=secret_hash("t" * 43),
            device_name="test", agent_version="test", last_seen_at=utc_now()))
        session.add(WorkMethod(id="wm_apply", version=1, profile_id="local", task_type="code_review",
            prompt_text="review", is_active=True))
        version = session.get(AssetVersion, "demo_ver_01")
        version.bundle_sha256 = digest
        version.metadata_json = {**version.metadata_json, "bundle": bundle}
        session.add(CandidateAdapter(id="adapter_apply", asset_version_id=version.id,
            runner="codex_cli", supported_tasks=["code_review"], approved_bundle_sha256=digest,
            support_level="applicable", reviewed=True, runtime={"skill_name": "review-skill"}))
        session.add(Comparison(id="cmp_apply", profile_id="local", method_id="wm_apply",
            method_version=1, candidate_version_id=version.id, input_manifest=[],
            input_sha256="a" * 64, plan={}, status="done", user_decision="candidate"))
        session.add(Run(id="run_apply", comparison_id="cmp_apply", arm="candidate", status="completed",
            skill_usage_status="confirmed", result_json={"inputs": []}, metrics_json={"inputs": []}))
    return engine, sessions, digest


def complete_job(client, root, handler):
    claim = client.get("/api/agent/jobs", headers=BEARER).json()
    assert claim is not None
    result = asyncio.run(handler(claim["payload"], root))
    assert client.post(f"/api/agent/jobs/{claim['job_id']}/result", headers=BEARER,
        json={"claim_token": claim["claim_token"], "status": "done", "result": result}).status_code == 204
    return claim


def test_apply_preview_install_and_rollback_roundtrip(database, tmp_path):
    engine, sessions, digest = prepared(database)
    root = tmp_path / "project"
    target = root / ".agents" / "skills" / "review-skill"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_bytes(b"old skill\n")
    with TestClient(create_app(database_url=engine.url.render_as_string(hide_password=False),
                               local_token="test-token")) as client:
        preview = client.post("/api/apply/preview", headers=WEB,
            json={"comparison_id": "cmp_apply"})
        assert preview.status_code == 202
        preview_id = preview.json()["preview_id"]
        assert client.get(f"/api/apply/preview/{preview_id}", headers=WEB).json()["status"] == "queued"
        complete_job(client, root, apply_job)
        view = client.get(f"/api/apply/preview/{preview_id}", headers=WEB).json()
        assert view["status"] == "done"
        assert view["changes"] == [{"path": "SKILL.md", "action": "replace"}]
        rejected = client.post("/api/apply", headers=WEB,
            json={"preview_id": preview_id, "change_set_sha256": "0" * 64})
        assert rejected.status_code == 409
        applied = client.post("/api/apply", headers=WEB,
            json={"preview_id": preview_id, "change_set_sha256": view["change_set_sha256"]})
        assert applied.status_code == 202, applied.text
        apply_id = applied.json()["apply_id"]
        repeated = client.post("/api/apply", headers=WEB,
            json={"preview_id": preview_id, "change_set_sha256": view["change_set_sha256"]})
        assert repeated.status_code == 202 and repeated.json()["apply_id"] == apply_id
        complete_job(client, root, apply_job)
        assert (target / "SKILL.md").read_bytes() == b"new skill\n"
        status = client.get(f"/api/apply/{apply_id}", headers=WEB).json()
        assert status["status"] == "applied" and status["rollback_available"]
        assert client.post(f"/api/apply/{apply_id}/rollback", headers=WEB,
            json={"confirmed": False}).status_code == 422
        queued = client.post(f"/api/apply/{apply_id}/rollback", headers=WEB,
            json={"confirmed": True})
        assert queued.status_code == 202
        complete_job(client, root, rollback_job)
        assert client.get(f"/api/apply/{apply_id}", headers=WEB).json()["status"] == "rolled_back"
        assert (target / "SKILL.md").read_bytes() == b"old skill\n"
    with sessions() as session:
        record = session.get(ApplyRecord, apply_id)
        assert record.applied_sha256 == digest
        assert session.get(Snapshot, record.snapshot_id).verified


def test_apply_requires_user_choice_and_confirmed_candidate(database):
    engine, sessions, _ = prepared(database)
    with sessions.begin() as session:
        session.get(Comparison, "cmp_apply").user_decision = "baseline"
    with TestClient(create_app(database_url=engine.url.render_as_string(hide_password=False),
                               local_token="test-token")) as client:
        response = client.post("/api/apply/preview", headers=WEB,
            json={"comparison_id": "cmp_apply"})
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "CANDIDATE_NOT_SELECTED"


def test_changed_skill_after_preview_requires_recovery_check(database, tmp_path):
    engine, _, _ = prepared(database)
    root = tmp_path / "project"
    target = root / ".agents" / "skills" / "review-skill"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_bytes(b"old skill\n")
    with TestClient(create_app(database_url=engine.url.render_as_string(hide_password=False),
                               local_token="test-token")) as client:
        preview_id = client.post("/api/apply/preview", headers=WEB,
            json={"comparison_id": "cmp_apply"}).json()["preview_id"]
        complete_job(client, root, apply_job)
        view = client.get(f"/api/apply/preview/{preview_id}", headers=WEB).json()
        apply_id = client.post("/api/apply", headers=WEB,
            json={"preview_id": preview_id, "change_set_sha256": view["change_set_sha256"]}).json()["apply_id"]
        (target / "SKILL.md").write_bytes(b"user edit\n")
        claim = client.get("/api/agent/jobs", headers=BEARER).json()
        try:
            asyncio.run(apply_job(claim["payload"], root))
        except AgentError:
            pass
        else:
            raise AssertionError("stale preview was installed")
        assert client.post(f"/api/agent/jobs/{claim['job_id']}/result", headers=BEARER,
            json={"claim_token": claim["claim_token"], "status": "failed",
                  "message": "미리보기 이후 변경됨"}).status_code == 204
        assert client.get(f"/api/apply/{apply_id}", headers=WEB).json()["status"] == "recovery_required"
        assert (target / "SKILL.md").read_bytes() == b"user edit\n"
