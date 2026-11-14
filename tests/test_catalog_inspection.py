import base64
import hashlib
import json

from backend.catalog.inspection import inspect_bundle, permission_declarations


def payload(files):
    """모의 원문을 수집기와 같은 고정 JSON 및 해시로 만든다."""
    bundle = {"files": {path: base64.b64encode(value).decode() for path, value in files.items()}}
    digest = hashlib.sha256(json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return bundle, digest


def test_permissions_keep_locations_without_command_arguments():
    """권한 종류와 줄 번호를 추출하고 명령 인수와 추가 권한을 추측하지 않는다."""
    text = b"---\nname: review\nallowed-tools: [Read, Edit, Bash(curl SECRET_TOKEN), WebFetch]\n---\n"
    result = permission_declarations(text)
    assert result["verification"] == "declared"
    assert {item["kind"] for item in result["declared"]} == {"file_read", "file_write", "command_exec", "network"}
    assert all(item["additional"] is None for item in result["declared"])
    assert all(item["evidence"] == [{"path": "SKILL.md", "line": 3}] for item in result["declared"])
    assert "SECRET_TOKEN" not in json.dumps(result)


def test_multiline_unknown_tools_remain_partial():
    """지원하지 않는 도구가 섞인 선언을 완전한 권한 분석으로 표시하지 않는다."""
    result = permission_declarations(b"---\nallowed-tools:\n  - Read\n  - mcp_unknown\n---\n")
    assert result["verification"] == "partial"
    assert result["declared"][0]["evidence"][0]["line"] == 3
    assert permission_declarations(b"Read and Bash in body")["verification"] == "unknown"
    assert permission_declarations(b"---\nallowed-tools: Read\n")["verification"] == "unknown"


def test_space_separated_tools_and_unclosed_arguments():
    """공식 공백 구분 형식을 읽고 깨진 괄호를 완전한 선언으로 표시하지 않는다."""
    value = permission_declarations(b"---\nallowed-tools: Bash(git:*) Bash(jq:*) Read\n---\n")
    assert value["verification"] == "declared"
    assert {item["kind"] for item in value["declared"]} == {"command_exec", "file_read"}
    broken = permission_declarations(b"---\nallowed-tools: Bash(curl SECRET\n---\n")
    assert broken["verification"] == "partial"


def test_bundle_mismatch_and_invalid_paths_do_not_produce_permission_claims():
    """해시 불일치와 잘못된 파일 경로는 선언 분석 전에 차단한다."""
    bundle, digest = payload({"SKILL.md": b"---\nallowed-tools: Bash\n---"})
    result = inspect_bundle({"bundle": bundle}, "0" * 64)
    assert result["integrity_status"] == "mismatch" and result["permissions"]["declared"] == []
    invalid, digest = payload({"../SKILL.md": b"private"})
    assert inspect_bundle({"bundle": invalid}, digest)["integrity_status"] == "invalid"


def test_referenced_code_is_detected_without_execution():
    """참조 스크립트는 존재만 표시하며 내용은 실행하거나 응답에 복사하지 않는다."""
    bundle, digest = payload({"SKILL.md": b"plain description", "run.py": b"raise RuntimeError('MUST_NOT_EXECUTE')"})
    result = inspect_bundle({"bundle": bundle}, digest)
    assert result["integrity_status"] == "verified" and result["executable_files"]
    assert "MUST_NOT_EXECUTE" not in json.dumps(result)
