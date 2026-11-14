"""게시자와 작성자, 저장소와 스킬 선언 및 시각의 구분을 검증한다."""
import json

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.catalog.config import RepositoryConfig
from backend.catalog.github_client import GithubClient
from backend.catalog.metadata import manifest_license, public_facts, repository_facts, timestamp
from backend.catalog.service import CatalogCollector
from backend.main import create_app
from backend.models import Asset, AssetVersion
from test_catalog import FakeGithub


def test_dates_unknown_and_private_metadata_are_not_guessed():
    """잘못된 시각과 게시 계정 및 개인 이메일을 공개 사실에 넣지 않는다."""
    assert timestamp("2026-11-15T12:00:00+09:00") == "2026-11-15T03:00:00Z"
    for value in (None, {}, "2026-11-15", "2026-99-99T00:00:00Z"):
        assert timestamp(value) is None
    facts = repository_facts({"owner": {"login": "bad/name", "email": "PRIVATE_EMAIL"},
        "license": {"spdx_id": "NOASSERTION"}}, {"commit": {"committer": {"email": "PRIVATE_EMAIL"}}})
    assert all(value is None for value in facts.values())
    assert "PRIVATE_EMAIL" not in json.dumps(facts)
    assert public_facts({"repository": {"facts": {"publisher": "name@evil.test"}}}, True)["publisher_url"] is None
    assert public_facts({"repository": {"facts": {"publisher": "valid"}}}, False)["publisher"] is None


def test_collected_metadata_scopes_and_content_stability(database):
    """원본 선언을 상세와 추천에 연결하고 시각 갱신이 콘텐츠 중복을 만들지 않는다."""
    remote = FakeGithub()
    remote.files["skills/review/SKILL.md"] = b"---\nname: review\nlicense: Apache-2.0\n---\nReview code."

    def handle(request):
        """저장소 API의 명시적 사실과 개인 이메일 포함 커밋 응답을 제공한다."""
        response = remote.handle(request)
        data = response.json()
        if request.url.path == "/repos/example/review":
            data.update(owner={"login": "publisher", "email": "PRIVATE_EMAIL"}, license={"spdx_id": "MIT"},
                created_at="2020-01-01T00:00:00Z", pushed_at="2026-11-01T00:00:00Z")
        if request.url.path.endswith("/commits"):
            data[0]["commit"]["committer"] = {"date": "2026-11-01T01:00:00+01:00", "email": "PRIVATE_EMAIL"}
        return httpx.Response(response.status_code, json=data)

    github = GithubClient(transport=httpx.MockTransport(handle))
    collector = CatalogCollector(database[1], github)
    config = RepositoryConfig(repository="example/review")
    assert collector.collect(config)["new_versions"] == 2
    assert collector.collect(config)["new_versions"] == 0
    github.close()
    with database[1]() as session:
        versions = {asset.kind: version.id for asset, version in session.execute(select(Asset, AssetVersion)
            .join(AssetVersion, AssetVersion.asset_id == Asset.id))}
        assert len(versions) == 2
        assert "PRIVATE_EMAIL" not in json.dumps([version.metadata_json for version in session.scalars(select(AssetVersion))])
    with TestClient(create_app(database_url=database[2].attributes["database_url"], local_token="test-token")) as api:
        headers = {"X-Local-Token": "test-token"}
        skill = api.get("/api/candidates/" + versions["skill"], headers=headers).json()
        repo = api.get("/api/candidates/" + versions["repo"], headers=headers).json()
        assert skill["maintainer"]["author"] is None
        assert skill["maintainer"]["license"] == "Apache-2.0"
        assert skill["maintainer"]["last_updated_at"] is None
        assert repo["maintainer"]["license"] == "MIT"
        assert repo["maintainer"]["last_updated_at"] == "2026-11-01T00:00:00Z"
        facts = skill["source_metadata"]
        assert facts["publisher"] == "publisher" and facts["publisher_url"] == "https://github.com/publisher"
        assert facts["repository_license"] == "MIT" and facts["metadata_observed_at"]
        assert skill["published_at"] is None
        api.put("/api/profile", headers=headers, json={"role": "dev", "primary_language": "python", "task_types": ["code_review"]})
        cards = api.get("/api/recommendations", headers=headers).json()["items"]
        assert next(card for card in cards if card["candidate_version_id"] == versions["skill"])["source_metadata"] == facts


def test_manifest_license_rejects_ambiguous_or_structured_values():
    """중복 선언과 YAML 구조를 임의 해석해 라이선스로 표시하지 않는다."""
    import base64
    for text in ("---\nlicense: MIT\nlicense: Apache-2.0\n---", "---\nlicense: &secret value\n---",
                 "---\nlicense: [MIT]\n---", "license: MIT"):
        assert manifest_license({"bundle": {"files": {"SKILL.md": base64.b64encode(text.encode()).decode()}}}) is None
