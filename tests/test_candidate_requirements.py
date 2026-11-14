"""후보의 추가 API와 설치 조건을 선언 및 미확인 상태로 분리한다."""
import base64
import hashlib
import json

from backend.catalog.requirements import declared_requirements


def bundle(text):
    """실제 후보와 같은 정렬 JSON 해시를 가진 작은 공개 문서를 구성한다."""
    payload = {"files": {"SKILL.md": base64.b64encode(text.encode()).decode()}}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True,
        separators=(",", ":")).encode()).hexdigest()
    return {"bundle": payload}, digest


def test_cli_and_external_model_setup_are_declared_without_running_them():
    """compatibility 설명이 접혀 있어도 API 제공자와 CLI 필요를 명시한다."""
    metadata, digest = bundle("---\ncompatibility: >\n  Requires the `ocr` CLI installed. Requires a\n"
        "  configured supported LLM provider before first run.\n---\n# Code review\n")
    result = declared_requirements(metadata, digest)
    assert result["external_api"] == "declared_required" and result["cost_status"] == "possible"
    assert result["setup_verified"] is False
    assert {item["kind"] for item in result["items"]} == {"cli", "external_api"}
    assert all(item["evidence"]["path"] == "SKILL.md" for item in result["items"])
    assert result["items"][0]["evidence"]["line"] == 3


def test_no_external_endpoint_does_not_claim_free_host_model():
    """별도 endpoint가 없어도 호스트 모델 토큰 비용을 무료라고 단정하지 않는다."""
    metadata, digest = bundle("Does NOT require a configured LLM endpoint.\n"
        "Use when the host agent should drive the review with its own LLM capabilities.")
    result = declared_requirements(metadata, digest)
    assert result["external_api"] == "declared_not_required" and result["cost_status"] == "possible"
    assert [item["kind"] for item in result["items"]] == ["host_model"]


def test_unknown_and_tampered_bundles_do_not_invent_requirements():
    """언급이 없거나 무결성을 확인하지 못하면 조건을 미확인으로 둔다."""
    metadata, digest = bundle("No CLI is required.\nRequires no CLI.\nDoes not require the `ocr` CLI.")
    assert declared_requirements(metadata, digest)["items"] == []
    result = declared_requirements(metadata, "0" * 64)
    assert result == {"items": [], "external_api": "unknown", "cost_status": "unknown", "setup_verified": False}


def test_references_outside_skill_folder_are_setup_conditions():
    """부모 폴더의 워크플로 참조를 실행하지 않고 패키지 확인 조건으로 표시한다."""
    metadata, digest = bundle("Use [workflow](../../workflows/review.js) after setup.")
    item = declared_requirements(metadata, digest)["items"][0]
    assert item["kind"] == "package_files" and item["verification"] == "declared"
