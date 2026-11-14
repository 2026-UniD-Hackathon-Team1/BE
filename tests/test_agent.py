from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import backend.agent as agent
from backend.main import create_app
from backend.models import AgentDevice, AgentPairingCode, Profile

HEADERS = {"X-Local-Token": "test-local-token"}
PROFILE = {"role": "developer", "primary_language": "python", "task_types": ["code_review"]}


@pytest.fixture
def client(database):
    """마이그레이션을 적용한 DB와 단일 사용자 웹 인증으로 API를 준비한다."""
    with TestClient(create_app(database_url=database[0].url.render_as_string(hide_password=False), local_token="test-local-token")) as value:
        assert value.put("/api/profile", headers=HEADERS, json=PROFILE).status_code == 200
        yield value


def pair_device(client, name="MacBook-Pro"):
    """웹에서 발급한 코드만 사용해 모의 연결 프로그램을 등록한다."""
    response = client.post("/api/agent/pairing-code", headers=HEADERS)
    assert response.status_code == 200
    code = response.json()["code"]
    response = client.post("/api/agent/pair", json={"code": code, "device_name": name, "agent_version": "0.1.0"})
    assert response.status_code == 200
    return code, response.json()["agent_token"]


def test_pair_heartbeat_restart_and_auth_boundaries(client, database):
    """연결부터 재시작 후 상태 조회까지 검증하고 원문 비밀값과 인증 혼용을 차단한다."""
    assert client.get("/api/agent", headers=HEADERS).json()["status"] == "never_connected"
    code, token = pair_device(client)
    bearer = {"Authorization": "Bearer " + token}
    assert client.post("/api/agent/heartbeat", headers=bearer,
                       json={"codex_version": "test-version", "codex_ready": True}).status_code == 204
    state = client.get("/api/agent", headers=HEADERS).json()
    assert state["status"] == "connected" and state["codex_ready"]
    assert state["device_name"] == "MacBook-Pro"
    assert "agent_token" not in state
    assert client.get("/api/profile", headers=bearer).status_code == 401
    assert client.post("/api/agent/heartbeat", headers=HEADERS, json={}).status_code == 401
    assert client.post("/api/agent/heartbeat", headers={"Authorization": "Bearer wrong"}, json={}).status_code == 401
    with database[1]() as session:
        stored = session.scalar(select(AgentDevice))
        pairing = session.scalar(select(AgentPairingCode))
        assert stored.token_hash == agent.secret_hash(token) and stored.token_hash != token
        assert pairing.code_hash == agent.secret_hash(code.replace("-", ""))
        assert pairing.consumed_at is not None
    with TestClient(create_app(database_url=database[0].url.render_as_string(hide_password=False), local_token="test-local-token")) as restarted:
        assert restarted.get("/api/agent", headers=HEADERS).json() == state
        assert restarted.post("/api/agent/heartbeat", headers=bearer, json={}).status_code == 204


def test_code_expiry_reissue_and_device_offline(client, monkeypatch):
    """재발급한 코드만 유효하고 10분 코드 만료와 60초 연결 만료가 적용된다."""
    now = agent.current_time()
    monkeypatch.setattr(agent, "current_time", lambda: now)
    first = client.post("/api/agent/pairing-code", headers=HEADERS).json()["code"]
    second = client.post("/api/agent/pairing-code", headers=HEADERS).json()["code"]
    body = {"device_name": "Windows", "agent_version": "0.1.0"}
    assert client.post("/api/agent/pair", json={**body, "code": first}).status_code == 400
    now += timedelta(minutes=10)
    assert client.post("/api/agent/pair", json={**body, "code": second}).status_code == 400
    _, token = pair_device(client)
    now += timedelta(seconds=61)
    state = client.get("/api/agent", headers=HEADERS).json()
    assert state["status"] == "disconnected" and not state["codex_ready"]
    assert client.post("/api/agent/heartbeat", headers={"Authorization": "Bearer " + token}, json={}).status_code == 204
    assert client.get("/api/agent", headers=HEADERS).json()["status"] == "connected"


def test_reconnect_and_disconnect_revoke_tokens_and_pending_codes(client):
    """재연결과 해제 뒤 이전 토큰 및 대기 중 코드로 접근하지 못하게 한다."""
    code, old_token = pair_device(client, "old")
    body = {"code": code, "device_name": "repeat", "agent_version": "0.1.0"}
    assert client.post("/api/agent/pair", json=body).status_code == 400
    _, new_token = pair_device(client, "new")
    assert client.post("/api/agent/heartbeat", headers={"Authorization": "Bearer " + old_token}, json={}).status_code == 401
    assert client.get("/api/agent", headers=HEADERS).json()["device_name"] == "new"
    pending = client.post("/api/agent/pairing-code", headers=HEADERS).json()["code"]
    assert client.delete("/api/agent", headers=HEADERS).status_code == 204
    assert client.delete("/api/agent", headers=HEADERS).status_code == 204
    assert client.post("/api/agent/heartbeat", headers={"Authorization": "Bearer " + new_token}, json={}).status_code == 401
    assert client.post("/api/agent/pair", json={**body, "code": pending}).status_code == 400
    assert client.get("/api/agent", headers=HEADERS).json()["status"] == "disconnected"


def test_rate_limit_persists_and_resets(client, database, monkeypatch):
    """잘못된 코드 시도도 영속 제한에 포함하며 재시작 뒤에도 유지한다."""
    now = agent.current_time()
    monkeypatch.setattr(agent, "current_time", lambda: now)
    body = {"code": "AAAA-AAAA", "device_name": "test", "agent_version": "0.1.0"}
    for _ in range(10):
        assert client.post("/api/agent/pair", json=body).status_code == 400
    response = client.post("/api/agent/pair", json=body)
    assert response.status_code == 429 and response.headers["Retry-After"] == "60"
    with TestClient(create_app(database_url=database[0].url.render_as_string(hide_password=False), local_token="test-local-token")) as restarted:
        assert restarted.post("/api/agent/pair", json=body).status_code == 429
    now += timedelta(seconds=61)
    assert client.post("/api/agent/pair", json=body).status_code == 400
    for _ in range(5):
        assert client.post("/api/agent/pairing-code", headers=HEADERS).status_code == 200
    assert client.post("/api/agent/pairing-code", headers=HEADERS).status_code == 429


def test_code_is_consumed_once_under_concurrent_exchange(client, database):
    """같은 연결 코드의 동시 교환에서 한 요청만 기기를 등록한다."""
    code = client.post("/api/agent/pairing-code", headers=HEADERS).json()["code"]
    body = {"code": code, "device_name": "concurrent", "agent_version": "0.1.0"}
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: client.post("/api/agent/pair", json=body).status_code, range(2)))
    assert sorted(results) == [200, 400]
    with database[1]() as session:
        assert session.scalar(select(func.count()).select_from(AgentDevice)) == 1


def test_web_auth_scope_validation_and_openapi(client, database):
    """웹 인증과 기기 인증을 문서에서 구분하고 다른 사용자 기기를 노출하지 않는다."""
    for method, path in (("post", "/api/agent/pairing-code"), ("get", "/api/agent"), ("delete", "/api/agent")):
        assert getattr(client, method)(path).status_code == 401
    with database[1].begin() as session:
        session.add(Profile(id="other", role="test", primary_language="python", task_types=[], goals=[]))
        session.flush()
        session.add(AgentDevice(id="other-agent", profile_id="other", token_hash="other-hash", device_name="private",
            agent_version="0.1.0", codex_ready=False, last_seen_at=agent.iso_time(agent.current_time())))
    assert client.get("/api/agent", headers=HEADERS).json()["status"] == "never_connected"
    assert client.delete("/api/agent", headers=HEADERS).status_code == 204
    with database[1]() as session:
        assert session.get(AgentDevice, "other-agent").revoked_at is None
    _, token = pair_device(client)
    assert client.post("/api/agent/heartbeat", headers={"Authorization": "Bearer " + token},
                       json={"codex_ready": True}).status_code == 422
    schema = client.get("/openapi.json").json()
    assert schema["paths"]["/api/agent"]["get"]["security"] == [{"APIKeyHeader": []}]
    assert schema["paths"]["/api/agent/heartbeat"]["post"]["security"] == [{"AgentToken": []}]
    preflight = client.options("/api/agent", headers={"Origin": "http://localhost:5173",
        "Access-Control-Request-Method": "DELETE", "Access-Control-Request-Headers": "X-Local-Token"})
    assert preflight.status_code == 200
