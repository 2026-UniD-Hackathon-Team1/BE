"""다중 기기의 작업 대상 고정과 만료 처리 및 사용자 격리를 검증한다."""
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.agent import secret_hash
from backend.agent_jobs import JobSpec, enqueue_job
from backend.main import create_app
from backend.models import AgentDevice, AgentJob, ApplyRecord, Environment, utc_now
from test_apply_api import prepared, complete_job
from galtalka_agent.skill_switch import apply_job

WEB = {"X-Local-Token": "test-token"}


def select_second(sessions):
    """기존 기기를 보존하고 새 기본 기기를 등록한 상태를 만든다."""
    with sessions.begin() as session:
        session.get(AgentDevice, "device_test").is_primary = False
        session.flush()
        session.add(AgentDevice(id="device_second", profile_id="local", token_hash=secret_hash("u" * 43),
            device_name="second", agent_version="test", is_primary=True, last_seen_at=utc_now()))


def project(root):
    """실제 사용자 파일 대신 스킬 적용 검증용 임시 파일을 만든다."""
    target = root / ".agents" / "skills" / "review-skill"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_bytes(b"old skill\n")


def test_expired_previous_device_does_not_block_new_primary(database):
    """이전 기기가 폴링하지 않아도 만료된 실행을 정리하고 새 작업을 인수한다."""
    with TestClient(create_app(database_url=database[2].attributes["database_url"], local_token="test-token")) as api:
        api.put("/api/profile", headers=WEB, json={"role": "dev", "primary_language": "python", "task_types": ["code_review"]})

        def pair(name):
            """서버의 실제 일회용 코드 교환으로 새 기기를 등록한다."""
            code = api.post("/api/agent/pairing-code", headers=WEB).json()["code"]
            token = api.post("/api/agent/pair", json={"code": code, "device_name": name, "agent_version": "test"}).json()["agent_token"]
            return {"Authorization": "Bearer " + token}

        first = pair("first")
        old = enqueue_job(database[1], "local", JobSpec(kind="suggest_inputs", request_key="old", payload={}))
        claim = api.get("/api/agent/jobs", headers=first).json()
        second = pair("second")
        with database[1].begin() as session:
            session.get(AgentJob, old.job_id).lease_expires_at = "2000-01-01T00:00:00.000000Z"
        new = enqueue_job(database[1], "local", JobSpec(kind="suggest_inputs", request_key="new", payload={}))
        assert api.get("/api/agent/jobs", headers=second).json()["job_id"] == new.job_id
        assert api.post(f"/api/agent/jobs/{old.job_id}/result", headers=first,
            json={"claim_token": claim["claim_token"], "status": "done"}).status_code == 409
        with database[1]() as session:
            assert session.get(AgentJob, old.job_id).status == "queued"
            assert session.get(AgentJob, new.job_id).status == "running"


def test_install_is_bound_to_preview_device_and_is_atomic(database, tmp_path):
    """미리보기 후 기기를 바꾸면 다른 PC에 쓰지 않고 DB 기록도 롤백한다."""
    _, sessions, _ = prepared(database)
    project(tmp_path)
    with TestClient(create_app(database_url=database[2].attributes["database_url"], local_token="test-token")) as api:
        preview = api.post("/api/apply/preview", headers=WEB, json={"comparison_id": "cmp_apply"}).json()
        complete_job(api, tmp_path, apply_job)
        view = api.get("/api/apply/preview/" + preview["preview_id"], headers=WEB).json()
        assert view["device_id"] == "device_test"
        select_second(sessions)
        body = {"preview_id": preview["preview_id"], "change_set_sha256": view["change_set_sha256"]}
        blocked = api.post("/api/apply", headers=WEB, json=body)
        assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "DEVICE_CHANGED"
        with sessions() as session:
            assert session.scalar(select(func.count()).select_from(ApplyRecord)) == 0
            assert session.scalar(select(func.count()).select_from(AgentJob)) == 1
        assert api.put("/api/agent/devices/device_test/primary", headers=WEB).status_code == 204
        accepted = api.post("/api/apply", headers=WEB, json=body)
        assert accepted.status_code == 202 and accepted.json()["device_id"] == "device_test"


def test_rollback_uses_original_install_device(database, tmp_path):
    """복원도 다른 기본 기기에 보내지 않고 실제 적용 기기를 요구한다."""
    _, sessions, _ = prepared(database)
    project(tmp_path)
    with TestClient(create_app(database_url=database[2].attributes["database_url"], local_token="test-token")) as api:
        preview = api.post("/api/apply/preview", headers=WEB, json={"comparison_id": "cmp_apply"}).json()
        complete_job(api, tmp_path, apply_job)
        view = api.get("/api/apply/preview/" + preview["preview_id"], headers=WEB).json()
        applied = api.post("/api/apply", headers=WEB, json={"preview_id": preview["preview_id"],
            "change_set_sha256": view["change_set_sha256"]}).json()
        complete_job(api, tmp_path, apply_job)
        assert api.get("/api/apply/" + applied["apply_id"], headers=WEB).json()["status"] == "applied"
        select_second(sessions)
        path = "/api/apply/" + applied["apply_id"] + "/rollback"
        blocked = api.post(path, headers=WEB, json={"confirmed": True})
        assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "DEVICE_CHANGED"
        api.put("/api/agent/devices/device_test/primary", headers=WEB)
        accepted = api.post(path, headers=WEB, json={"confirmed": True})
        assert accepted.status_code == 202 and accepted.json()["device_id"] == "device_test"


def test_accounts_cannot_select_or_poll_other_users_devices(database):
    """여러 로그인 사용자의 기기 목록과 기본 기기 변경을 격리한다."""
    with TestClient(create_app(database_url=database[2].attributes["database_url"], local_token="test-token")) as api:
        owners = []
        for name in ("alice", "bob"):
            token = api.post("/api/auth/signup", json={"username": name, "password": "account-password"}).json()["token"]
            headers = {"Authorization": "Bearer " + token}
            api.put("/api/profile", headers=headers, json={"role": "dev", "primary_language": "python", "task_types": ["code_review"]})
            code = api.post("/api/agent/pairing-code", headers=headers).json()["code"]
            api.post("/api/agent/pair", json={"code": code, "device_name": name, "agent_version": "test"})
            device = api.get("/api/agent/devices", headers=headers).json()["devices"][0]["device_id"]
            owners.append((headers, device))
        assert api.put("/api/agent/devices/" + owners[0][1] + "/primary", headers=owners[1][0]).status_code == 404
        assert [item["device_id"] for item in api.get("/api/agent/devices", headers=owners[1][0]).json()["devices"]] == [owners[1][1]]


def test_switch_requires_the_selected_devices_own_environment(database):
    """기기 전환 뒤 이전 PC 환경을 보여 주거나 그 경로로 입력을 수집하지 않는다."""
    from backend.environment_data import current_environment
    _, sessions, _ = prepared(database)
    with sessions.begin() as session:
        session.add(Environment(id="old-env", profile_id="local", device_id="device_test", runner="codex_cli",
            runner_version="test", model_id="test", installed_skills=[], settings_hash="old",
            scan_root="agent://device_test/old-scan"))
    with sessions() as session:
        assert current_environment(session, "local").id == "old-env"
    select_second(sessions)
    with TestClient(create_app(database_url=database[2].attributes["database_url"], local_token="test-token")) as api:
        assert api.get("/api/environment", headers=WEB).status_code == 404
        response = api.post("/api/inputs", headers=WEB, json={"relative_paths": ["app.py"], "task_type": "code_review"})
        assert response.status_code == 409 and response.json()["error"]["code"] == "environment_not_ready"
        assert api.get("/api/agent", headers=WEB).json()["device_name"] == "second"
    with sessions.begin() as session:
        session.add(Environment(id="new-env", profile_id="local", device_id="device_second", runner="codex_cli",
            runner_version="test", model_id="test", installed_skills=[], settings_hash="new",
            scan_root="agent://device_second/new-scan", scanned_at="2020-01-01T00:00:00.000000Z"))
    with sessions() as session:
        assert current_environment(session, "local").id == "new-env"
