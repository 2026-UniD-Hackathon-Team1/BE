from backend.catalog.classification import classify_bundle


def test_explicit_tasks_keep_exact_source_evidence():
    """태그의 원문과 줄 번호를 보존하고 실행 호환성은 추측하지 않는다."""
    tags = classify_bundle({"SKILL.md": "# Review\nCode review with input validation.\n테스트 생성과 회귀 테스트\n".encode()})
    assert tags["task_types"] == ["code_review", "test_generation"]
    assert tags["capabilities"] == ["input_validation", "regression_test"]
    assert tags["languages"] == ["unknown"]
    assert tags["supported_tools"] == []
    evidence = next(item for item in tags["evidence"] if item["tag"] == "code_review")
    assert evidence == {"category": "task_types", "tag": "code_review", "path": "SKILL.md",
                        "line": 2, "excerpt": "Code review with input validation."}


def test_unsupported_examples_and_reference_files_are_not_task_claims():
    """부정 문장과 코드 예시, 참조 파일 및 해독할 수 없는 문서는 근거로 쓰지 않는다."""
    tags = classify_bundle({
        "SKILL.md": b"Not for code review.\n```text\ngenerate tests\n```\n~~~\nCode review\n~~~\n",
        "references/README.md": b"Generate tests",
        "example.py": b"# code review",
        "README.md": b"\xff\xfe",
    })
    assert tags["task_types"] == []
    assert tags["evidence"] == []
