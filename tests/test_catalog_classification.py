from backend.catalog.classification import classify_bundle
import pytest


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


@pytest.mark.parametrize("text,expected", [
    ("Code review without network access.", ["code_review"]),
    ("No code review is provided.", []),
    ("Code review is not supported.", []),
    ("Supports code review, not test generation.", ["code_review"]),
    ("Not only code review but input validation.", ["code_review"]),
    ("Test generation is planned for next year.", []),
    ("Generate tests automatically.", ["test_generation"]),
    ("Writing unit tests for existing functions.", ["test_generation"]),
    ("Review the changes against a fixed revision.", ["code_review"]),
    ("Test-driven development for new features.", ["test_generation"]),
    ("Write a failing test before editing production code.", ["test_generation"]),
    ("테스트 생성 기능은 추가 예정입니다.", []),
    ("코드 리뷰를 지원하지 않습니다.", []),
    ("    Generate tests", []),
    ("> Code review", []),
    ("Example: Generate tests", []),
    ("Use the `code review` command as an example.", []),
    ("<!-- Code review -->", []),
    ("````text\n```\nGenerate tests\n````", []),
    ("---\nname: code-review\ndescription: Helps generate tests.\n---", ["test_generation"]),
    ("---\ndescription: >\n  Use for code review.\n---", ["code_review"]),
])
def test_quality_cases_keep_claims_and_exclude_non_claims(text, expected):
    """부정과 계획 및 코드 예제, frontmatter를 구분하는 고정 품질 사례다."""
    assert classify_bundle({"SKILL.md": text.encode()})["task_types"] == expected


def test_skill_manifest_takes_priority_over_readme_and_catalog_links():
    """다른 후보 목록을 현재 스킬 자체의 기능으로 분류하지 않는다."""
    assert classify_bundle({"SKILL.md": b"Validate input.", "README.md": b"Code review and generate tests"})["task_types"] == []
    assert classify_bundle({"README.md": b"# Collection of agent skills\nCode review\nGenerate tests"})["task_types"] == []
    assert classify_bundle({"README.md": b"# Tools\n- [Code review](skills/review)\n- [Generate tests](skills/tests)"})["task_types"] == []
    catalog = b"[A](skills/a)\n[B](skills/b)\n[C](skills/c)\nUse test-driven development for changes."
    assert classify_bundle({"README.md": catalog})["task_types"] == []
    assert classify_bundle({"README.md": b"A tool for code review.\n[Docs](docs)\n[Help](help)"})["task_types"] == ["code_review"]
