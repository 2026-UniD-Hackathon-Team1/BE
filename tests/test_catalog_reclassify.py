"""오프라인 재분류가 검증한 전체 번들의 참조와 기존 데이터 무결성을 유지하는지 확인한다."""
import base64
import hashlib
import json

import pytest

from backend.catalog.classification import CLASSIFIER_VERSION, classify_bundle
from backend.catalog.github_client import MAX_BUNDLE_BYTES, MAX_FILE_BYTES, MAX_FILES
from backend.catalog.reclassify import reclassify_versions, verified_bundle_files
from backend.models import AssetVersion
from backend.seed import seed


def stored_bundle(files):
    """수집기와 같은 정규 JSON 및 SHA-256으로 고정한 번들만 만든다."""
    payload = {"files": {path: base64.b64encode(content).decode() for path, content in files.items()}}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True,
        separators=(",", ":")).encode()).hexdigest()
    return {"bundle": payload}, digest


def test_verified_bundle_decode_keeps_explicit_reference_files_and_ignores_unlinked_capabilities():
    """재분류가 참조 파일을 잘라내지 않으며 미사용 참조까지 기능으로 읽지는 않는다."""
    files = {"SKILL.md": b"# Code Review\nRead `references/security.md`.",
        "references/security.md": b"Provides API security and input validation.",
        "references/unused.md": b"Provides browser automation.", "scripts/ignored.py": b"raise RuntimeError('never run')"}
    metadata, digest = stored_bundle(files)
    decoded = verified_bundle_files(metadata, digest)
    assert decoded == files
    classified = classify_bundle(decoded)
    assert set(classified["capabilities"]) == {"api_security", "input_validation"}
    assert classified["task_types"] == ["code_review"]


@pytest.mark.parametrize("mode", ["hash", "file_count", "file_size", "bundle_size", "base64", "path"])
def test_reclassification_preserves_hash_path_and_collection_size_limits(mode):
    """참조 전달을 넓혀도 해시와 경로 및 파일 수와 용량 검증을 느슨하게 하지 않는다."""
    files = {"SKILL.md": b"# Code review"}
    if mode == "file_count":
        files.update({f"references/{index}.md": b"x" for index in range(MAX_FILES)})
    elif mode == "file_size":
        files["references/large.md"] = b"x" * (MAX_FILE_BYTES + 1)
    elif mode == "bundle_size":
        files.update({f"references/{index}.md": b"x" * MAX_FILE_BYTES
            for index in range(MAX_BUNDLE_BYTES // MAX_FILE_BYTES + 1)})
    elif mode == "path":
        files["../outside.md"] = b"x"
    metadata, digest = stored_bundle(files)
    if mode == "hash":
        digest = "0" * 64
    elif mode == "base64":
        metadata["bundle"]["files"]["SKILL.md"] = "!!!"
        digest = hashlib.sha256(json.dumps(metadata["bundle"], sort_keys=True, ensure_ascii=True,
            separators=(",", ":")).encode()).hexdigest()
    assert verified_bundle_files(metadata, digest) is None


def test_v7_primary_only_record_is_reclassified_from_full_bundle_without_changing_source(database):
    """이미 v7로 잘못 표시된 저장값도 v8에서 참조 기능을 복구하고 원본 해시와 ID를 보존한다."""
    _, sessions, _ = database
    metadata, digest = stored_bundle({"SKILL.md": b"# Code Review\nRead `references/security.md`.",
        "references/security.md": b"# Rules\nProvides API security and input validation."})
    with sessions.begin() as session:
        seed(session)
        version = session.get(AssetVersion, "demo_ver_01")
        version.bundle_sha256 = digest
        version.metadata_json = {**metadata, "classifier_version": "explicit-context-v7"}
        version.tags = {"task_types": ["code_review"], "capabilities": [], "dimensions": {"capabilities": []}}
        revision, asset_id, payload = version.revision, version.asset_id, version.metadata_json["bundle"]
    dry = reclassify_versions(sessions)
    assert dry["would_update"] == 1 and dry["network_executed"] is False
    with sessions() as session:
        assert session.get(AssetVersion, "demo_ver_01").metadata_json["classifier_version"] == "explicit-context-v7"
    assert reclassify_versions(sessions, execute=True)["updated"] == 1
    with sessions() as session:
        value = session.get(AssetVersion, "demo_ver_01")
        assert value.metadata_json["classifier_version"] == CLASSIFIER_VERSION
        assert set(value.tags["capabilities"]) == {"api_security", "input_validation"}
        assert (value.revision, value.asset_id, value.bundle_sha256) == (revision, asset_id, digest)
        assert value.metadata_json["bundle"] == payload
        assert any(item["path"] == "references/security.md" for item in value.tags["evidence"])
    assert reclassify_versions(sessions, execute=True)["updated"] == 0
