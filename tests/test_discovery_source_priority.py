"""공개 검색의 약한 설명으로 읽을 저장소를 고르되 추천 근거로 승격하지 않는다."""
import httpx
import pytest
from sqlalchemy import select

from backend.catalog.config import CollectionConfig
from backend.catalog.github_client import CollectionError, GithubClient
from backend.catalog.service import CatalogCollector
from backend.models import AssetVersion, Source
from test_catalog import FakeGithub


def repository(name, description, **values):
    """공개 저장소 검색 응답에 필요한 최소한의 필드만 만든다."""
    return {"full_name": name, "private": False, "archived": False,
        "description": description, "topics": ["agent-skills"], **values}


def discover(items, *, hints=None, limit=1, incomplete=False):
    """후보 풀을 실제 HTTP 매개변수로 제한하고 외부 통신 없이 선택 결과를 읽는다."""
    requests = []

    def remote(request):
        """요청한 페이지 크기 안에서만 검색 후보를 반환한다."""
        requests.append(request)
        count = int(request.url.params["per_page"])
        return httpx.Response(200, json={"incomplete_results": incomplete, "items": items[:count]})

    client = GithubClient(transport=httpx.MockTransport(remote), max_requests=1)
    try:
        result = client.discover("skills topic:agent-skills", limit, sort="best_match", hints=hints)
        return [item.repository for item in result], requests
    finally:
        client.close()


def test_security_work_prefers_audit_source_over_generic_inventory_and_web_app():
    """작업 검사와 관련된 설명을 가진 저장소를 일반 모음이나 React 앱보다 먼저 읽는다."""
    items = [repository("generic/skills", "Public repository for Agent Skills"),
        repository("new/game", "A React game with animations"),
        repository("research/skills", "Skills for security research, vulnerability detection, and audit workflows")]
    result, requests = discover(items, hints=["python", "fastapi", "code_review", "api_security"])
    assert result == ["research/skills"]
    assert requests[0].url.params["per_page"] == "5"
    assert requests[0].url.params["q"] == "skills topic:agent-skills is:public"
    assert "hints" not in requests[0].url.params


def test_same_public_pool_yields_different_sources_for_different_work():
    """언어 단어만 같은 앱 대신 해당 사용자의 테스트나 리뷰 목적에 맞는 출처를 고른다."""
    items = [repository("generic/skills", "General agent skills"),
        repository("new/game", "React game UI"),
        repository("frontend/testing-skills", "React testing with Playwright browser automation and coverage"),
        repository("backend/review-skills", "Python code review and input validation"),
        repository("security/audit-skills", "Security audit workflows")]
    frontend, _ = discover(items, hints=["react", "typescript", "test_generation", "browser_testing", "test_coverage"])
    backend, _ = discover(items, hints=["python", "code_review", "input_validation"])
    assert frontend == ["frontend/testing-skills"]
    assert backend == ["backend/review-skills"]


def test_equal_weak_relevance_preserves_provider_order_instead_of_stars():
    """같은 단서는 검색 제공자의 순서를 보존하고 인기 숫자를 추천 적합성으로 쓰지 않는다."""
    items = [repository("z-first/skills", "Security audit skills", stargazers_count=1),
        repository("a-second/skills", "Security audit skills", stargazers_count=999999)]
    result, _ = discover(items, hints=["api_security", "code_review"], limit=2)
    assert result == ["z-first/skills", "a-second/skills"]


def test_without_hints_preserves_requested_page_size_and_provider_first():
    """관심 맥락이 없으면 기존 검색 예산과 반환 순서를 유지한다."""
    items = [repository("generic/skills", "Public agent skills"),
        repository("security/skills", "Security audit skills")]
    result, requests = discover(items)
    assert result == ["generic/skills"] and requests[0].url.params["per_page"] == "1"


@pytest.mark.parametrize("limit,expected", [(1, 5), (2, 10), (4, 20), (5, 20), (20, 20)])
def test_metadata_pool_stays_bounded_to_twenty(limit, expected):
    """후보 풀을 넓혀도 한 검색에서 최대 스무 개의 메타데이터만 읽는다."""
    _, requests = discover([], hints=["code_review"], limit=limit)
    assert requests[0].url.params["per_page"] == str(expected)


@pytest.mark.parametrize("hints", [["private project"], ["../../secret"], ["Python"], "api_security", [1]])
def test_direct_client_rejects_unknown_hints_before_network(hints):
    """설정 모델을 거치지 않은 직접 호출도 자유 문장과 경로를 힌트로 받지 않는다."""
    requests = []

    def remote(request):
        """잘못된 입력이면 도달하면 안 되는 전송 지점을 관찰한다."""
        requests.append(request)
        return httpx.Response(200, json={"incomplete_results": False, "items": []})

    client = GithubClient(transport=httpx.MockTransport(remote), max_requests=1)
    try:
        with pytest.raises(CollectionError, match="invalid_search"):
            client.discover("skills", 1, hints=hints)
    finally:
        client.close()
    assert requests == []


def test_partial_search_is_not_ranked_as_a_complete_result():
    """후보 풀이 불완전하면 약한 관련성 정렬로 성공을 꾸미지 않는다."""
    with pytest.raises(CollectionError, match="incomplete_search"):
        discover([repository("security/skills", "Security audit")], hints=["api_security"], incomplete=True)


def test_collector_selects_context_source_without_turning_metadata_into_candidate_tags(database):
    """실제 수집이 선택한 출처만 읽으며 검색 설명의 기능을 후보 원문 태그로 복사하지 않는다."""
    remote = FakeGithub()
    original = remote.handle

    def with_ranked_sources(request):
        """첫 일반 저장소 뒤에 적합한 보안 출처가 있는 검색 응답을 만든다."""
        if request.url.path == "/search/repositories":
            remote.requests.append(request)
            return httpx.Response(200, json={"incomplete_results": False, "items": [
                repository("generic/skills", "Agent skills"),
                repository("example/review", "Security audit workflows")]})
        return original(request)

    client = GithubClient(transport=httpx.MockTransport(with_ranked_sources), max_requests=10)
    try:
        result = CatalogCollector(database[1], client).run(CollectionConfig(discovery_queries=["skills"],
            discovery_limit=1, discovery_hints=["api_security", "code_review"]))
    finally:
        client.close()
    assert result["discovery"][0]["repositories"] == ["example/review"]
    assert result["items"][0]["repository"] == "example/review"
    with database[1]() as session:
        assert list(session.scalars(select(Source.id))) == ["github:example/review"]
        assert all("api_security" not in version.tags["capabilities"] for version in session.scalars(select(AssetVersion)))
