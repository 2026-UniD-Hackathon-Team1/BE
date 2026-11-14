"""외부 실행과 DB 없이 원본별 스킬 계열 식별의 보수적인 경계를 검증한다."""
import base64
from types import SimpleNamespace

import pytest

from backend.catalog.families import MAX_MANIFEST_BYTES, candidate_family, canonical_family


def _candidate(asset_id, name="open-code-review", *, source="github:alibaba/open-code-review", body="본문", manifest=None):
    """같은 출처의 서로 다른 번들과 선언 이름을 가진 순수 입력을 준비한다."""
    content = manifest if manifest is not None else f"---\nname: {name}\n---\n{body}"
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    asset = {"id": asset_id, "source_id": source}
    version = {"asset_id": asset_id, "bundle_sha256": ("a" if body == "본문" else "b") * 64,
        "metadata_json": {"bundle": {"files": {"SKILL.md": encoded}}, "source_verified": False}}
    return asset, version


def test_canonical_and_plugin_mirror_with_different_hashes_share_family():
    """원본과 플러그인 미러는 내용 해시가 달라도 같은 저장소의 같은 이름이면 묶인다."""
    canonical = _candidate("asset-canonical")
    mirror = _candidate("asset-plugin-mirror", body="미러에 추가한 설명")
    assert canonical_family(*canonical) == canonical_family(*mirror)
    assert candidate_family(*canonical)["bundle_sha256"] != candidate_family(*mirror)["bundle_sha256"]
    assert candidate_family(*canonical)["basis"] == "source_manifest_name"


def test_delegate_mode_with_different_declared_name_is_another_family():
    """같은 저장소에서도 별도로 선언한 위임 모드는 미러로 합치지 않는다."""
    canonical = _candidate("canonical")
    delegate = _candidate("delegate", "open-code-review-delegate")
    assert canonical_family(*canonical) != canonical_family(*delegate)


def test_same_name_from_other_source_or_owner_is_not_merged():
    """이름이 같아도 게시 저장소나 소스가 다르면 같은 계열이라고 단정하지 않는다."""
    canonical = _candidate("canonical")
    other_owner = _candidate("other-owner", source="github:another/open-code-review")
    same_owner_other_repo = _candidate("other-repo", source="github:alibaba/other-repo")
    assert len({canonical_family(*item) for item in (canonical, other_owner, same_owner_other_repo)}) == 3


def test_identical_cross_source_bundle_hash_is_separate_metadata_for_optional_deduplication():
    """전체 번들 해시 중복은 별도로 제공하고 서로 다른 소스의 이름 계열은 유지한다."""
    left = candidate_family(*_candidate("left"))
    right = candidate_family(*_candidate("right", source="github:another/repository"))
    assert left["family_key"] != right["family_key"]
    assert left["bundle_sha256"] == right["bundle_sha256"]
    assert set(left) == {"family_key", "basis", "declared_name", "bundle_sha256"}


@pytest.mark.parametrize("manifest", [
    "# Document\nname: review", "---\ndescription: review\n---", "---\nname: review",
    "---\nname: review\nname: review\n---", "---\nname: review\n'name': other\n---",
    '---\nname: review\n"name": other\n---', "---\nname: ../review\n---",
    "---\nname: |\n  review\n---", "---\nname: [review]\n---",
    "---\nname: {review: value}\n---", "---\nname: null\nname: review\n---",
    "---\nname: $(python execute.py)\n---", "---\nname: 'review with spaces'\n---",
    "---\nname: null\n---", "---\nname: true\n---", "---\nname: 123\n---",
    "---\nname: 0xFF\n---", "---\nname: 2026-11-15\n---",
    "---\nmetadata:\n  name: review\n---",
])
def test_missing_unsafe_or_ambiguous_names_fall_back_to_individual_assets(manifest):
    """이름이 없거나 안전하게 확정할 수 없으면 각 자산을 다른 계열로 유지한다."""
    left, right = _candidate("left", manifest=manifest), _candidate("right", manifest=manifest)
    assert canonical_family(*left) != canonical_family(*right)
    assert candidate_family(*left)["basis"] == "asset_id"
    assert candidate_family(*left)["declared_name"] is None


@pytest.mark.parametrize("value", ["review", "'review'", '"review"', "review # 주석", "'review' # 주석"])
def test_simple_yaml_scalars_have_same_declared_name(value):
    """단순 따옴표와 설명 주석은 실행하거나 해석하지 않고 이름 값만 읽는다."""
    actual = candidate_family(*_candidate("asset", manifest=f"---\nname: {value}\n---"))
    assert actual["declared_name"] == "review"


def test_nested_manifests_or_case_ambiguous_root_documents_are_not_followed():
    """하위 파일과 서로 겹치는 루트 이름은 임의로 선택하지 않는다."""
    asset, version = _candidate("asset")
    encoded = version["metadata_json"]["bundle"]["files"]["SKILL.md"]
    version["metadata_json"]["bundle"]["files"] = {"nested/SKILL.md": encoded}
    assert candidate_family(asset, version)["basis"] == "asset_id"
    version["metadata_json"]["bundle"]["files"] = {"SKILL.md": encoded, "skill.md": encoded}
    assert candidate_family(asset, version)["basis"] == "asset_id"


@pytest.mark.parametrize("content", [b"\xff", b"x" * (MAX_MANIFEST_BYTES + 1)])
def test_invalid_or_oversized_manifest_is_not_parsed(content):
    """인코딩이나 크기 제한을 벗어난 원문은 이름을 추측하지 않는다."""
    asset, version = _candidate("asset")
    version["metadata_json"]["bundle"]["files"]["SKILL.md"] = base64.b64encode(content).decode("ascii")
    assert candidate_family(asset, version)["basis"] == "asset_id"


def test_invalid_base64_and_excessive_frontmatter_are_not_parsed():
    """손상된 번들과 과도하게 긴 헤더는 다른 이름을 찾아 우회하지 않는다."""
    asset, version = _candidate("asset")
    version["metadata_json"]["bundle"]["files"]["SKILL.md"] = "not valid base64!"
    assert candidate_family(asset, version)["basis"] == "asset_id"
    too_long = "---\nname: review\n" + "description: ignored\n" * 130 + "---"
    assert candidate_family(*_candidate("asset", manifest=too_long))["basis"] == "asset_id"


def test_source_and_version_pairing_are_required_for_name_based_family():
    """출처가 없거나 다른 자산의 버전이면 선언 이름으로 묶지 않는다."""
    asset, version = _candidate("asset")
    assert candidate_family({**asset, "source_id": None}, version)["basis"] == "asset_id"
    assert candidate_family(asset, {**version, "asset_id": "other"})["basis"] == "asset_id"
    with pytest.raises(ValueError):
        canonical_family({}, version)


def test_object_and_dictionary_inputs_are_stable_without_granting_trust():
    """ORM 형태 객체와 사전의 키가 같고 이름으로 검증이나 권한 필드를 만들지 않는다."""
    asset, version = _candidate("asset")
    actual = candidate_family(SimpleNamespace(**asset), SimpleNamespace(**version))
    assert actual == candidate_family(asset, version)
    assert actual["family_key"].startswith("family_")
    assert "source_verified" not in actual and "permissions" not in actual and "support_level" not in actual
    version["bundle_sha256"] = "not-a-hash"
    assert candidate_family(asset, version)["bundle_sha256"] is None
