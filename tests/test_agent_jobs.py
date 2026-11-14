from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from alembic import command
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select

import backend.agent as agent
from backend.agent_jobs import JobSpec, enqueue_job
from backend.main import create_app
from backend.models import AgentDevice, AgentJob, Profile

WEB = {"X-Local-Token": "test-local-token"}


@pytest.fixture
def queue(database):
    """실제 PostgreSQL과 모의 기기 토큰으로 큐 API 환경을 준비한다."""
    url = database[0].url.render_as_string(hide_password=False)
    with TestClient(create_app(database_url=url, local_token="test-local-token")) as client:
        client.put("/api/profile", headers=WEB, json={"role": "developer", "primary_language": "python",
            "task_types": ["code_review"]})
        code = client.post("/api/agent/pairing-code", headers=WEB).json()["code"]
        paired = client.post("/api/agent/pair", json={"code": code, "device_name": "test-pc", "agent_version": "test"})
        bearer = {"Authorization": "Bearer " + paired.json()["agent_token"]}
        assert client.post("/api/agent/heartbeat", headers=bearer,
            json={"codex_version": "test", "codex_ready": True}).status_code == 204
        yield client, database[1], bearer


def add_job(sessions, key="request-1", kind="scan", profile="local"):
    """파일 원문 대신 고정 식별자를 담은 서버 내부 작업을 생성한다."""
    return enqueue_job(sessions, profile, JobSpec(kind=kind, request_key=key, payload={"scope_id": "approved-project"}))


def test_job_roundtrip_and_duplicate_results(queue, database):
    """진행과 결과를 영속 저장하며 웹에 토큰을 노출하지 않고 결과 재전송을 처리한다."""
    client, sessions, bearer = queue
    queued = add_job(sessions)
    assert queued.status == "queued"
    response = client.get("/api/agent/jobs", headers=bearer)
    assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    claim = response.json()
    assert claim["job_id"] == queued.job_id and claim["attempt"] == 1
    assert client.get("/api/agent/jobs", headers=bearer).json() is None
    assert client.post(f"/api/agent/jobs/{queued.job_id}/progress", headers=bearer,
        json={"claim_token": claim["claim_token"], "step": "scanning", "detail": "확인 중"}).status_code == 204
    state = client.get(f"/api/agent/jobs/{queued.job_id}", headers=WEB).json()
    assert state["progress_step"] == "scanning"
    assert "claim_token" not in state and "claim_token_hash" not in state and "payload" not in state
    body = {"claim_token": claim["claim_token"], "status": "done", "result": {"languages": ["python"]}}
    path = f"/api/agent/jobs/{queued.job_id}/result"
    assert client.post(path, headers=bearer, json=body).status_code == 204
    assert client.post(path, headers=bearer, json=body).status_code == 204
    assert client.post(path, headers=bearer, json={**body, "result": {"different": True}}).status_code == 409
    with sessions() as session:
        stored = session.get(AgentJob, queued.job_id)
        assert stored.claim_token_hash == agent.secret_hash(claim["claim_token"])
    with TestClient(create_app(database_url=database[0].url.render_as_string(hide_password=False),
                              local_token="test-local-token")) as restarted:
        state = restarted.get(f"/api/agent/jobs/{queued.job_id}", headers=WEB).json()
        assert state["status"] == "done" and state["result"] == {"languages": ["python"]}
        assert restarted.post(path, headers=bearer, json=body).status_code == 204
    command.check(database[2])


def test_enqueue_and_claim_are_atomic_and_idempotent(queue):
    """동시 생성은 한 행으로 모으고 동시 폴링은 한 요청에만 실행 권한을 준다."""
    client, sessions, bearer = queue
    with ThreadPoolExecutor(max_workers=2) as executor:
        created = list(executor.map(lambda _: add_job(sessions), range(2)))
    assert created[0].job_id == created[1].job_id
    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(lambda _: client.get("/api/agent/jobs", headers=bearer).json(), range(2)))
    assert sum(claim is not None for claim in claims) == 1
    second = add_job(sessions, key="request-2")
    assert client.get("/api/agent/jobs", headers=bearer).json() is None
    first = next(claim for claim in claims if claim is not None)
    assert client.post(f"/api/agent/jobs/{first['job_id']}/result", headers=bearer,
        json={"claim_token": first["claim_token"], "status": "failed", "message": "모의 실패"}).status_code == 204
    assert client.get("/api/agent/jobs", headers=bearer).json()["job_id"] == second.job_id
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(AgentJob)) == 2
    with pytest.raises(HTTPException) as error:
        enqueue_job(sessions, "local", JobSpec(kind="scan", request_key="request-1", payload={"scope_id": "different"}))
    assert error.value.status_code == 409


def test_read_job_retries_and_rejects_old_claim(queue, monkeypatch):
    """만료된 읽기 작업은 최대 세 번 인수하며 이전 실행의 결과는 거부한다."""
    client, sessions, bearer = queue
    now = agent.current_time()
    monkeypatch.setattr(agent, "current_time", lambda: now)
    job = add_job(sessions)
    previous = None
    for attempt in range(1, 4):
        claim = client.get("/api/agent/jobs", headers=bearer).json()
        assert claim["attempt"] == attempt
        if previous is not None:
            assert client.post(f"/api/agent/jobs/{job.job_id}/result", headers=bearer,
                json={"claim_token": previous, "status": "done"}).status_code == 409
        previous = claim["claim_token"]
        now += timedelta(seconds=121)
        assert client.post(f"/api/agent/jobs/{job.job_id}/progress", headers=bearer,
            json={"claim_token": previous, "step": "late"}).status_code == 409
        state = client.get(f"/api/agent/jobs/{job.job_id}", headers=WEB).json()
        assert state["status"] == ("queued" if attempt < 3 else "failed")
    assert client.get("/api/agent/jobs", headers=bearer).json() is None


@pytest.mark.parametrize("kind", ["run_comparison", "apply", "rollback"])
def test_costly_or_write_jobs_are_never_automatically_retried(queue, monkeypatch, kind):
    """비교 비용과 파일 변경을 반복하지 않도록 만료된 작업을 종료한다."""
    client, sessions, bearer = queue
    now = agent.current_time()
    monkeypatch.setattr(agent, "current_time", lambda: now)
    job = add_job(sessions, kind=kind)
    claim = client.get("/api/agent/jobs", headers=bearer).json()
    now += timedelta(seconds=121)
    assert client.get("/api/agent/jobs", headers=bearer).json() is None
    state = client.get(f"/api/agent/jobs/{job.job_id}", headers=WEB).json()
    assert state["status"] == "failed" and state["attempts"] == 1
    assert client.post(f"/api/agent/jobs/{job.job_id}/result", headers=bearer,
        json={"claim_token": claim["claim_token"], "status": "done"}).status_code == 409


def test_progress_renews_lease_with_execution_cap(queue, monkeypatch):
    """진행 보고로 기한을 연장하되 총 실행 상한을 넘기지 않는다."""
    client, sessions, bearer = queue
    now = agent.current_time()
    monkeypatch.setattr(agent, "current_time", lambda: now)
    job = add_job(sessions)
    claim = client.get("/api/agent/jobs", headers=bearer).json()
    now += timedelta(seconds=60)
    body = {"claim_token": claim["claim_token"], "step": "working"}
    assert client.post(f"/api/agent/jobs/{job.job_id}/progress", headers=bearer, json=body).status_code == 204
    with sessions.begin() as session:
        stored = session.get(AgentJob, job.job_id)
        assert stored.lease_expires_at == agent.iso_time(now + timedelta(seconds=120))
        stored.started_at = agent.iso_time(now - timedelta(seconds=1790))
    assert client.post(f"/api/agent/jobs/{job.job_id}/progress", headers=bearer, json=body).status_code == 204
    with sessions() as session:
        assert session.get(AgentJob, job.job_id).lease_expires_at == agent.iso_time(now + timedelta(seconds=10))
    now += timedelta(seconds=11)
    assert client.post(f"/api/agent/jobs/{job.job_id}/progress", headers=bearer, json=body).status_code == 409


def test_other_device_and_web_cannot_report_jobs(queue):
    """다른 기기와 웹 토큰이 작업을 인수하거나 결과를 쓰지 못하게 한다."""
    client, sessions, bearer = queue
    job = add_job(sessions)
    claim = client.get("/api/agent/jobs", headers=bearer).json()
    with sessions.begin() as session:
        session.add(Profile(id="other", role="test", primary_language="python", task_types=[], goals=[]))
        session.flush()
        session.add(AgentDevice(id="other-agent", profile_id="other", token_hash=agent.secret_hash("other-token"),
            device_name="private", agent_version="test", codex_ready=False, last_seen_at=agent.iso_time(agent.current_time())))
    other = {"Authorization": "Bearer other-token"}
    assert client.get("/api/agent/jobs", headers=other).json() is None
    assert client.post(f"/api/agent/jobs/{job.job_id}/result", headers=other,
        json={"claim_token": claim["claim_token"], "status": "done"}).status_code == 404
    assert client.get("/api/agent/jobs", headers=WEB).status_code == 401
    assert client.get(f"/api/agent/jobs/{job.job_id}", headers=bearer).status_code == 401
    private = add_job(sessions, profile="other", key="other-request")
    assert client.get(f"/api/agent/jobs/{private.job_id}", headers=WEB).status_code == 404
    assert client.get("/openapi.json").json()["paths"]["/api/agent/jobs"]["get"]["security"] == [{"AgentToken": []}]


@pytest.mark.parametrize("reconnect", [False, True])
def test_revocation_cancels_queued_and_running_jobs(queue, reconnect):
    """연결 해제와 재연결이 이전 기기 작업 및 보고 권한을 함께 취소한다."""
    client, sessions, bearer = queue
    first = add_job(sessions)
    claim = client.get("/api/agent/jobs", headers=bearer).json()
    second = add_job(sessions, key="request-2")
    if reconnect:
        code = client.post("/api/agent/pairing-code", headers=WEB).json()["code"]
        assert client.post("/api/agent/pair", json={"code": code, "device_name": "replacement", "agent_version": "test"}).status_code == 200
    else:
        assert client.delete("/api/agent", headers=WEB).status_code == 204
    assert client.get("/api/agent/jobs", headers=bearer).status_code == 401
    assert client.post(f"/api/agent/jobs/{first.job_id}/result", headers=bearer,
        json={"claim_token": claim["claim_token"], "status": "done"}).status_code == 401
    for job in (first, second):
        assert client.get(f"/api/agent/jobs/{job.job_id}", headers=WEB).json()["status"] == "cancelled"
    assert add_job(sessions).job_id == first.job_id


def test_enqueue_requires_online_device_and_codex_readiness(queue, monkeypatch):
    """꺼진 기기와 Codex 미준비 상태에서 새 작업을 만들지 않는다."""
    client, sessions, bearer = queue
    assert client.post("/api/agent/heartbeat", headers=bearer, json={}).status_code == 204
    with pytest.raises(HTTPException) as error:
        add_job(sessions, kind="run_comparison")
    assert error.value.detail["code"] == "CODEX_NOT_READY"
    later = agent.current_time() + timedelta(seconds=61)
    monkeypatch.setattr(agent, "current_time", lambda: later)
    with pytest.raises(HTTPException) as error:
        add_job(sessions)
    assert error.value.detail["code"] == "AGENT_OFFLINE"


def test_invalid_payload_and_reports_are_rejected(queue):
    """지원하지 않는 작업과 과도한 JSON 및 필수 인수 토큰 누락을 거부한다."""
    client, sessions, bearer = queue
    with pytest.raises(ValidationError):
        JobSpec(kind="shell", request_key="bad", payload={"command": "echo"})
    with pytest.raises(ValidationError):
        JobSpec(kind="scan", request_key="big", payload={"value": "x" * (128 * 1024)})
    job = add_job(sessions)
    claim = client.get("/api/agent/jobs", headers=bearer).json()
    assert client.post(f"/api/agent/jobs/{job.job_id}/result", headers=bearer,
        json={"status": "done"}).status_code == 422
    assert client.post(f"/api/agent/jobs/{job.job_id}/result", headers=bearer,
        json={"claim_token": claim["claim_token"], "status": "done", "result": {"value": "x" * (256 * 1024)}}).status_code == 422
