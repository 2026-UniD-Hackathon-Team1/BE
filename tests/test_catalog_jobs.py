"""모의 HTTP와 실제 PostgreSQL로 서버 수집의 수명주기를 검증한다."""
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import httpx
import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.catalog.github_client import CollectionError, GithubClient
from backend.catalog.jobs import catalog_router, run_job
from backend.models import Base, CatalogSyncJob
from test_catalog import FakeGithub


def test_catalog_schema_matches_models(database):
    """실제 마이그레이션 결과와 전체 모델 사이의 스키마 차이가 없는지 확인한다."""
    with database[0].connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []


@pytest.fixture
def sync_api(database):
    """외부 호출 없이 실제 라우터와 수집기의 통합 환경을 구성한다."""
    sessions = database[1]
    remote = FakeGithub()

    def factory(**kwargs):
        """명시된 요청 예산을 유지한 채 HTTP만 모의 전송으로 교체한다."""
        return GithubClient(transport=httpx.MockTransport(remote.handle), **kwargs)

    def auth():
        """테스트에서 인증된 프로필 식별자를 제공한다."""
        return "local"

    app = FastAPI()
    app.include_router(catalog_router(sessions, auth, client_factory=factory))
    with TestClient(app) as client:
        yield client, sessions, remote, factory


def test_sync_idempotency_results_and_restart(sync_api):
    """중복 요청은 재수집하지 않고 새 앱에서도 결과가 보존되는지 확인한다."""
    client, sessions, remote, factory = sync_api
    payload = {"discovery_queries": ["code review"], "discovery_limit": 1}
    headers = {"Idempotency-Key": "first"}
    response = client.post("/api/catalog/sync", json=payload, headers=headers)
    assert response.status_code == 202
    job_id = response.json()["id"]
    result = client.get("/api/catalog/jobs/" + job_id).json()
    assert result["status"] == "done"
    assert result["result"]["items"][0]["new_versions"] == 2
    assert result["request_count"] == len(remote.requests) == 8
    count = len(remote.requests)
    assert client.post("/api/catalog/sync", json=payload, headers=headers).json()["id"] == job_id
    assert len(remote.requests) == count
    assert client.post("/api/catalog/sync", json={**payload, "max_requests": 1}, headers=headers).status_code == 409
    run_job(sessions, job_id, factory)
    assert len(remote.requests) == count
    app = FastAPI()
    app.include_router(catalog_router(sessions, lambda: "local", client_factory=factory))
    with TestClient(app) as restarted:
        assert restarted.get("/api/catalog/jobs/" + job_id).json() == result
    other = FastAPI()
    other.include_router(catalog_router(sessions, lambda: "other", client_factory=factory))
    with TestClient(other) as outsider:
        assert outsider.get("/api/catalog/jobs/" + job_id).status_code == 404


def test_budget_stops_requests_and_keeps_failure(sync_api):
    """수집 중 예산을 넘으면 더 호출하지 않고 실패 이력을 남긴다."""
    client, sessions, remote, _ = sync_api
    response = client.post("/api/catalog/sync", json={"repositories": [{"repository": "example/review"}],
        "max_requests": 2}, headers={"Idempotency-Key": "limited"})
    result = client.get("/api/catalog/jobs/" + response.json()["id"]).json()
    assert result["status"] == "failed"
    assert result["request_count"] == len(remote.requests) == 2
    assert result["result"]["items"][0]["error"] == "request_budget_exceeded"
    with sessions() as session:
        assert session.get(CatalogSyncJob, response.json()["id"]).active_slot is None


def test_expired_worker_releases_slot_and_does_not_resume(sync_api):
    """종료된 서버 작업은 만료 후 중단 표시하고 새 키로만 다시 요청한다."""
    client, sessions, remote, factory = sync_api
    with sessions.begin() as session:
        session.add(CatalogSyncJob(id="stale", profile_id="local", request_key="stale",
            request_hash="old", config_json={"max_requests": 20}, status="running", active_slot=1,
            expires_at="2000-01-01T00:00:00.000000Z"))
    assert client.get("/api/catalog/jobs/stale").json()["status"] == "interrupted"
    run_job(sessions, "stale", factory)
    assert remote.requests == []
    response = client.post("/api/catalog/sync", json={"discovery_queries": ["review"]},
                           headers={"Idempotency-Key": "after-restart"})
    assert response.status_code == 202


def test_active_job_blocks_new_work_and_validates_requests(sync_api):
    """다른 활성 작업과 잘못된 설정은 외부 HTTP 요청 전에 거부한다."""
    client, sessions, remote, _ = sync_api
    with sessions.begin() as session:
        session.add(CatalogSyncJob(id="busy", profile_id="local", request_key="busy", request_hash="old",
            config_json={"max_requests": 20}, active_slot=1, expires_at="2999-01-01T00:00:00.000000Z"))
    payload = {"discovery_queries": ["review"]}
    assert client.post("/api/catalog/sync", json=payload, headers={"Idempotency-Key": "new"}).status_code == 409
    assert client.post("/api/catalog/sync", json=payload).status_code == 422
    assert client.post("/api/catalog/sync", json={}, headers={"Idempotency-Key": "empty"}).status_code == 422
    assert client.post("/api/catalog/sync", json={**payload, "token": "secret"},
                       headers={"Idempotency-Key": "secret"}).status_code == 422
    assert remote.requests == []


def test_deadline_stops_requests_before_network():
    """늦게 도착한 응답도 전체 제한 시간을 넘으면 저장하지 않는다."""
    remote = FakeGithub()
    client = GithubClient(transport=httpx.MockTransport(remote.handle), max_requests=1)
    client.deadline = time.monotonic() - 1
    with pytest.raises(CollectionError, match="collection_deadline_exceeded"):
        client.discover("review", 1)
    assert remote.requests == []
    client.close()


def test_response_after_deadline_is_rejected():
    """응답을 기다리는 사이 제한 시간이 지나도 성공 결과로 처리하지 않는다."""
    def late(request):
        """응답 도착 시점에 전체 수집 기한이 지난 상태를 재현한다."""
        client.deadline = time.monotonic() - 1
        return httpx.Response(200, json={"items": [], "incomplete_results": False})

    client = GithubClient(transport=httpx.MockTransport(late))
    with pytest.raises(CollectionError, match="collection_deadline_exceeded"):
        client.discover("review", 1)
    assert client.request_count == 1
    client.close()


def test_catalog_sync_requires_web_auth(database):
    """실제 앱의 인증을 우회해 수집을 실행하거나 이력을 읽을 수 없다."""
    from backend.main import create_app
    with TestClient(create_app(database_url=database[2].attributes["database_url"],
                              local_token="test-local-token")) as client:
        assert client.post("/api/catalog/sync", json={"discovery_queries": ["review"]},
                           headers={"Idempotency-Key": "unauthorized"}).status_code == 401
        assert client.get("/api/catalog/jobs/anything").status_code == 401
        schema = client.get("/openapi.json").json()
        assert schema["paths"]["/api/catalog/sync"]["post"]["security"]


def test_concurrent_requests_share_job_and_block_other_run(sync_api):
    """실제 병렬 요청에서 같은 키는 재사용하고 다른 키는 중복 실행하지 않는다."""
    client, _, remote, _ = sync_api
    entered, release = Event(), Event()
    original = remote.handle

    def slow(request):
        """수집 중 요청을 고정해 동시 제출 시점의 상태를 검사한다."""
        entered.set()
        assert release.wait(10)
        return original(request)

    remote.handle = slow
    # factory는 생성 시점의 handle을 참조하므로 이미 만든 라우터에도 반영된다.
    payload = {"discovery_queries": ["review"], "discovery_limit": 1}
    headers = {"Idempotency-Key": "concurrent"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(client.post, "/api/catalog/sync", json=payload, headers=headers)
        try:
            assert entered.wait(10)
            second = client.post("/api/catalog/sync", json=payload, headers=headers)
            assert second.status_code == 202
            assert second.json()["status"] == "running"
            busy = client.post("/api/catalog/sync", json=payload, headers={"Idempotency-Key": "another"})
            assert busy.status_code == 409
        finally:
            release.set()
        assert first.result().json()["id"] == second.json()["id"]
    assert len(remote.requests) == 8


def test_worker_exception_is_safe_and_releases_lock(sync_api):
    """예상하지 못한 오류도 비밀값 없이 종료하고 다음 작업을 허용한다."""
    client, sessions, remote, factory = sync_api
    with sessions.begin() as session:
        session.add(CatalogSyncJob(id="broken", profile_id="local", request_key="broken", request_hash="x",
            config_json={"discovery_queries": ["review"], "max_requests": 20}, active_slot=1,
            expires_at="2999-01-01T00:00:00.000000Z"))

    def broken(**kwargs):
        """인증 정보가 들어 있는 예외를 모의로 발생시킨다."""
        raise RuntimeError("secret database password")

    run_job(sessions, "broken", broken)
    result = client.get("/api/catalog/jobs/broken").json()
    assert result["status"] == "failed"
    assert result["error"] == "collection_failed"
    assert "secret" not in str(result)
    assert remote.requests == []
    response = client.post("/api/catalog/sync", json={"discovery_queries": ["review"]},
                           headers={"Idempotency-Key": "after-failure"})
    assert response.status_code == 202
    assert client.get("/api/catalog/jobs/" + response.json()["id"]).json()["status"] == "done"
