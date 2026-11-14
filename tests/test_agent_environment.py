import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.main import create_app
from backend.models import AgentJob, Environment
from galtalka_agent.client import AgentClient
from galtalka_agent.config import AgentConfig
from galtalka_agent.scan import collect_scan, scan
from galtalka_agent.worker import AgentRunner

WEB = {"X-Local-Token": "test-local-token"}


@pytest.fixture
def environment_api(database):
    """실제 PostgreSQL과 짧은 대기 시간을 사용하는 웹 및 기기 인증을 준비한다."""
    app = create_app(database_url=database[2].attributes["database_url"], local_token="test-local-token", agent_wait_timeout=0)
    with TestClient(app) as client:
        client.put("/api/profile", headers=WEB, json={"role": "developer", "primary_language": "python", "task_types": ["code_review"]})
        code = client.post("/api/agent/pairing-code", headers=WEB).json()["code"]
        token = client.post("/api/agent/pair", json={"code": code, "device_name": "fixture-pc", "agent_version": "test"}).json()["agent_token"]
        yield client, token


def run_pc(client, token, root, pending_path):
    """PC CLI와 실제 서버 API 사이만 모의 전송하고 임시 프로젝트를 스캔한다."""
    def dispatch(request):
        """외부 네트워크 대신 TestClient로 실제 서버 요청을 전달한다."""
        response = client.request(request.method, request.url.raw_path.decode(), content=request.content,
                                  headers=dict(request.headers))
        return httpx.Response(response.status_code, content=response.content, headers=response.headers)
    async def execute():
        """한 번의 기기 폴링과 scan 핸들러 실행 및 결과 보고를 수행한다."""
        http = AgentClient("https://testserver", token, transport=httpx.MockTransport(dispatch))
        try:
            await AgentRunner(http, AgentConfig("https://testserver", root, token), pending_path,
                              handlers={"scan": scan}).run(once=True)
        finally:
            await http.close()
    asyncio.run(execute())


def test_scan_timeout_requery_persistence_and_refresh(environment_api, database, tmp_path, monkeypatch):
    """웹 요청과 PC 스캔 및 환경 저장을 연결하고 재조회와 새 스캔을 검증한다."""
    client, token = environment_api
    (tmp_path / ".codex").mkdir()
    config = tmp_path / ".codex/config.toml"
    config.write_text('model="gpt-example"\napi_key="DO_NOT_UPLOAD"', encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("fastapi==1", encoding="utf-8")
    body = {"root": ".", "scopes": ["config", "skills"]}
    first = client.post("/api/environment/scan", headers={**WEB, "Idempotency-Key": "scan-first"}, json=body)
    assert first.status_code == 504 and first.headers["Retry-After"] == "2"
    job_id = first.json()["error"]["job_id"]
    pending = client.get("/api/environment", headers=WEB, params={"job_id": job_id})
    assert pending.status_code == 202 and pending.json()["job_id"] == job_id
    run_pc(client, token, tmp_path.resolve(), tmp_path / "agent-config.json")
    fetched = client.get("/api/environment", headers=WEB)
    assert fetched.status_code == 200
    value = fetched.json()
    assert value["model_id"] == "gpt-example" and value["runner_version"] == "unknown"
    assert value["scan_root"] == str(tmp_path.resolve()) and value["frameworks"] == ["fastapi"]
    assert value["verification"] == "configuration_only"
    assert "DO_NOT_UPLOAD" not in fetched.text
    repeated = client.post("/api/environment/scan", headers={**WEB, "Idempotency-Key": "scan-first"}, json=body)
    assert repeated.status_code == 200 and repeated.json() == value
    assert client.get("/api/environment", headers=WEB, params={"job_id": job_id}).json() == value
    with database[1]() as session:
        stored = session.scalar(select(Environment))
        assert stored.scan_root.startswith("agent://") and stored.scan_job_id == job_id
        assert session.scalar(select(func.count()).select_from(Environment)) == 1
    def forbidden(*args, **kwargs):
        """기기 환경을 서버 로컬 경로로 읽으려는 호출을 금지한다."""
        pytest.fail("agent path must not be opened on server")
    monkeypatch.setattr("backend.comparisons.register_input", forbidden)
    response = client.post("/api/inputs", headers=WEB, json={"relative_paths": ["app.py"], "task_type": "code_review"})
    assert response.status_code == 409 and response.json()["error"]["code"] == "AGENT_INPUTS_NOT_READY"
    config.write_text('model="gpt-second-example"', encoding="utf-8")
    assert client.post("/api/environment/scan", headers={**WEB, "Idempotency-Key": "scan-second"}, json=body).status_code == 504
    run_pc(client, token, tmp_path.resolve(), tmp_path / "agent-config.json")
    updated = client.get("/api/environment", headers=WEB).json()
    assert updated["model_id"] == "gpt-second-example" and updated["settings_hash"] != value["settings_hash"]
    with database[1]() as session:
        assert session.scalar(select(func.count()).select_from(Environment)) == 2
    command.check(database[2])


def test_scan_post_can_finish_while_waiting(database, tmp_path):
    """웹이 기다리는 동안 PC가 같은 서버의 큐 작업을 완료할 수 있다."""
    app = create_app(database_url=database[2].attributes["database_url"], local_token="test-local-token", agent_wait_timeout=3)
    with TestClient(app) as client:
        client.put("/api/profile", headers=WEB, json={"role": "developer", "primary_language": "python", "task_types": ["code_review"]})
        code = client.post("/api/agent/pairing-code", headers=WEB).json()["code"]
        token = client.post("/api/agent/pair", json={"code": code, "device_name": "pc", "agent_version": "test"}).json()["agent_token"]
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(client.post, "/api/environment/scan", headers=WEB, json={"root": ".", "scopes": ["config"]})
            deadline = time.monotonic() + 2
            while True:
                with database[1]() as session:
                    queued = session.scalar(select(AgentJob.id))
                if queued:
                    break
                assert time.monotonic() < deadline
                time.sleep(0.01)
            run_pc(client, token, tmp_path.resolve(), tmp_path / "agent-config.json")
            result = future.result(timeout=5)
        assert result.status_code == 200
        assert result.json()["scopes"] == ["config"]


def test_concurrent_retries_reuse_pending_scan(environment_api, database):
    """요청 키 없이 반복한 같은 스캔도 진행 중 작업 하나를 공유한다."""
    client, _ = environment_api
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: client.post("/api/environment/scan", headers=WEB,
            json={"root": ".", "scopes": ["config"]}), range(2)))
    assert all(result.status_code == 504 for result in results)
    assert results[0].json()["error"]["job_id"] == results[1].json()["error"]["job_id"]
    with database[1]() as session:
        assert session.scalar(select(func.count()).select_from(AgentJob)) == 1


def test_root_rejection_reports_permission_error(environment_api, database, tmp_path):
    """승인하지 않은 폴더 요청을 PC가 거부하고 웹에는 403 안내를 준다."""
    client, token = environment_api
    posted = client.post("/api/environment/scan", headers=WEB, json={"root": str(tmp_path.parent), "scopes": ["config"]})
    job_id = posted.json()["error"]["job_id"]
    run_pc(client, token, tmp_path.resolve(), tmp_path / "agent-config.json")
    response = client.get("/api/environment", headers=WEB, params={"job_id": job_id})
    assert response.status_code == 403 and response.json()["error"]["code"] == "ROOT_NOT_APPROVED"
    with database[1]() as session:
        assert session.scalar(select(func.count()).select_from(Environment)) == 0


def test_raw_or_broader_results_are_rejected_atomically(environment_api, database, tmp_path):
    """원문 업로드나 승인 범위 확대 결과는 작업 완료와 환경 저장 전에 거부한다."""
    client, token = environment_api
    posted = client.post("/api/environment/scan", headers=WEB, json={"root": ".", "scopes": ["config"]})
    job_id = posted.json()["error"]["job_id"]
    bearer = {"Authorization": "Bearer " + token}
    claim = client.get("/api/agent/jobs", headers=bearer).json()
    value = collect_scan(claim["payload"], tmp_path.resolve(), threading.Event())
    invalid = [{**value, "files": {"app.py": "PRIVATE"}}, {**value, "scopes": ["config", "skills"]}]
    for candidate in invalid:
        response = client.post(f"/api/agent/jobs/{job_id}/result", headers=bearer,
            json={"claim_token": claim["claim_token"], "status": "done", "result": candidate})
        assert response.status_code == 422
    with database[1]() as session:
        assert session.scalar(select(func.count()).select_from(Environment)) == 0
        assert session.get(AgentJob, job_id).status == "running"
    body = {"claim_token": claim["claim_token"], "status": "done", "result": value}
    assert client.post(f"/api/agent/jobs/{job_id}/result", headers=bearer, json=body).status_code == 204
    assert client.post(f"/api/agent/jobs/{job_id}/result", headers=bearer, json=body).status_code == 204
    with database[1]() as session:
        assert session.scalar(select(func.count()).select_from(Environment)) == 1


def test_auth_scopes_offline_and_cors(environment_api, database):
    """웹 인증과 범위 검증 및 연결 해제 오류를 프론트 계약으로 제공한다."""
    client, token = environment_api
    assert client.get("/api/environment", headers=WEB).status_code == 404
    assert client.get("/api/environment").status_code == 401
    assert client.post("/api/environment/scan", json={"root": ".", "scopes": ["config"]}).status_code == 401
    for body in ({"root": ".", "scopes": ["config", "write"]}, {"root": ".", "scopes": ["skills"]},
                 {"root": "file:///private", "scopes": ["config"]}):
        assert client.post("/api/environment/scan", headers=WEB, json=body).status_code == 422
    response = client.options("/api/environment/scan", headers={"Origin": "http://localhost:5173",
        "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "Idempotency-Key,X-Local-Token,Content-Type"})
    assert response.status_code == 200
    assert client.delete("/api/agent", headers=WEB).status_code == 204
    response = client.post("/api/environment/scan", headers=WEB, json={"root": ".", "scopes": ["config"]})
    assert response.status_code == 409 and response.json()["error"]["code"] == "AGENT_OFFLINE"
