import base64
import hashlib

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.catalog.config import CollectionConfig, RepositoryConfig
from backend.catalog.github_client import GithubClient
from backend.catalog.service import CatalogCollector
from backend.models import Asset, AssetVersion, ChangeEvent, Source
from backend.main import create_app


class FakeGithub:
    def __init__(self):
        """외부 통신 없이 커밋과 파일을 바꿀 수 있는 모의 응답을 준비한다."""
        self.sha = "a" * 40
        self.files = {"skills/review/SKILL.md": b"---\nname: review\n---\nReview code.",
                      "skills/review/check.py": b"print('not executed')", "README.md": b"Review tools"}
        self.requests = []
        self.status = None
        self.truncated = False
        self.corrupt = False
        self.incomplete = False
        self.file_mode = "100644"

    def handle(self, request):
        """요청 경로와 고정 커밋을 검사하고 GitHub 형식의 모의 JSON을 반환한다."""
        self.requests.append(request)
        path = request.url.path
        if self.status:
            return httpx.Response(self.status, headers={"Retry-After": "60"}, json={"message": "secret must not be saved"})
        if path == "/search/repositories":
            return httpx.Response(200, json={"incomplete_results": self.incomplete,
                    "items": [{"full_name": "example/review", "private": False, "archived": False}]})
        if path.endswith("/commits"):
            data = [{"sha": self.sha, "commit": {"tree": {"sha": "b" * 40}}}]
        elif "/git/trees/" in path:
            data = {"truncated": self.truncated, "tree": [
                {"path": name, "type": "blob", "mode": self.file_mode, "size": len(content),
                 "sha": hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()}
                for name, content in self.files.items()]}
        elif "/contents/" in path:
            assert request.url.params["ref"] == self.sha
            name = path.split("/contents/", 1)[1]
            content = self.files[name]
            data = {"path": name, "type": "file", "encoding": "base64",
                    "content": base64.b64encode(content).decode(),
                    "sha": "0" * 40 if self.corrupt else hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()}
        elif path.endswith("/releases"):
            data = [{"tag_name": "v1", "published_at": "2026-11-01T00:00:00Z"}]
        else:
            data = {"private": False, "default_branch": "main", "description": "Review", "language": "Python"}
        return httpx.Response(200, json=data)


@pytest.fixture
def catalog(database):
    """실제 임시 DB와 모의 HTTP만 사용하는 수집 테스트 환경을 만든다."""
    remote = FakeGithub()
    client = GithubClient(transport=httpx.MockTransport(remote.handle))
    collector = CatalogCollector(database[1], client)
    yield collector, remote, database[1]
    client.close()


def counts(sessions):
    """DB에서 자산과 불변 버전 및 변화 이벤트 수를 확인한다."""
    with sessions() as session:
        return tuple(session.scalar(select(func.count()).select_from(model))
                     for model in (Asset, AssetVersion, ChangeEvent))


def test_search_discovers_skills_and_repo_without_preset(catalog):
    """검색부터 스킬 자동 발견 및 정보 제공 후보 저장까지 검증한다."""
    collector, remote, sessions = catalog
    result = collector.run(CollectionConfig(discovery_queries=["code review skills"]))
    assert result["items"][0]["new_versions"] == 2
    assert counts(sessions) == (2, 2, 2)
    assert remote.requests[0].url.params["q"] == "code review skills is:public"
    with sessions() as session:
        assert set(session.scalars(select(Asset.kind))) == {"repo", "skill"}
        for version in session.scalars(select(AssetVersion)):
            assert version.revision == "a" * 40
            assert version.metadata_json["support_level"] == "information_only"
            expected = ["code_review"] if "SKILL.md" in version.metadata_json["bundle"]["files"] else []
            assert version.tags["task_types"] == expected
            assert "bundle" in version.metadata_json


def test_same_bundle_and_unrelated_commit_do_not_duplicate(catalog):
    """재수집과 스킬 외부 파일 변경이 중복 버전을 만들지 않는지 검증한다."""
    collector, remote, sessions = catalog
    config = RepositoryConfig(repository="example/review")
    collector.collect(config)
    remote.sha = "c" * 40
    remote.files["unrelated.txt"] = b"changed"
    assert collector.collect(config)["new_versions"] == 0
    assert counts(sessions) == (2, 2, 2)


def test_reference_change_creates_version_and_keeps_old_content(catalog):
    """참조 파일 변경을 새 버전으로 저장하고 이전 원본이 유지되는지 검증한다."""
    collector, remote, sessions = catalog
    config = RepositoryConfig(repository="example/review")
    collector.collect(config)
    remote.sha = "c" * 40
    remote.files["skills/review/check.py"] = b"new content"
    assert collector.collect(config)["new_versions"] == 1
    assert counts(sessions) == (2, 3, 3)
    with sessions() as session:
        change = session.scalar(select(ChangeEvent).where(ChangeEvent.change_type == "updated"))
        assert change.from_version_id != change.to_version_id
        original = session.get(AssetVersion, change.from_version_id)
        assert original.revision == "a" * 40
        assert base64.b64decode(original.metadata_json["bundle"]["files"]["check.py"]) == b"print('not executed')"


@pytest.mark.parametrize("attribute,value,code", [
    ("status", 401, "authentication_failed"), ("status", 429, "rate_limited"),
    ("truncated", True, "truncated_tree"), ("corrupt", True, "file_hash_mismatch"),
    ("file_mode", "120000", "unsupported_file_type"),
])
def test_failure_preserves_catalog_and_success_cursor(catalog, attribute, value, code):
    """수집 실패 시 과거 버전과 성공 커서를 유지하며 안전한 오류만 저장한다."""
    collector, remote, sessions = catalog
    config = RepositoryConfig(repository="example/review")
    collector.collect(config)
    with sessions() as session:
        previous = session.get(Source, "github:example/review").last_success_at
    setattr(remote, attribute, value)
    result = collector.collect(config)
    assert result["error"] == code
    assert counts(sessions) == (2, 2, 2)
    with sessions() as session:
        source = session.get(Source, "github:example/review")
        assert source.last_error == code
        assert source.last_success_at == previous
    if value == 429:
        assert result["retry_after"] == "60"


def test_plain_repository_is_not_missing_manifest_error(catalog):
    """스킬이 없는 일반 저장소도 README 기반 정보 후보로 수집하는지 검증한다."""
    collector, remote, sessions = catalog
    remote.files = {"README.md": b"A useful command line tool"}
    assert collector.collect(RepositoryConfig(repository="example/review"))["status"] == "done"
    assert counts(sessions) == (1, 1, 1)


def test_incomplete_search_is_reported(catalog):
    """불완전한 검색 결과를 정상적인 후보 없음으로 처리하지 않는지 검증한다."""
    collector, remote, sessions = catalog
    remote.incomplete = True
    result = collector.run(CollectionConfig(discovery_queries=["skills"]))
    assert result["discovery_errors"][0]["error"] == "incomplete_search"
    assert counts(sessions) == (0, 0, 0)


def test_invalid_repository_and_path_are_rejected():
    """URL과 상위 경로 이동 및 중복 설정이 수집 전에 거부되는지 검증한다."""
    for data in ({"repository": "https://evil.test/x"},
                 {"repository": "example/review", "skill_folders": ["../private"]}):
        with pytest.raises(ValueError):
            RepositoryConfig(**data)


def test_collected_candidate_reaches_recommendations_with_evidence(catalog):
    """모의 수집 원문이 분류와 API 추천까지 연결되며 미분류 재처리가 멱등적인지 검증한다."""
    collector, remote, sessions = catalog
    config = RepositoryConfig(repository="example/review")
    collector.collect(config)
    with sessions.begin() as session:
        version = session.scalar(select(AssetVersion).join(Asset).where(Asset.kind == "skill"))
        version_id, digest = version.id, version.bundle_sha256
        version.tags = {"task_types": []}
        version.metadata_json = {**version.metadata_json, "classification_status": "pending"}
        url = session.bind.url.render_as_string(hide_password=False)
    assert collector.collect(config)["new_versions"] == 0
    assert counts(sessions) == (2, 2, 2)
    with sessions() as session:
        version = session.get(AssetVersion, version_id)
        assert version.bundle_sha256 == digest
        assert version.tags["task_types"] == ["code_review"]
        assert version.metadata_json["classification_status"] == "rule_classified"
    headers = {"X-Local-Token": "test-local-token"}
    with TestClient(create_app(database_url=url, local_token="test-local-token")) as client:
        client.put("/api/profile", headers=headers, json={"role": "developer", "primary_language": "python",
                   "task_types": ["code_review"]})
        result = client.get("/api/recommendations", headers=headers)
        assert result.status_code == 200
        items = result.json()["items"]
        assert len(items) == 1
        assert items[0]["candidate_version_id"] == version_id
        assert items[0]["tag_evidence"][0]["excerpt"] == "Review code."
        assert items[0]["support_level"] == "information_only"
        assert items[0]["compatibility"] == "unknown"
        assert items[0]["demo"] is False
        candidates = client.get("/api/catalog/candidates").json()["items"]
        assert next(item for item in candidates if item["candidate_version_id"] == version_id)["tag_evidence"]
        client.put("/api/profile", headers=headers, json={"role": "developer", "primary_language": "python",
                   "task_types": ["test_generation"]})
        assert client.get("/api/recommendations", headers=headers).json()["items"] == []
