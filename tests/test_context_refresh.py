"""옵트인 개인화 예약의 검색 순환과 기존 추적 우선순위를 검증한다."""
import copy
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.catalog.github_client import GithubClient
from backend.catalog.discovery import DiscoveryQuery
from backend.catalog.refresh import RefreshSchedule, contextual_queries, refresh_router, run_refresh_tick
from backend.models import CatalogRuntime, CatalogSyncJob, Profile, Source
from test_catalog import FakeGithub


@pytest.fixture
def contextual_refresh(database):
    """서버 시계와 공개 검색 응답을 주입하고 모든 원격 호출을 차단한다."""
    now = [1794700800]
    requests = []

    def empty_search(request):
        """쿼리를 관찰하되 저장소를 발견하지 않아 다음 tick도 발견만 검증한다."""
        requests.append(request)
        assert request.url.path == "/search/repositories"
        return httpx.Response(200, json={"incomplete_results": False, "items": []})

    def factory(**kwargs):
        """기존 예산을 보존한 채 실제 네트워크를 모의 전송으로 교체한다."""
        return GithubClient(transport=httpx.MockTransport(empty_search), **kwargs)

    app = FastAPI()
    app.include_router(refresh_router(database[1], lambda: "local", clock=lambda: now[0]))
    with TestClient(app) as api:
        yield api, database[1], now, requests, factory


def add_profiles(sessions, count=2, *, diverse=True):
    """목표와 기술 환경이 다른 사용자 또는 동일 환경의 다수 사용자를 등록한다."""
    with sessions.begin() as session:
        for index in range(count):
            frontend = diverse and index % 2
            session.add(Profile(id=f"profile_{index:02}", role="PRIVATE_ROLE",
                primary_language="typescript" if frontend else "python",
                task_types=["test_generation" if frontend else "code_review"],
                goals=["test_coverage" if frontend else "input_validation"],
                context_json={"frameworks": ["react" if frontend else "fastapi"]}))


def state(sessions):
    """현재 커서와 기한을 검증할 수 있도록 JSON 값을 복사한다."""
    with sessions() as session:
        return copy.deepcopy(session.get(CatalogRuntime, "github").state_json)


@pytest.mark.parametrize("query_count", [4, 5])
def test_full_query_pool_rotates_without_truncating_later_purposes(monkeypatch, query_count):
    """열 명의 계획이 서른 검색어를 넘어도 뒤의 목적 검색까지 모든 회차에 배정한다."""
    from backend.catalog import refresh
    profile_ids = [f"profile_{index:02}" for index in range(10)]
    plans = {profile_id: SimpleNamespace(profile_id=profile_id, context_fingerprint="fingerprint",
        folder_hints=["code_review"], queries=[DiscoveryQuery(query=f"public_{index}_{purpose}",
            reason="공개 작업 목적", scope="evergreen", sort="best_match")
            for purpose in range(query_count)]) for index, profile_id in enumerate(profile_ids)}
    monkeypatch.setattr(refresh, "load_plan", lambda session, profile_id, **kwargs: plans[profile_id])
    session = SimpleNamespace(scalars=lambda statement: profile_ids)
    runtime = {"contextual_cursor": {"profile_after": "", "batch_profile_ids": profile_ids, "query_offset": 0}}
    selected = []
    for _ in range(query_count * 2):
        queries, _, cursor = contextual_queries(session, runtime, RefreshSchedule(contextual_discovery=True), 1794700800)
        assert len(queries) == 5
        selected.extend(queries)
        runtime["contextual_cursor"] = cursor
    assert len(selected) == len(set(selected)) == 10 * query_count
    assert set(selected) == {item.query for plan in plans.values() for item in plan.queries}
    assert runtime["contextual_cursor"] == {
        "profile_after": "profile_09", "batch_profile_ids": [], "query_offset": 0}


def test_contextual_schedule_defaults_off_and_enabling_does_not_start_network(contextual_refresh):
    """개인화와 예약은 모두 기본 비활성이며 설정 저장만으로 요청을 보내지 않는다."""
    api, sessions, now, requests, factory = contextual_refresh
    add_profiles(sessions)
    schedule = api.get("/api/catalog/refresh").json()["schedule"]
    assert schedule["enabled"] is False and schedule["contextual_discovery"] is False
    assert run_refresh_tick(sessions, clock=lambda: now[0], client_factory=factory)["status"] == "disabled"
    api.put("/api/catalog/refresh/schedule", json={"enabled": True})
    before = state(sessions)["contextual_cursor"]
    assert run_refresh_tick(sessions, clock=lambda: now[0], client_factory=factory)["status"] == "idle"
    assert state(sessions)["contextual_cursor"] == before
    assert requests == []
    response = api.put("/api/catalog/refresh/schedule", json={"enabled": True, "contextual_discovery": True})
    assert response.status_code == 200 and response.json()["contextual_discovery"] is True
    assert requests == []


def test_contextual_queries_rotate_recent_evergreen_and_goal_across_ticks(contextual_refresh):
    """첫 다섯 개 뒤의 기능 검색도 다음 회차에 실행하고 프로필별 포함 여부를 남긴다."""
    api, sessions, now, requests, factory = contextual_refresh
    add_profiles(sessions, count=3)
    with sessions.begin() as session:
        profile = session.get(Profile, "profile_02")
        profile.context_json = {"frameworks": ["django"]}
        profile.goals = ["regression_test"]
    api.put("/api/catalog/refresh/schedule", json={"enabled": True, "contextual_discovery": True,
        "discovery_queries": ["public static skill"]})
    first = run_refresh_tick(sessions, clock=lambda: now[0], client_factory=factory)
    assert first["mode"] == "discovery"
    first_state = state(sessions)
    assert first_state["contextual_cursor"]["query_offset"] == 5
    assert first_state["contextual_cursor"]["batch_profile_ids"] == ["profile_00", "profile_01", "profile_02"]
    first_queries = [request.url.params["q"] for request in requests]
    assert len(first_queries) == 5
    assert any("fastapi" in query and "pushed:" in query for query in first_queries)
    assert any("react" in query and "pushed:" in query for query in first_queries)
    assert any('"code review"' in query and "pushed:" not in query for query in first_queries)
    assert any('"test-driven"' in query and "pushed:" not in query for query in first_queries)
    assert not any(query.startswith("skills in:name ") for query in first_queries)
    with sessions() as session:
        job = session.get(CatalogSyncJob, first["job_id"])
        assert job.status == "done" and all(value["covered"] for value in job.context_snapshot.values())
        assert all(value["fingerprint"] for value in job.context_snapshot.values())
        assert {"fastapi", "react", "django", "input_validation", "test_coverage", "regression_test"} <= set(
            job.config_json["discovery_hints"])
    count = len(requests)
    assert run_refresh_tick(sessions, clock=lambda: now[0], client_factory=factory)["status"] == "idle"
    assert len(requests) == count and state(sessions)["contextual_cursor"] == first_state["contextual_cursor"]
    now[0] += 86401
    second = run_refresh_tick(sessions, clock=lambda: now[0], client_factory=factory)
    assert second["mode"] == "discovery"
    second_queries = [request.url.params["q"] for request in requests[count:]]
    assert any("coverage" in query for query in second_queries)
    assert any('"input validation"' in query for query in second_queries)
    assert any(query.startswith("skills in:name ") for query in second_queries)
    assert any("public static skill" in query for query in second_queries)
    assert any("regression" in query for query in second_queries)
    assert len(second_queries) == 5
    assert state(sessions)["contextual_cursor"] == {
        "profile_after": "profile_02", "batch_profile_ids": [], "query_offset": 0}
    with sessions() as session:
        snapshot = session.get(CatalogSyncJob, second["job_id"]).context_snapshot
        assert snapshot["profile_00"]["covered"] is True
        assert snapshot["profile_01"]["covered"] is True
    assert "PRIVATE_ROLE" not in str([request.url for request in requests])


def test_profile_batches_advance_and_wrap_without_starving_later_users(contextual_refresh):
    """열 명 뒤의 사용자도 다음 회차를 배정받고 끝에서는 첫 사용자로 순환한다."""
    api, sessions, now, _, factory = contextual_refresh
    add_profiles(sessions, count=12, diverse=False)
    api.put("/api/catalog/refresh/schedule", json={"enabled": True, "contextual_discovery": True})
    seen = []
    for _ in range(3):
        result = run_refresh_tick(sessions, clock=lambda: now[0], client_factory=factory)
        with sessions() as session:
            seen.append(set(session.get(CatalogSyncJob, result["job_id"]).context_snapshot))
        now[0] += 86401
    assert seen[0] == {f"profile_{index:02}" for index in range(10)}
    assert seen[1] == {"profile_10", "profile_11"}
    assert seen[2] == seen[0]


def test_existing_source_tracking_precedes_context_discovery(contextual_refresh):
    """기존 저장소 후속 추적을 먼저 실행하고 개인화 검색 위치는 그대로 둔다."""
    api, sessions, now, _, _ = contextual_refresh
    add_profiles(sessions)
    with sessions.begin() as session:
        session.add(Source(id="github:example/review", kind="github_repo", url="https://github.com/example/review",
            sync_state={"batch": {"next": 1, "folders": [], "scope": []}}))
    api.put("/api/catalog/refresh/schedule", json={"enabled": True, "contextual_discovery": True})
    before = state(sessions)["contextual_cursor"]
    remote = FakeGithub()

    def factory(**kwargs):
        """기존 수집 회차의 실제 요청 흐름을 모의 GitHub로 재현한다."""
        return GithubClient(transport=httpx.MockTransport(remote.handle), **kwargs)

    result = run_refresh_tick(sessions, clock=lambda: now[0], client_factory=factory)
    assert result["mode"] == "track"
    assert state(sessions)["contextual_cursor"] == before
    assert all(request.url.path != "/search/repositories" for request in remote.requests)
    with sessions() as session:
        assert session.get(CatalogSyncJob, result["job_id"]).context_snapshot == {}


def test_job_cursor_and_deadline_roll_back_together_on_submit_failure(contextual_refresh, monkeypatch):
    """제출 트랜잭션이 실패하면 검색 커서와 다음 기한 및 작업도 함께 되돌린다."""
    from backend.catalog import refresh
    api, sessions, now, requests, factory = contextual_refresh
    add_profiles(sessions)
    api.put("/api/catalog/refresh/schedule", json={"enabled": True, "contextual_discovery": True})
    before = state(sessions)
    original = refresh.enqueue_job

    def failing(*args, **kwargs):
        """작업 저장 직후 오류를 발생시켜 원자적 롤백을 검증한다."""
        original(*args, **kwargs)
        raise RuntimeError("submit failed")

    monkeypatch.setattr(refresh, "enqueue_job", failing)
    with pytest.raises(RuntimeError, match="submit failed"):
        run_refresh_tick(sessions, clock=lambda: now[0], client_factory=factory)
    assert state(sessions) == before and requests == []
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(CatalogSyncJob)) == 0
    monkeypatch.setattr(refresh, "enqueue_job", original)
    result = run_refresh_tick(sessions, clock=lambda: now[0], client_factory=factory)
    assert result["mode"] == "discovery" and state(sessions)["contextual_cursor"]["query_offset"] == 5
