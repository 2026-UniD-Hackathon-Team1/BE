"""새 후보 발견은 기존 출처를 건너뛰고 후속 추적과 재개는 별도로 유지한다."""
import httpx
import pytest
from sqlalchemy import select

from backend.catalog.config import CollectionConfig, RepositoryConfig
from backend.catalog.github_client import CollectionError, GithubClient
from backend.catalog.schemas import CollectionResult
from backend.catalog.service import CatalogCollector
from backend.models import AssetVersion, Source
from test_catalog import FakeGithub
from test_catalog_batching import PinnedGithub


def item(name):
    """이름 이외의 적합성이 같은 공개 검색 후보를 만든다."""
    return {"full_name": name, "private": False, "archived": False,
        "description": "Code review and security audit workflows", "topics": ["agent-skills"]}


def test_client_skips_case_normalized_known_repo_without_extra_search():
    """최상단 기존 출처를 제외하고 같은 응답의 다음 후보를 한 번의 요청으로 선택한다."""
    requests = []

    def remote(request):
        """이미 알려진 출처를 대소문자가 다른 이름으로 반환한다."""
        requests.append(request)
        return httpx.Response(200, json={"incomplete_results": False,
            "items": [item("KNOWN/Skills"), item("new/skills"), item("third/skills")]})

    client = GithubClient(transport=httpx.MockTransport(remote), max_requests=1)
    try:
        found = client.discover("skills", 1, hints=["code_review"], exclude_repositories={"known/skills"})
        assert [entry.repository for entry in found] == ["new/skills"]
        assert client.last_discovery_stats == {"source_pool_count": 3, "known_skipped": 1, "selected_count": 1}
    finally:
        client.close()
    assert len(requests) == 1 and requests[0].url.params["per_page"] == "5"
    assert requests[0].url.params["q"] == "skills is:public" and "page" not in requests[0].url.params
    assert "exclude_repositories" not in requests[0].url.params


def test_full_known_page_is_normal_empty_and_does_not_paginate():
    """스무 개가 모두 기존 출처이면 새 후보 없음으로 종료하고 추가 페이지를 읽지 않는다."""
    repositories = [f"known/skills{index}" for index in range(20)]
    requests = []

    def remote(request):
        """현재 페이지의 모든 후보가 이미 알려진 정상 검색 결과를 반환한다."""
        requests.append(request)
        return httpx.Response(200, json={"incomplete_results": False, "items": [item(name) for name in repositories]})

    client = GithubClient(transport=httpx.MockTransport(remote), max_requests=1)
    try:
        assert client.discover("skills", 4, hints=["code_review"], exclude_repositories=repositories) == []
        assert client.last_discovery_stats == {"source_pool_count": 20, "known_skipped": 20, "selected_count": 0}
    finally:
        client.close()
    assert len(requests) == 1 and requests[0].url.params["per_page"] == "20"


@pytest.mark.parametrize("excluded", ["owner/repo", [None], ["github:owner/repo"], ["https://github.com/owner/repo"], [1]])
def test_bad_direct_exclusions_are_rejected_before_network(excluded):
    """직접 호출에서도 저장소 식별자가 아닌 값은 원격 요청 전에 거부한다."""
    requests = []

    def remote(request):
        """잘못된 제외 설정이면 도달하지 않아야 하는 전송 지점을 기록한다."""
        requests.append(request)
        return httpx.Response(200, json={"incomplete_results": False, "items": []})

    client = GithubClient(transport=httpx.MockTransport(remote))
    try:
        with pytest.raises(CollectionError, match="invalid_search"):
            client.discover("skills", 1, exclude_repositories=excluded)
    finally:
        client.close()
    assert requests == []


def multi_source_remote(names):
    """공개 검색 결과만 확장하고 고정 원문 검증은 기존 모의 GitHub를 사용한다."""
    remote = FakeGithub()
    original = remote.handle

    def handle(request):
        """페이지 크기를 보존해 같은 후보 풀을 검색마다 반환한다."""
        if request.url.path == "/search/repositories":
            remote.requests.append(request)
            count = int(request.url.params["per_page"])
            return httpx.Response(200, json={"incomplete_results": False, "items": [item(name) for name in names[:count]]})
        return original(request)

    return remote, handle


def test_repeating_discovery_selects_next_new_source_without_recollecting_known(database):
    """같은 검색의 다음 회차가 기존 첫 저장소를 재수집하지 않고 다음 새 출처를 발견한다."""
    remote, handle = multi_source_remote(["example/a", "example/b", "example/c"])
    config = CollectionConfig(discovery_queries=["skills"], discovery_limit=1, discovery_hints=["code_review"])
    for expected in ("example/a", "example/b"):
        remote.requests.clear()
        client = GithubClient(transport=httpx.MockTransport(handle), max_requests=10)
        try:
            result = CatalogCollector(database[1], client).run(config)
        finally:
            client.close()
        assert result["discovery"][0]["repositories"] == [expected]
        assert result["items"][0]["repository"] == expected and result["items"][0]["status"] == "done"
        searches = [request for request in remote.requests if request.url.path == "/search/repositories"]
        assert len(searches) == 1
        if expected == "example/b":
            assert result["discovery"][0]["known_repositories_skipped"] == 1
            assert not any(request.url.path.startswith("/repos/example/a") for request in remote.requests)
    with database[1]() as session:
        assert set(session.scalars(select(Source.id))) == {"github:example/a", "github:example/b"}


def test_queries_in_one_run_do_not_reselect_already_discovered_source(database):
    """한 작업의 여러 검색어도 직전에 발견한 출처를 다시 선택해 발견 예산을 소모하지 않는다."""
    remote, handle = multi_source_remote(["example/a", "example/b", "example/c"])
    client = GithubClient(transport=httpx.MockTransport(handle), max_requests=30)
    try:
        result = CatalogCollector(database[1], client).run(CollectionConfig(discovery_queries=["one", "two", "three"],
            discovery_limit=1, discovery_hints=["code_review"]))
    finally:
        client.close()
    assert [row["repositories"] for row in result["discovery"]] == [["example/a"], ["example/b"], ["example/c"]]
    assert [row["known_repositories_skipped"] for row in result["discovery"]] == [0, 1, 2]
    assert len([request for request in remote.requests if request.url.path == "/search/repositories"]) == 3


def test_disabled_and_active_known_sources_are_not_new_discoveries(database):
    """비활성 출처를 검색이 다시 살리지 않고 기존 출처뿐인 결과를 빈 정상 검색과 구분한다."""
    with database[1].begin() as session:
        session.add_all([Source(id="github:example/disabled", kind="github_repo", url="https://github.com/example/disabled",
            enabled=False, sync_state={}), Source(id="github:example/active", kind="github_repo",
            url="https://github.com/example/active", enabled=True, sync_state={})])
    remote, handle = multi_source_remote(["example/disabled", "example/active"])
    client = GithubClient(transport=httpx.MockTransport(handle), max_requests=1)
    try:
        result = CatalogCollector(database[1], client).run(CollectionConfig(discovery_queries=["skills"],
            discovery_limit=1, discovery_hints=["code_review"]))
    finally:
        client.close()
    assert result["items"] == [] and result["discovery_errors"] == [] and len(remote.requests) == 1
    discovery = CollectionResult.model_validate(result).discovery[0]
    assert discovery.status == "done" and discovery.repositories == [] and discovery.known_repositories_skipped == 2
    with database[1]() as session:
        assert session.get(Source, "github:example/disabled").enabled is False
    assert CollectionResult.model_validate({"discovery": [{"query": "legacy", "status": "done"}]}).discovery[
        0].known_repositories_skipped == 0


def test_known_filter_does_not_change_pending_pinned_commit_or_explicit_tracking(database):
    """새 발견에서 제외된 기존 저장소도 고정 커밋 재개와 명시적 후속 추적은 계속 지원한다."""
    remote = PinnedGithub(skills=2)
    repository = RepositoryConfig(repository="example/review")

    def run(config):
        """각 회차는 새 클라이언트로 같은 DB의 진행 상태를 이어받는다."""
        remote.requests.clear()
        client = GithubClient(transport=httpx.MockTransport(remote.handle), max_requests=20)
        try:
            return CatalogCollector(database[1], client).run(config)
        finally:
            client.close()

    first = run(CollectionConfig(repositories=[repository], batch_size=1))
    assert first["items"][0]["status"] == "partial"
    remote.sha = "c" * 40
    remote.history[remote.sha] = {**remote.files, "skills/s000/SKILL.md": b"Code review. Updated."}
    resumed = run(CollectionConfig(resume_pending=True, discovery_queries=["skills"], discovery_hints=["code_review"]))
    assert resumed["discovery"][0]["repositories"] == [] and resumed["discovery"][0]["known_repositories_skipped"] == 1
    assert resumed["items"][0]["status"] == "done" and resumed["items"][0]["commit_sha"] == "a" * 40
    with database[1]() as session:
        assert set(session.scalars(select(AssetVersion.revision))) == {"a" * 40}
    tracked = run(CollectionConfig(repositories=[repository], discovery_queries=["skills"]))
    assert tracked["items"][0]["commit_sha"] == "c" * 40 and tracked["items"][0]["new_versions"] == 1
    unchanged = run(CollectionConfig(repositories=[repository]))
    assert unchanged["items"][0]["status"] == "done" and unchanged["items"][0]["new_versions"] == 0
