"""저장소의 테스트용 스킬과 배포용 후보를 경로 기준으로 구분한다."""
import httpx
import pytest
from sqlalchemy import func, select

from backend.catalog.config import RepositoryConfig, is_fixture_folder
from backend.catalog.github_client import GithubClient
from backend.catalog.service import CatalogCollector
from backend.models import Asset, AssetVersion, ChangeEvent, Source
from test_catalog import FakeGithub
from test_catalog_folder_priority import remote_client


@pytest.mark.parametrize("component", ["tests", "test", "__tests__", "fixtures", "fixture", "testdata",
    "node_modules", ".venv", ".git", "Tests", "TESTDATA"])
def test_exact_fixture_path_components_are_identified(component):
    """중첩 깊이와 대소문자에 관계없이 명시된 테스트 자료 구성요소를 식별한다."""
    assert is_fixture_folder(f"plugins/code-improver/{component}/skills/review-pr")


@pytest.mark.parametrize("folder", [".", "skills/testing", "skills/test-generation", "skills/fixture-review",
    "skills/tests-for-api", "specs/review-pr", "vendor/skills/review", "plugins/testdata-tools/skills/review"])
def test_valid_similar_names_and_specification_folders_remain_candidates(folder):
    """실제 테스트 생성 스킬과 명세 폴더를 부분 문자열만으로 제외하지 않는다."""
    assert not is_fixture_folder(folder)


def test_automatic_discovery_excludes_fixture_before_personal_priority(remote_client):
    """가장 관련 있어 보이는 이름의 테스트용 스킬도 자동 후보 목록에서는 제거한다."""
    fixtures = ["plugins/code-improver/tests/fixtures/skills/review-pr", "node_modules/review",
        ".venv/review", ".git/review"]
    valid = ["skills/a-documentation", "skills/z-real-review", "specs/security-review", "skills/testing"]
    client, _ = remote_client(fixtures + valid)
    plan = client.prepare(RepositoryConfig(repository="example/skills", folder_hints=["code_review"]))
    assert set(plan.state["folders"]) == set(valid)
    assert all(not is_fixture_folder(folder) for folder in plan.state["folders"])
    assert plan.state["folders"][0] in {"skills/z-real-review", "specs/security-review"}


def test_explicit_operator_scope_can_read_fixture_for_inspection(remote_client):
    """운영자가 지정한 고정 폴더는 검사 목적의 수집 범위로 유지한다."""
    fixture = "plugins/tests/fixtures/skills/review-pr"
    client, _ = remote_client([fixture, "skills/real-review"])
    plan = client.prepare(RepositoryConfig(repository="example/skills", skill_folders=[fixture]))
    assert plan.state["folders"] == [fixture] and plan.state["scope"] == [fixture]


def test_existing_pending_fixture_cursor_is_not_reordered_or_deleted(remote_client):
    """과거 고정 회차의 위치를 건너뛰거나 다시 섞지 않고 수집 역할로 별도 구분한다."""
    fixture, valid = "plugins/tests/fixtures/skills/review", "skills/real-review"
    client, _ = remote_client([fixture, valid])
    first = client.prepare(RepositoryConfig(repository="example/skills"))
    previous = {**first.state, "folders": [fixture, valid], "next": 1}
    resumed = client.prepare(RepositoryConfig(repository="example/skills", folder_hints=["code_review"]), previous)
    assert resumed.state == previous and resumed.state["next"] == 1
    assert resumed.state["folders"] == [fixture, valid]


def test_explicit_fixture_role_is_saved_and_backfilled_without_duplicate_versions(database):
    """테스트용 원본은 남기되 기존 버전에도 역할을 기록하고 새 이벤트를 만들지 않는다."""
    fixture, valid = "plugins/tests/fixtures/skills/review-pr", "skills/real-review"
    remote = FakeGithub()
    remote.files = {fixture + "/SKILL.md": b"Code review provides input validation.",
        valid + "/SKILL.md": b"Code review provides input validation.", "README.md": b"Skill collection"}
    client = GithubClient(transport=httpx.MockTransport(remote.handle), max_requests=50)
    collector = CatalogCollector(database[1], client)
    explicit = RepositoryConfig(repository="example/review", skill_folders=[fixture])
    try:
        first = collector.collect(explicit)
        assert first["status"] == "done" and first["new_versions"] == 2
        with database[1].begin() as session:
            version = session.scalar(select(AssetVersion).join(Asset).where(Asset.kind == "skill"))
            version_id = version.id
            assert version.metadata_json["source_folder"] == fixture
            assert version.metadata_json["collection_role"] == "repository_fixture"
            metadata = dict(version.metadata_json)
            metadata.pop("source_folder")
            metadata.pop("collection_role")
            version.metadata_json = metadata
        repeated = collector.collect(explicit)
        assert repeated["status"] == "done" and repeated["new_versions"] == 0
        with database[1]() as session:
            version = session.get(AssetVersion, version_id)
            assert version.metadata_json["collection_role"] == "repository_fixture"
            assert version.metadata_json["source_folder"] == fixture
            assert session.scalar(select(func.count()).select_from(ChangeEvent)) == 2
        automatic = collector.collect(RepositoryConfig(repository="example/review", folder_hints=["code_review"]))
        assert automatic["status"] == "done" and automatic["new_versions"] == 1
        with database[1]() as session:
            source = session.get(Source, "github:example/review")
            assert source.sync_state["batch"]["folders"] == [valid]
            assert session.get(AssetVersion, version_id).metadata_json["collection_role"] == "repository_fixture"
            assert session.scalar(select(func.count()).select_from(AssetVersion)) == 3
    finally:
        client.close()
