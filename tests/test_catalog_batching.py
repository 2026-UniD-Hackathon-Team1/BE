"""고정 SHA 분할 수집과 중단 후 재개를 실제 PostgreSQL에서 검증한다."""
import base64
import hashlib

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from backend.catalog.config import CollectionConfig, RepositoryConfig
from backend.catalog.github_client import GithubClient
from backend.catalog.jobs import catalog_router
from backend.catalog.service import CatalogCollector
from backend.models import AssetVersion, ChangeEvent, Source
from test_catalog import FakeGithub


class PinnedGithub(FakeGithub):
    def __init__(self, skills=23):
        """커밋별 파일을 보존하는 대형 저장소 모의 서버를 만든다."""
        super().__init__()
        self.files = {f"skills/s{index:03}/SKILL.md": b"Review code." for index in range(skills)}
        self.files["README.md"] = b"Review tools"
        self.history = {self.sha: dict(self.files)}
        self.fail_path = None

    def handle(self, request):
        """브랜치가 전진해도 기존 트리와 콘텐츠를 고정 SHA로 제공한다."""
        path = request.url.path
        if not (path.endswith("/commits") or "/git/trees/" in path or "/contents/" in path):
            return super().handle(request)
        self.requests.append(request)
        if path.endswith("/commits"):
            return httpx.Response(200, json=[{"sha": self.sha,
                "commit": {"tree": {"sha": self.tree_sha(self.sha)}}}])
        if "/git/trees/" in path:
            sha = next(value for value in self.history if self.tree_sha(value) == path.rsplit("/", 1)[1])
            return httpx.Response(200, json={"truncated": False, "tree": [
                {"path": name, "type": "blob", "mode": "100644", "size": len(content), "sha": self.blob_sha(content)}
                for name, content in self.history[sha].items()]})
        name = path.split("/contents/", 1)[1]
        if name == self.fail_path:
            return httpx.Response(429, headers={"Retry-After": "60"})
        content = self.history[request.url.params["ref"]][name]
        return httpx.Response(200, json={"path": name, "type": "file", "encoding": "base64",
            "content": base64.b64encode(content).decode(), "sha": self.blob_sha(content)})

    @staticmethod
    def tree_sha(sha):
        """커밋마다 구분되는 고정 트리 식별자를 만든다."""
        return hashlib.sha1(("tree:" + sha).encode()).hexdigest()

    @staticmethod
    def blob_sha(content):
        """Git blob 규칙에 맞는 검증용 해시를 만든다."""
        return hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()


@pytest.fixture
def batches(database):
    """각 호출이 새 클라이언트와 같은 영속 상태를 쓰도록 구성한다."""
    remote = PinnedGithub()
    sessions = database[1]

    def collect(*, size=5, budget=50, config=None):
        """프로세스 재시작처럼 클라이언트를 새로 만들어 다음 배치를 수집한다."""
        client = GithubClient(transport=httpx.MockTransport(remote.handle), max_requests=budget)
        try:
            result = CatalogCollector(sessions, client).collect(
                config or RepositoryConfig(repository="example/review"), batch_size=size)
            return result, client.request_count
        finally:
            client.close()

    return remote, sessions, collect


def test_large_repository_resumes_pinned_sha_then_refreshes(batches):
    """20개 초과 스킬을 순차 저장하고 진행 중에는 새 커밋을 섞지 않는다."""
    remote, sessions, collect = batches
    first, _ = collect()
    assert first["status"] == "partial" and first["completed_units"] == 5
    assert first["total_units"] == 24 and first["resume_required"] is True
    with sessions() as session:
        assert session.get(Source, "github:example/review").last_success_at is None
    remote.sha = "c" * 40
    remote.history[remote.sha] = {**remote.files, "skills/s000/SKILL.md": b"Review code. New version."}
    for _ in range(4):
        result, _ = collect()
    assert result["status"] == "done" and result["completed_units"] == 24
    assert result["commit_sha"] == "a" * 40
    assert result["resume_required"] is False
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(AssetVersion)) == 24
        assert set(session.scalars(select(AssetVersion.revision))) == {"a" * 40}
        source = session.get(Source, "github:example/review")
        assert len(source.sync_state["current_versions"]) == 24
        assert source.last_success_at is not None
    refreshed, _ = collect()
    assert refreshed["commit_sha"] == "c" * 40
    assert refreshed["new_versions"] == 1


def test_request_budget_pauses_before_partial_bundle_read(batches):
    """요청 예산을 미리 계산해 다음 폴더를 중간까지만 읽지 않는다."""
    remote, sessions, collect = batches
    # 첫 계획 4회 뒤 한 폴더만 읽을 수 있다.
    first, count = collect(budget=5)
    assert count == 5 and first["completed_units"] == 1
    assert first["pause_reason"] == "request_budget_insufficient"
    remote.requests.clear()
    second, count = collect(budget=5)
    assert count == 5 and second["completed_units"] == 4
    assert not any(request.url.path.endswith("/commits") for request in remote.requests)
    assert all(request.url.params["ref"] == "a" * 40 for request in remote.requests if "/contents/" in request.url.path)


def test_failed_folder_retains_cursor_and_prior_versions(batches):
    """실패한 폴더 앞의 저장 결과를 보존하고 해당 위치부터 재개한다."""
    remote, sessions, collect = batches
    remote.fail_path = "skills/s002/SKILL.md"
    failed, _ = collect()
    assert failed["status"] == "partial" and failed["error"] == "rate_limited"
    assert failed["new_versions"] == failed["completed_units"] == 2
    with sessions() as session:
        assert session.get(Source, "github:example/review").sync_state["batch"]["next"] == 2
        assert session.scalar(select(func.count()).select_from(AssetVersion)) == 2
    remote.fail_path = None
    remote.requests.clear()
    resumed, _ = collect(size=2)
    assert resumed["new_versions"] == 2 and resumed["completed_units"] == 4
    assert not any("s000/" in str(request.url) or "s001/" in str(request.url) for request in remote.requests)


def test_scope_changes_do_not_overwrite_pending_plan(batches):
    """범위가 다른 요청은 기존 회차와 커서를 덮어쓰지 않는다."""
    _, sessions, collect = batches
    collect(size=1)
    with sessions() as session:
        previous = session.get(Source, "github:example/review").sync_state
    conflict, count = collect(config=RepositoryConfig(repository="example/review", skill_folders=["skills/s020"]))
    assert count == 0 and conflict["error"] == "collection_scope_conflict"
    with sessions() as session:
        assert session.get(Source, "github:example/review").sync_state == previous


def test_competing_batch_does_not_regress_cursor(batches, monkeypatch):
    """계획 확인 후 다른 수집이 전진하면 이전 작업의 쓰기를 거부한다."""
    remote, sessions, collect = batches
    collect(size=1)
    client = GithubClient(transport=httpx.MockTransport(remote.handle))
    prepare = client.prepare

    def competing(config, continuation=None):
        """계획을 읽은 직후 다른 작업이 같은 회차를 한 번 진행하게 한다."""
        plan = prepare(config, continuation)
        collect(size=1)
        return plan

    monkeypatch.setattr(client, "prepare", competing)
    try:
        result = CatalogCollector(sessions, client).collect(RepositoryConfig(repository="example/review"))
        assert result["error"] == "collection_batch_conflict"
    finally:
        client.close()
    with sessions() as session:
        assert session.get(Source, "github:example/review").sync_state["batch"]["next"] == 2
        assert session.scalar(select(func.count()).select_from(ChangeEvent)) == 2


def test_discovery_job_can_resume_without_search_or_folder_names(database):
    """검색으로 발견한 대형 후보를 새 요청 키로 재개하고 완료를 구분한다."""
    remote = PinnedGithub()

    def factory(**kwargs):
        """실제 수집 API의 HTTP만 모의 서버로 바꾼다."""
        return GithubClient(transport=httpx.MockTransport(remote.handle), **kwargs)

    app = FastAPI()
    app.include_router(catalog_router(database[1], lambda: "local", client_factory=factory))
    with TestClient(app) as client:
        response = client.post("/api/catalog/sync", json={"discovery_queries": ["review"], "discovery_limit": 1},
                               headers={"Idempotency-Key": "discover"})
        first = client.get("/api/catalog/jobs/" + response.json()["id"]).json()
        assert first["status"] == "partial"
        assert first["result"]["items"][0]["resume_required"] is True
        remote.requests.clear()
        for index in range(4):
            response = client.post("/api/catalog/sync", json={"resume_pending": True},
                                   headers={"Idempotency-Key": f"resume-{index}"})
            result = client.get("/api/catalog/jobs/" + response.json()["id"]).json()
        assert result["status"] == "done"
        assert result["result"]["items"][0]["completed_units"] == 24
        assert not any(request.url.path == "/search/repositories" for request in remote.requests)
        assert not any(request.url.path.endswith("/commits") for request in remote.requests)


def test_bundle_and_cursor_roll_back_together(batches, monkeypatch):
    """DB 저장 도중 실패하면 번들 일부와 커서가 함께 롤백되는지 확인한다."""
    _, sessions, collect = batches
    collect(size=1)
    from backend.catalog import service
    original = service.classify_bundle

    def fail(files):
        """원본 행을 추가한 뒤 분류 단계에서 DB 오류가 발생하도록 한다."""
        raise SQLAlchemyError("private database detail")

    monkeypatch.setattr(service, "classify_bundle", fail)
    result, _ = collect(size=2)
    assert result["error"] == "storage_error" and result["new_versions"] == 0
    with sessions() as session:
        assert session.get(Source, "github:example/review").sync_state["batch"]["next"] == 1
        assert session.scalar(select(func.count()).select_from(AssetVersion)) == 1
    monkeypatch.setattr(service, "classify_bundle", original)
    result, _ = collect(size=2)
    assert result["completed_units"] == 3 and result["new_versions"] == 2


def test_unstarted_discovered_targets_survive_budget_stop(database):
    """예산 때문에 시작하지 못한 발견 후보도 DB에 남아 다음 요청으로 이어진다."""
    remote = PinnedGithub(skills=3)
    client = GithubClient(transport=httpx.MockTransport(remote.handle), max_requests=5)
    try:
        result = CatalogCollector(database[1], client).run(CollectionConfig(repositories=[
            RepositoryConfig(repository="example/review"), RepositoryConfig(repository="example/second")]))
        assert len(result["items"]) == 1
        assert result["items"][0]["status"] == "partial"
    finally:
        client.close()
    with database[1]() as session:
        assert session.get(Source, "github:example/second").sync_state["queued_config"]["repository"] == "example/second"
    client = GithubClient(transport=httpx.MockTransport(remote.handle), max_requests=50)
    try:
        result = CatalogCollector(database[1], client).run(CollectionConfig(resume_pending=True))
        assert len(result["items"]) == 2
        assert all(item["status"] == "done" for item in result["items"])
    finally:
        client.close()
