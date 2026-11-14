"""재분류와 추천의 근거 및 순서를 실제 PostgreSQL에서 검증한다."""
import httpx
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.catalog.classification import CLASSIFIER_VERSION
from backend.catalog.config import RepositoryConfig
from backend.catalog.github_client import GithubClient
from backend.catalog.reclassify import reclassify_versions
from backend.catalog.service import CatalogCollector
from backend.main import create_app
from backend.models import AssetVersion, ChangeEvent
from test_catalog import FakeGithub


def test_offline_reclassification_preserves_content_and_removes_catalog_recommendation(database):
    """옛 목록 오탐 태그를 원본 변경 없이 정정하고 추천에서 제거한다."""
    remote = FakeGithub()
    remote.files["README.md"] = b"A collection of agent skills.\nCode review and generate tests."
    github = GithubClient(transport=httpx.MockTransport(remote.handle))
    collector = CatalogCollector(database[1], github)
    collector.collect(RepositoryConfig(repository="example/review"))
    github.close()
    with database[1].begin() as session:
        versions = list(session.scalars(select(AssetVersion)))
        snapshots = {version.id: (version.revision, version.bundle_sha256, version.metadata_json["bundle"])
                     for version in versions}
        for version in versions:
            version.tags = {"task_types": ["code_review"], "languages": ["unknown"]}
            version.metadata_json = {**version.metadata_json, "classifier_version": "explicit-phrases-v1"}
    assert reclassify_versions(database[1])["would_update"] == 2
    with database[1]() as session:
        assert all(version.metadata_json["classifier_version"] == "explicit-phrases-v1"
                   for version in session.scalars(select(AssetVersion)))
    first = reclassify_versions(database[1], limit=1, execute=True)
    assert first["updated"] == 1 and first["next_after"]
    assert reclassify_versions(database[1], after=first["next_after"], execute=True)["updated"] == 1
    assert reclassify_versions(database[1], execute=True)["updated"] == 0
    with database[1]() as session:
        assert session.scalar(select(func.count()).select_from(ChangeEvent)) == 2
        for version in session.scalars(select(AssetVersion)):
            assert snapshots[version.id] == (version.revision, version.bundle_sha256, version.metadata_json["bundle"])
            assert version.metadata_json["classifier_version"] == CLASSIFIER_VERSION
    with TestClient(create_app(database_url=database[2].attributes["database_url"], local_token="test-token")) as api:
        headers = {"X-Local-Token": "test-token"}
        api.put("/api/profile", headers=headers, json={"role": "dev", "primary_language": "python", "task_types": ["code_review"]})
        cards = api.get("/api/recommendations", headers=headers).json()["items"]
        assert len(cards) == 1 and "/ skills/review" in cards[0]["title"]
        assert any("SKILL.md:4" in reason for reason in cards[0]["related_reasons"])
        assert cards[0]["support_level"] == "information_only"


def test_recollection_upgrades_classifier_without_new_versions(database):
    """다시 수집해도 분류 규칙 갱신만으로 원본 버전을 추가하지 않는다."""
    remote = FakeGithub()
    github = GithubClient(transport=httpx.MockTransport(remote.handle))
    collector = CatalogCollector(database[1], github)
    config = RepositoryConfig(repository="example/review")
    collector.collect(config)
    with database[1].begin() as session:
        for version in session.scalars(select(AssetVersion)):
            version.metadata_json = {**version.metadata_json, "classifier_version": "explicit-phrases-v1"}
    assert collector.collect(config)["new_versions"] == 0
    github.close()
    with database[1]() as session:
        assert all(version.metadata_json["classifier_version"] == CLASSIFIER_VERSION
                   for version in session.scalars(select(AssetVersion)))


def test_equal_scores_show_newest_changes_first(database):
    """동일 관련성 점수에서는 최신 변화를 먼저 보여 주고 재조회 순서가 안정적이다."""
    from backend.seed import seed
    with database[1].begin() as session:
        seed(session)
        for index in range(1, 6):
            session.get(ChangeEvent, f"demo_change_{index:02}").first_seen_at = f"2026-11-{index:02}T00:00:00.000000Z"
    with TestClient(create_app(database_url=database[2].attributes["database_url"], local_token="test-token")) as api:
        headers = {"X-Local-Token": "test-token"}
        expected = [f"demo_ver_{index:02}" for index in range(5, 0, -1)]
        for _ in range(2):
            cards = api.get("/api/recommendations", headers=headers).json()["items"]
            assert [card["candidate_version_id"] for card in cards] == expected
