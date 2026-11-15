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


def test_real_tdd_heading_has_supported_task_and_test_design_evidence():
    """실제 공개 스킬에서 사용한 TDD 표제를 작업과 설계 기능에 함께 연결한다."""
    text = ("---\nname: test-driven-development\ndescription: Use before implementation.\n---\n"
            "# Test-Driven Development (TDD)\nWrite the test first. Watch it fail.")
    tags = classify_bundle({"SKILL.md": text.encode()})
    assert tags["task_types"] == ["test_generation"]
    assert tags["capabilities"] == ["test_design"]
    assert tags["dimensions"]["languages"] == []
    assert tags["dimensions"]["tools"] == []
    assert next(item for item in tags["evidence"] if item["tag"] == "test_design")["line"] == 5


def test_long_description_keeps_bounded_evidence_instead_of_losing_supported_task():
    """500자를 넘는 실제 문서형 설명도 긍정 작업 주변의 짧은 근거로 보존한다."""
    description = "Useful context. " * 45 + "Generate tests before changing implementation."
    text = "---\nname: tests\ndescription: " + description + "\n---"
    tags = classify_bundle({"SKILL.md": text.encode()})
    assert tags["task_types"] == ["test_generation"]
    evidence = next(item for item in tags["evidence"] if item["category"] == "task_types")
    assert evidence["line"] == 3 and "Generate tests" in evidence["excerpt"]
    assert len(evidence["excerpt"]) <= 240
    assert evidence["excerpt"] in text.splitlines()[2]


@pytest.mark.parametrize("text", [
    "# Roadmap\nTest-first development for new features.",
    "# Unsupported\nTest-driven development.",
    "No code review, test generation or test-first development.",
    "We will support test-first development.",
    "---\nname: test-first\ndescription: No testing support.\n---",
    "Context. " * 70 + "You don't review or fix the code yourself.",
])
def test_new_testing_vocabulary_does_not_turn_non_support_into_tasks(text):
    """동의어와 긴 설명 처리도 계획과 부정 및 오케스트레이션을 작업으로 만들지 않는다."""
    assert classify_bundle({"SKILL.md": text.encode()})["task_types"] == []


def test_error_handling_review_claim_maps_to_existing_goal_without_task_expansion():
    """오류 처리 검토 문구를 기존 목표로 연결하며 실제 코드 리뷰 명시를 유지한다."""
    tags = classify_bundle({"SKILL.md": b"Code review.\nReport error handling gaps with context."})
    assert tags["task_types"] == ["code_review"]
    assert "exception_handling" in tags["capabilities"]
    evidence = next(item for item in tags["evidence"] if item["tag"] == "exception_handling")
    assert evidence["line"] == 2 and "error handling" in evidence["excerpt"]


def test_new_capability_vocabulary_does_not_label_implementation_or_security_audit_as_review():
    """기능 동의어가 있다고 구현 가이드나 일반 보안 감사를 코드 리뷰로 분류하지 않는다."""
    for text in ("A guide to handle errors and validate inputs.",
                 "Security audit of authentication and authorization.",
                 "Memory safety vulnerability assessment."):
        assert classify_bundle({"SKILL.md": text.encode()})["task_types"] == []


@pytest.mark.parametrize("text", ["Read the documentation.", "See the project documentation.",
                                 "Refer to the API documentation."])
def test_legacy_capability_path_cannot_restore_reference_only_documentation(text):
    """기존 핵심 태그 추출 경로에서도 참조 전용 문구가 문서 작성 기능으로 되살아나지 않는다."""
    tags = classify_bundle({"SKILL.md": text.encode()})
    assert "documentation" not in tags["capabilities"]
    assert not any(item["tag"] == "documentation" for item in tags["evidence"])


def test_explicit_test_strategy_is_design_without_claiming_test_code_generation():
    """실제 전략 스킬의 설계 기능을 인식하되 명시적으로 제외한 테스트 작성은 붙이지 않는다."""
    text = ("---\nname: test-strategy\ndescription: >-\n"
        "  Plans risk-based test coverage for a feature.\n  Not for writing one test case.\n---\n# Test Strategy\n"
        "Choose what to test and define release gates.")
    tags = classify_bundle({"SKILL.md": text.encode()})
    assert "test_design" in tags["capabilities"]
    assert tags["task_types"] == []
    evidence = next(item for item in tags["dimensions"]["evidence"] if item["value"] == "test_design")
    assert evidence["line"] == 7 and evidence["excerpt"] == "# Test Strategy"


def test_explicit_github_pull_request_review_is_an_actual_review_task():
    """실제 독립 리뷰 설명의 GitHub 수식어 때문에 수행 작업이 누락되지 않는다."""
    tags = classify_bundle({"SKILL.md": b"---\nname: pr-code-reviewer\ndescription: >-\n  Reviews a GitHub pull request for correctness and test gaps.\n---\n# PR Code Reviewer"})
    assert tags["task_types"] == ["code_review"]
    assert any(item["category"] == "task_types" and item["line"] == 4 for item in tags["evidence"])


@pytest.mark.parametrize("description,role", [
    ("Prepares a focused code review request for a branch.", "review_request_preparation"),
    ("Processes code review feedback by validating findings.", "review_feedback_processing"),
])
def test_preparing_or_receiving_reviews_is_not_independent_code_review(description, role):
    """리뷰 단어가 있어도 명시한 주 기능이 요청 준비나 피드백 처리이면 독립 리뷰로 추천하지 않는다."""
    tags = classify_bundle({"SKILL.md": ("---\nname: helper\ndescription: " + description + "\n---\n# Code Review Helper").encode()})
    assert tags["task_types"] == [] and tags["task_scope"]["code_review"] == role


def test_reviewing_regression_coverage_is_not_generic_regression_hunting():
    """명시한 테스트 회귀 커버리지 검토와 일반적인 회귀 결함 조사를 구분한다."""
    positive = classify_bundle({"SKILL.md": b"Reviews a GitHub pull request.\n- **Tests:** missing regression coverage and assertions."})
    assert positive["task_types"] == ["code_review"] and "regression_test" in positive["capabilities"]
    assert "regression_test" not in classify_bundle({"SKILL.md": b"Hunt regressions in changed code."})["capabilities"]


def test_reviewed_missing_regression_coverage_survives_unfalsifiable_assertion_finding():
    """실제 검사 목록의 실패하지 않는 assertion 설명을 제공 기능 미지원으로 오인하지 않는다."""
    text = "Reviews a GitHub pull request.\n- **Tests:** missing regression coverage, assertions that cannot fail for the defect."
    tags = classify_bundle({"SKILL.md": text.encode()})
    assert "regression_test" in tags["capabilities"]
    evidence = next(item for item in tags["dimensions"]["evidence"] if item["value"] == "regression_test")
    assert evidence["line"] == 2 and evidence["excerpt"] in text.splitlines()[1]
    assert "regression_test" not in classify_bundle({"SKILL.md": b"No regression tests are provided."})["capabilities"]


def test_output_category_enum_does_not_become_documentation_function():
    """출력 분류 라벨에 등장한 문서화와 성능 단어를 수행 기능으로 등록하지 않는다."""
    text = ("# Code Review\nOutput includes category (bug / performance / documentation / other).\n"
        "- `category`: Issue category (bug, performance, documentation, other)")
    tags = classify_bundle({"SKILL.md": text.encode()})
    assert tags["task_types"] == ["code_review"] and tags["capabilities"] == []
