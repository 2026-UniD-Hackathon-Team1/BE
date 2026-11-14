"""원격 호출 없는 시계 주입으로 대기와 예약 수집의 영속성을 검증한다."""
from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.catalog.github_client import CollectionError, GithubClient
from backend.catalog.jobs import catalog_router
from backend.catalog.policy import RequestPolicy, bind_policy, retry_epoch
from backend.catalog.refresh import refresh_router, run_refresh_tick
from backend.models import CatalogRuntime, CatalogSyncJob
from test_catalog import FakeGithub


def test_cooldown_survives_client_restart_and_expiry(database):
    """제한 응답 이후 새 클라이언트도 대기하며 만료 후에만 호출한다."""
    now = [1700000000]
    calls = []

    def limited(request):
        """남은 호출량과 reset을 포함한 GitHub 제한 응답을 만든다."""
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "90", "X-RateLimit-Reset": str(now[0] + 120)})

    client = bind_policy(database[1], GithubClient(transport=httpx.MockTransport(limited)), clock=lambda: now[0])
    with pytest.raises(CollectionError, match="rate_limited"):
        client.get("/repos/example/review")
    client.close()
    restarted = bind_policy(database[1], GithubClient(transport=httpx.MockTransport(limited)), clock=lambda: now[0])
    with pytest.raises(CollectionError, match="collection_cooldown"):
        restarted.get("/repos/example/review")
    assert len(calls) == 1 and restarted.request_count == 0
    now[0] += 122
    with pytest.raises(CollectionError, match="rate_limited"):
        restarted.get("/repos/example/review")
    assert len(calls) == 2
    restarted.close()


def test_success_with_zero_remaining_blocks_next_request(database):
    """마지막 허용 응답은 처리하되 남은 횟수가 0이면 다음 호출을 막는다."""
    calls = []

    def exhausted(request):
        """성공 응답에도 잔여 요청 수가 없는 상황을 재현한다."""
        calls.append(request)
        return httpx.Response(200, json={"ok": True}, headers={"X-RateLimit-Remaining": "0"})

    client = bind_policy(database[1], GithubClient(transport=httpx.MockTransport(exhausted)))
    assert client.get("/repos/example/review") == {"ok": True}
    with pytest.raises(CollectionError, match="collection_cooldown"):
        client.get("/repos/example/review")
    assert len(calls) == 1
    client.close()


def test_hourly_reservations_are_atomic_and_reset(database):
    """병렬 프로세스의 요청 예약 합계가 자체 시간당 상한을 넘지 않는다."""
    now = [1700000000]
    policy = RequestPolicy(database[1], clock=lambda: now[0])

    def reserve(index):
        """예약 성공과 제한 결과를 병렬 검증용 숫자로 반환한다."""
        try:
            policy.reserve()
            return 1
        except CollectionError as error:
            assert error.code == "collection_cooldown"
            return 0

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(reserve, range(64))) == 60
    with database[1]() as session:
        assert session.get(CatalogRuntime, "github").state_json["total_requests"] == 60
    now[0] += 3601
    policy.reserve()
    with database[1]() as session:
        state = session.get(CatalogRuntime, "github").state_json
        assert state["window"]["count"] == 1 and state["total_requests"] == 61


def test_retry_header_dates_and_invalid_values():
    """HTTP 날짜와 잘못된 대기 안내를 안전한 기본값으로 처리한다."""
    now = 1700000000
    assert retry_epoch({"Retry-After": formatdate(now + 120, usegmt=True)}, now) == now + 120
    assert retry_epoch({"Retry-After": "bad", "X-RateLimit-Reset": "bad"}, now) == now + 60


@pytest.fixture
def refresh(database):
    """워커와 API가 같은 DB와 주입 시계를 사용하도록 구성한다."""
    now = [1700000000]
    remote = FakeGithub()

    def factory(**kwargs):
        """시계와 요청 정책을 공유하면서 외부 HTTP만 모의로 바꾼다."""
        return bind_policy(database[1], GithubClient(transport=httpx.MockTransport(remote.handle), **kwargs),
                           clock=lambda: now[0])

    app = FastAPI()
    app.include_router(refresh_router(database[1], lambda: "local", clock=lambda: now[0]))
    app.include_router(catalog_router(database[1], lambda: "local", client_factory=factory))
    with TestClient(app) as api:
        yield api, remote, now, factory, database[1]


def test_schedule_default_off_then_discovery_and_tracking(refresh):
    """예약은 기본 꺼져 있고 켠 뒤에도 명시적 tick 전에는 호출하지 않는다."""
    api, remote, now, factory, sessions = refresh
    assert api.get("/api/catalog/refresh").json()["schedule"]["enabled"] is False
    assert run_refresh_tick(sessions, clock=lambda: now[0], client_factory=factory)["status"] == "disabled"
    assert remote.requests == []
    assert api.put("/api/catalog/refresh/schedule", json={"enabled": True,
        "discovery_queries": ["review"]}).status_code == 200
    assert remote.requests == []
    first = run_refresh_tick(sessions, clock=lambda: now[0], client_factory=factory)
    assert first["mode"] == "discovery"
    assert api.get("/api/catalog/jobs/" + first["job_id"]).json()["status"] == "done"
    status = api.get("/api/catalog/refresh").json()
    assert status["total_requests"] == 8 and status["job_count"] == 1
    second = run_refresh_tick(sessions, clock=lambda: now[0], client_factory=factory)
    assert second["mode"] == "track"
    assert run_refresh_tick(sessions, clock=lambda: now[0], client_factory=factory)["status"] == "idle"
    count = len(remote.requests)
    assert api.put("/api/catalog/refresh/schedule", json={"enabled": False}).status_code == 200
    assert run_refresh_tick(sessions, clock=lambda: now[0], client_factory=factory)["status"] == "disabled"
    assert len(remote.requests) == count


def test_schedule_resumes_pending_before_other_modes(refresh):
    """미완료 배치를 우선 처리하며 작업 키와 원본 SHA를 보존한다."""
    api, remote, now, factory, sessions = refresh
    api.put("/api/catalog/refresh/schedule", json={"enabled": True, "discovery_queries": ["review"], "batch_size": 1})
    first = run_refresh_tick(sessions, clock=lambda: now[0], client_factory=factory)
    assert first["mode"] == "discovery"
    assert api.get("/api/catalog/jobs/" + first["job_id"]).json()["status"] == "partial"
    second = run_refresh_tick(sessions, clock=lambda: now[0], client_factory=factory)
    assert second["mode"] == "resume" and second["job_id"] != first["job_id"]
    result = api.get("/api/catalog/jobs/" + second["job_id"]).json()
    assert result["status"] == "done"
    assert result["result"]["items"][0]["commit_sha"] == "a" * 40


def test_rate_limit_defers_ticks_and_manual_retries_without_network(refresh):
    """제한 중 재시작 tick과 수동 재요청이 HTTP를 추가하지 않는다."""
    api, remote, now, factory, sessions = refresh
    remote.status = 429
    api.put("/api/catalog/refresh/schedule", json={"enabled": True, "discovery_queries": ["review"]})
    first = run_refresh_tick(sessions, clock=lambda: now[0], client_factory=factory)
    assert first["status"] == "submitted" and len(remote.requests) == 1
    assert run_refresh_tick(sessions, clock=lambda: now[0], client_factory=factory)["status"] == "waiting"
    response = api.post("/api/catalog/sync", json={"discovery_queries": ["review"]},
                        headers={"Idempotency-Key": "retry"})
    result = api.get("/api/catalog/jobs/" + response.json()["id"]).json()
    assert result["request_count"] == 0
    assert result["result"]["discovery_errors"][0]["error"] == "collection_cooldown"
    assert len(remote.requests) == 1


def test_parallel_ticks_do_not_duplicate_discovery(refresh):
    """중복 트리거가 동시에 와도 발견 작업을 두 번 제출하지 않는다."""
    api, remote, now, factory, sessions = refresh
    api.put("/api/catalog/refresh/schedule", json={"enabled": True, "discovery_queries": ["review"]})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run_refresh_tick(sessions, clock=lambda: now[0], client_factory=factory), range(2)))
    assert sum(result.get("mode") == "discovery" for result in results) == 1
    with sessions() as session:
        assert len(list(session.scalars(select(CatalogSyncJob)))) == 1


def test_refresh_auth_validation_and_worker_dry_run(database, capsys, monkeypatch):
    """운영 설정 인증과 주기 하한을 확인하고 기본 CLI는 DB도 읽지 않는다."""
    from backend.main import create_app
    from backend.catalog.refresh import main
    with TestClient(create_app(database_url=database[2].attributes["database_url"], local_token="test-token")) as api:
        assert api.get("/api/catalog/refresh").status_code == 401
        assert api.put("/api/catalog/refresh/schedule", json={"enabled": True}).status_code == 401
        assert api.put("/api/catalog/refresh/schedule", headers={"X-Local-Token": "test-token"},
                       json={"resume_interval_seconds": 1}).status_code == 422
    monkeypatch.setattr("sys.argv", ["refresh"])
    main()
    assert '"network_executed": false' in capsys.readouterr().out
