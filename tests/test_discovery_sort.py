"""최근 변화 검색과 공통 스킬 모음 검색의 정렬을 목적에 맞게 분리한다."""
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from backend.catalog.config import CollectionConfig
from backend.catalog.discovery import DiscoveryQuery, shared_query_sorts
from backend.catalog.github_client import CollectionError, GithubClient
from backend.catalog.refresh import run_refresh_tick
from backend.catalog.service import CatalogCollector
from backend.models import CatalogSyncJob
from test_context_discovery import discovery_api
from test_context_refresh import add_profiles, contextual_refresh


def test_discovery_sort_configuration_only_allows_planned_queries():
    """검색어 밖 설정과 인기순 정렬을 거부하고 과거 설정은 그대로 읽는다."""
    assert CollectionConfig(discovery_queries=["skill"]).discovery_sort == {}
    config = CollectionConfig(discovery_queries=["skill"], discovery_sort={"skill": "best_match"})
    assert config.discovery_sort == {"skill": "best_match"}
    for sorts in ({"unplanned": "best_match"}, {"skill": "stars"}):
        with pytest.raises(ValidationError):
            CollectionConfig(discovery_queries=["skill"], discovery_sort=sorts)


def test_best_match_omits_remote_sort_and_default_preserves_updated():
    """관련성 정렬은 GitHub 기본 검색을 쓰고 최근 검색은 기존 갱신순을 유지한다."""
    requests = []

    def empty(request):
        """원격 통신 없이 실제 생성한 HTTP 매개변수만 관찰한다."""
        requests.append(request)
        return httpx.Response(200, json={"incomplete_results": False, "items": []})

    client = GithubClient(transport=httpx.MockTransport(empty), max_requests=2)
    try:
        assert client.discover("inventory", 1, sort="best_match") == []
        assert client.discover("recent", 1) == []
        with pytest.raises(CollectionError, match="invalid_search"):
            client.discover("unwanted", 1, sort="stars")
    finally:
        client.close()
    assert len(requests) == 2
    assert "sort" not in requests[0].url.params and "order" not in requests[0].url.params
    assert requests[1].url.params["sort"] == "updated" and requests[1].url.params["order"] == "desc"
    assert requests[0].url.params["q"] == "inventory is:public"


def test_quota_observation_only_keeps_bounded_numeric_headers():
    """요청량 관찰은 허용된 숫자 헤더만 남기며 임의 헤더와 비밀값은 저장하지 않는다."""
    def remote(request):
        """정상 숫자와 잘못된 제한 값 및 무관한 헤더를 함께 반환한다."""
        return httpx.Response(200, json={}, headers={"X-RateLimit-Limit": "60",
            "X-RateLimit-Remaining": "17", "X-RateLimit-Used": "43", "X-RateLimit-Reset": "invalid",
            "Authorization": "SECRET", "X-Private": "SECRET"})

    client = GithubClient(transport=httpx.MockTransport(remote), max_requests=1)
    try:
        client.get("/repos/example/review")
        assert client.last_quota == {"limit": 60, "remaining": 17, "used": 43}
    finally:
        client.close()


def test_duplicate_query_sort_conflict_uses_best_match_consistently():
    """동일한 공통 검색이 여러 계획에 있으면 프로필 순서와 무관하게 같은 정렬을 쓴다."""
    recent = SimpleNamespace(queries=[DiscoveryQuery(query="shared", reason="recent", scope="recent")])
    evergreen = SimpleNamespace(queries=[DiscoveryQuery(query="shared", reason="inventory",
        scope="evergreen", sort="best_match")])
    assert shared_query_sorts([recent, evergreen], ["shared", "static"]) == {
        "shared": "best_match", "static": "updated"}
    assert shared_query_sorts([evergreen, recent], ["shared"]) == {"shared": "best_match"}


def test_collector_passes_each_query_sort_to_client(database):
    """영속 수집기가 정렬 설정을 실제 HTTP 검색에 전달하는지 확인한다."""
    requests = []

    def empty(request):
        """두 검색의 빈 정상 결과를 반환한다."""
        requests.append(request)
        return httpx.Response(200, json={"incomplete_results": False, "items": []})

    client = GithubClient(transport=httpx.MockTransport(empty), max_requests=2)
    try:
        result = CatalogCollector(database[1], client).run(CollectionConfig(
            discovery_queries=["inventory", "recent"], discovery_sort={"inventory": "best_match"}))
    finally:
        client.close()
    assert [item["status"] for item in result["discovery"]] == ["done", "done"]
    assert "sort" not in requests[0].url.params
    assert requests[1].url.params["sort"] == "updated"


def test_profile_sync_persists_and_executes_recent_and_inventory_sorts(discovery_api):
    """개인화 API가 공개 계획과 일치하는 정렬을 작업 및 맥락 기록에 저장한다."""
    api, sessions, remote = discovery_api
    plan = api.get("/api/catalog/discovery-plan", headers={"X-Test-User": "alice"}).json()
    assert plan["queries"][0]["sort"] == "updated"
    assert all(item["sort"] == "best_match" for item in plan["queries"] if item["scope"] == "evergreen")
    response = api.post("/api/catalog/discovery-sync", json={"profile_ids": ["alice", "bob"],
        "max_requests": 20, "discovery_limit": 1},
        headers={"X-Test-Operator": "operator", "Idempotency-Key": "sort-contract"})
    assert response.status_code == 202
    with sessions() as session:
        job = session.get(CatalogSyncJob, response.json()["id"])
        sorts = job.config_json["discovery_sort"]
        assert len(sorts) <= 5 and set(sorts.values()) == {"updated", "best_match"}
        assert all(value["query_sorts"] for value in job.context_snapshot.values())
    searches = [request for request in remote.requests if request.url.path == "/search/repositories"]
    assert len(searches) == len(sorts)
    for request in searches:
        query = request.url.params["q"].removesuffix(" is:public")
        assert ("sort" not in request.url.params) == (sorts[query] == "best_match")


def test_refresh_preserves_plan_sorts_and_static_default(contextual_refresh):
    """예약 회차가 개인화 검색 정렬을 보존하고 수동 검색어는 기본 갱신순으로 실행한다."""
    api, sessions, now, requests, factory = contextual_refresh
    add_profiles(sessions)
    api.put("/api/catalog/refresh/schedule", json={"enabled": True, "contextual_discovery": True,
        "discovery_queries": ["static skill"]})
    first = run_refresh_tick(sessions, clock=lambda: now[0], client_factory=factory)
    with sessions() as session:
        job = session.get(CatalogSyncJob, first["job_id"])
        sorts = job.config_json["discovery_sort"]
        assert set(sorts.values()) == {"best_match", "updated"}
        assert any("best_match" in value["query_sorts"].values() for value in job.context_snapshot.values())
    now[0] += 86401
    second = run_refresh_tick(sessions, clock=lambda: now[0], client_factory=factory)
    with sessions() as session:
        job = session.get(CatalogSyncJob, second["job_id"])
        sorts = job.config_json["discovery_sort"]
        assert sorts["static skill"] == "updated" and len(sorts) == 3
        assert all(sort == "best_match" for query, sort in sorts.items() if query != "static skill")
    assert requests[-1].url.params["q"] == "static skill is:public"
    assert requests[-1].url.params["sort"] == "updated"
