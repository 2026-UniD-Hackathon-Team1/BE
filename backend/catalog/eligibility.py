"""저장소의 검사 데이터와 사용자에게 제공할 후보를 구분한다."""
from backend.catalog.config import is_fixture_folder


def repository_fixture(asset, version):
    """새 메타데이터와 과거 고정 경로 모두에서 저장소 테스트 원본을 식별한다."""
    metadata = version.metadata_json if isinstance(version.metadata_json, dict) else {}
    if metadata.get("collection_role") == "repository_fixture":
        return True
    folder = metadata.get("source_folder")
    if not isinstance(folder, str):
        key = asset.canonical_key.split(":", 2)
        folder = key[2] if len(key) == 3 and key[0] == "github" else ""
    return asset.kind == "skill" and is_fixture_folder(folder)
