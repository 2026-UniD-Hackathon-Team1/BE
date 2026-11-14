"""큰 번들을 보류해도 고정 회차의 작은 번들을 완전하게 수집할 수 있는지 검증한다."""
import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from backend.catalog.config import CollectionConfig, RepositoryConfig
from backend.catalog.github_client import GithubClient
from backend.catalog.policy import bind_policy
from backend.catalog.schemas import RepositoryProgress
from backend.catalog.service import CatalogCollector, completed_indices
from backend.models import Asset, AssetVersion, ChangeEvent, Source
from test_catalog_batching import PinnedGithub


@pytest.fixture
def budget_fits(database):
    """첫 스킬은 여섯 요청이고 뒤의 스킬은 각각 한 요청인 고정 원본을 만든다."""
    remote = PinnedGithub(skills=0)
    remote.files = {"skills/a-large/SKILL.md": b"Code review provides input validation.",
        **{f"skills/a-large/ref{index}.txt": b"Reference" for index in range(5)},
        "skills/b-small/SKILL.md": b"Code review provides input validation.",
        "skills/c-small/SKILL.md": b"Code review provides exception handling.", "README.md": b"Skills"}
    remote.history = {remote.sha: dict(remote.files)}

    def collect(*, budget=12, size=5, previous_requests=0):
        """같은 요청 예산에서 검색 등 선행 요청까지 포함해 수집기를 실행한다."""
        remote.requests.clear()
        client = GithubClient(transport=httpx.MockTransport(remote.handle), max_requests=budget)
        bind_policy(database[1], client, clock=lambda: remote.clock)
        try:
            for _ in range(previous_requests):
                client.get("/repos/example/other")
            result = CatalogCollector(database[1], client).collect(
                RepositoryConfig(repository="example/review"), batch_size=size)
            return result, client.request_count
        finally:
            client.close()

    return remote, database[1], collect


def saved_batch(sessions):
    """원본 소스의 고정 목록과 완료 위치를 독립 사전으로 읽는다."""
    with sessions() as session:
        return dict(session.get(Source, "github:example/review").sync_state["batch"])


def test_later_small_bundles_fit_remaining_budget_without_reading_large_prefix(budget_fits):
    """앞 번들 전체가 남은 예산보다 크면 뒤의 작은 번들을 읽고 큰 번들의 원문은 요청하지 않는다."""
    remote, sessions, collect = budget_fits
    result, count = collect(budget=9, previous_requests=2)
    assert result["status"] == "partial" and result["completed_units"] == result["new_versions"] == 2
    assert result["resume_required"] is True and count == len(remote.requests) == 8
    assert result["deferred_units"] == [{"folder": "skills/a-large", "required_requests": 6,
        "reason": "request_budget_insufficient"}]
    assert not any("a-large/" in request.url.path or "/contents/README.md" in request.url.path for request in remote.requests)
    state = saved_batch(sessions)
    assert state["next"] == 0 and state["completed_indices"] == [1, 2]
    assert state["folders"] == ["skills/a-large", "skills/b-small", "skills/c-small"]
    with sessions() as session:
        assert session.get(Source, "github:example/review").last_success_at is None
        assert set(session.scalars(select(Asset.kind))) == {"skill"}
    assert RepositoryProgress.model_validate(result).deferred_units[0].required_requests == 6


def test_larger_next_budget_fills_hole_on_same_commit_then_reads_readme(budget_fits):
    """다음 요청에서는 보류한 번들을 고정 SHA로 완성하고 이미 저장한 작은 번들은 다시 읽지 않는다."""
    remote, sessions, collect = budget_fits
    collect(budget=9, previous_requests=2)
    original = saved_batch(sessions)
    remote.sha = "f" * 40
    remote.history[remote.sha] = {**remote.files, "skills/a-large/ref0.txt": b"New head must not mix"}
    result, count = collect(budget=12)
    assert result["status"] == "done" and result["completed_units"] == 4 and result["new_versions"] == 2
    assert result["commit_sha"] == "a" * 40 and count == 9
    state = saved_batch(sessions)
    assert state["folders"] == original["folders"] and state["generation"] == original["generation"]
    assert state["completed_indices"] == [0, 1, 2, 3] and state["next"] == 4
    contents = [request for request in remote.requests if "/contents/" in request.url.path]
    assert all(request.url.params["ref"] == "a" * 40 for request in contents)
    assert not any("b-small/" in request.url.path or "c-small/" in request.url.path for request in contents)
    assert contents[-1].url.path.endswith("/README.md")
    with sessions() as session:
        assert session.get(Source, "github:example/review").last_success_at is not None
        assert session.scalar(select(func.count()).select_from(ChangeEvent)) == 4


def test_permanently_too_large_for_this_budget_is_deferred_without_blocking_small_units(budget_fits):
    """이번 최대 요청량으로도 읽을 수 없는 번들은 보류 이유를 남기고 작은 후보를 보존한다."""
    remote, sessions, collect = budget_fits
    result, count = collect(budget=6)
    assert result["status"] == "partial" and result["completed_units"] == 2 and count == 6
    assert result["deferred_units"][0]["reason"] == "bundle_request_budget_exceeded"
    assert result["deferred_units"][0]["required_requests"] == 6
    assert saved_batch(sessions)["completed_indices"] == [1, 2]
    assert not any("a-large/" in request.url.path for request in remote.requests)


def test_no_fitting_unit_is_partial_and_does_not_create_repo_completion(budget_fits):
    """스킬이 하나도 들어갈 수 없는 요청을 변화 없음이나 회차 완료로 보고하지 않는다."""
    _, sessions, collect = budget_fits
    result, count = collect(budget=4)
    assert result["status"] == "partial" and result["completed_units"] == result["new_versions"] == 0
    assert count == 4 and result["resume_required"] is True
    assert len(result["deferred_units"]) == 3
    assert saved_batch(sessions)["next"] == 0 and saved_batch(sessions)["completed_indices"] == []
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(AssetVersion)) == 0


def test_legacy_prefix_cursor_is_upgraded_without_repeating_completed_content(budget_fits):
    """기존 next만 있던 회차는 연속 완료 목록으로 읽어 다음 위치부터 이어간다."""
    remote, sessions, collect = budget_fits
    first, _ = collect(size=1)
    assert first["completed_units"] == 1
    with sessions.begin() as session:
        source = session.get(Source, "github:example/review")
        batch = dict(source.sync_state["batch"])
        batch.pop("completed_indices")
        source.sync_state = {**source.sync_state, "batch": batch}
    result, count = collect(budget=5)
    assert result["status"] == "done" and result["completed_units"] == 4 and count == 5
    assert saved_batch(sessions)["completed_indices"] == [0, 1, 2, 3]
    assert not any("a-large/" in request.url.path for request in remote.requests)


def test_storage_failure_does_not_mark_a_hole_complete(budget_fits, monkeypatch):
    """보류 위치를 채우는 DB 저장에 실패하면 새 원본과 완료 비트가 함께 롤백된다."""
    from backend.catalog import service
    _, sessions, collect = budget_fits
    collect(budget=9, previous_requests=2)
    before = saved_batch(sessions)
    original = service.classify_bundle

    def fail(files):
        """버전 추가 뒤 분류 단계의 DB 실패를 재현한다."""
        raise SQLAlchemyError("database detail must stay private")

    monkeypatch.setattr(service, "classify_bundle", fail)
    failed, _ = collect()
    assert failed["status"] == "failed" and failed["error"] == "storage_error"
    assert failed["completed_units"] == 2 and failed["new_versions"] == 0
    assert saved_batch(sessions) == before
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(ChangeEvent)) == 2
    monkeypatch.setattr(service, "classify_bundle", original)
    assert collect()[0]["status"] == "done"


def test_large_first_pending_source_does_not_starve_next_fitting_source(budget_fits):
    """첫 미완료 저장소에 큰 번들만 남아도 남은 예산으로 다음 저장소를 이어간다."""
    remote, sessions, _ = budget_fits
    for repository, folder in (("example/a", "skills/a-large"), ("example/b", "skills/b-small")):
        client = GithubClient(transport=httpx.MockTransport(remote.handle), max_requests=4)
        bind_policy(sessions, client, clock=lambda: remote.clock)
        try:
            result = CatalogCollector(sessions, client).collect(
                RepositoryConfig(repository=repository, skill_folders=[folder]))
            assert result["completed_units"] == 0
        finally:
            client.close()
    client = GithubClient(transport=httpx.MockTransport(remote.handle), max_requests=9)
    bind_policy(sessions, client, clock=lambda: remote.clock)
    try:
        client.get("/repos/example/search-overhead")
        client.get("/repos/example/search-overhead")
        result = CatalogCollector(sessions, client).run(CollectionConfig(resume_pending=True))
        assert client.request_count == 8
    finally:
        client.close()
    assert len(result["items"]) == 2
    assert result["items"][0]["status"] == "partial" and result["items"][0]["completed_units"] == 0
    assert result["items"][1]["status"] == "done" and result["items"][1]["completed_units"] == 2


@pytest.mark.parametrize("state", [
    {"folders": ["a", "b"], "next": 0, "completed_indices": [0]},
    {"folders": ["a", "b"], "next": 0, "completed_indices": [1, 1]},
    {"folders": ["a", "b"], "next": 0, "completed_indices": [2]},
    {"folders": ["a", "b"], "next": 0, "completed_indices": [True]},
    {"folders": ["a", "b"], "next": 4},
])
def test_invalid_completion_state_is_not_silently_treated_as_complete(state):
    """범위를 벗어나거나 README를 먼저 완료한 상태를 정상 회차로 인정하지 않는다."""
    from backend.catalog.github_client import CollectionError
    with pytest.raises(CollectionError, match="invalid_collection_state"):
        completed_indices(state)
