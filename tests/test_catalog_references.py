"""네트워크와 파일 실행 없이 스킬이 명시한 내부 참조의 기능과 경계만 확인한다."""
import pytest

from backend.catalog.classification import classify_bundle
from backend.catalog.references import MAX_DEPTH, MAX_FILE_BYTES, MAX_FILES


def classify(primary, references):
    """문자열로 만든 작은 고정 번들만 순수 분류 함수에 전달한다."""
    files = {"SKILL.md": primary.encode(), **{path: content.encode() for path, content in references.items()}}
    return classify_bundle(files), files


def test_only_linked_capabilities_are_inherited_with_original_reference_lines():
    """실제 사용 참조의 기능만 가져오고 참조의 작업과 언어 및 도구는 상속하지 않는다."""
    tags, files = classify("# Code Review\nRead `references/checks.md`.", {
        "references/checks.md": "# Rules\nGenerate tests. Supports Rust and Claude Code.\nCheck input validation and exception handling.",
        "references/unused.md": "Provides API security and browser automation.",
    })
    assert tags["task_types"] == ["code_review"]
    assert set(tags["capabilities"]) == {"input_validation", "exception_handling"}
    assert tags["languages"] == ["unknown"] and tags["supported_tools"] == []
    assert tags["dimensions"]["frameworks"] == []
    evidence = next(item for item in tags["dimensions"]["evidence"] if item["value"] == "input_validation")
    assert evidence["path"] == "references/checks.md" and evidence["line"] == 3
    assert evidence["excerpt"] in files[evidence["path"]].decode().splitlines()[2]
    link = tags["dimensions"]["reference_links"][0]
    assert (link["source_path"], link["source_line"], link["path"]) == ("SKILL.md", 2, "references/checks.md")


def test_default_rule_directory_and_registry_follow_literal_bundle_files_only():
    """기본 내장 규칙과 레지스트리에 적힌 파일을 명시한 깊이 안에서 따른다."""
    tags, _ = classify("# Code Review\nRule pack defaults to built-in `references/rules/`.\nRead `references/probes.yaml`.", {
        "references/rules/security.md": "# API security rules\nCheck authentication and authorization.",
        "references/probes.yaml": "probes:\n  - name: coverage\n    file: references/probes/coverage.md\n",
        "references/probes/coverage.md": "# Test coverage\nInspect line coverage.",
        "references/unselected.md": "Documentation generation.",
    })
    assert set(tags["capabilities"]) == {"api_security", "test_coverage"}
    links = tags["dimensions"]["reference_links"]
    registered = next(link for link in links if link["path"] == "references/probes/coverage.md")
    assert registered["depth"] == 2 and registered["kind"] == "registry"
    assert registered["from_path"] == "references/probes.yaml"
    assert registered["source_path"] == "SKILL.md" and registered["source_line"] == 3


def test_rule_table_uses_check_columns_and_never_example_columns():
    """규칙표의 검사명과 설명을 원본 위치로 보존하고 잘못된 코드 예제는 기능으로 쓰지 않는다."""
    text = ("# Security rules\n| Rule ID | Rule | Description | Wrong example | Correct example |\n"
        "|---|---|---|---|---|\n"
        "| SEC-103 | Input validation | Validate all inputs | API security | browser automation |")
    tags, files = classify("# Code Review\nRead [rules](references/security.md).", {"references/security.md": text})
    assert tags["capabilities"] == ["input_validation"]
    evidence = next(item for item in tags["dimensions"]["evidence"] if item["value"] == "input_validation")
    assert evidence["line"] == 4 and evidence["excerpt"].strip() == "Input validation"
    assert evidence["excerpt"] in files["references/security.md"].decode().splitlines()[3]


def test_inputs_resources_references_and_examples_do_not_supply_functions():
    """입력자료나 참고 및 예시 절의 명사를 수행 기능으로 추정하지 않는다."""
    tags, _ = classify("# Code Review\nRead `references/checks.md`.", {"references/checks.md":
        "# Rules\n## Inputs\nProduct documentation.\n## Resources\nAPI security.\n"
        "## References\nBrowser automation.\n## Examples\nTest design.\n"
        "## Actual rules\nCheck input validation."})
    assert tags["capabilities"] == ["input_validation"]


@pytest.mark.parametrize("claim", ["No input validation is provided.", "Input validation is unsupported.",
    "We plan to add input validation.", "## Unsupported\nInput validation."])
def test_primary_disabled_function_is_not_revived_by_positive_reference(claim):
    """루트가 지원하지 않거나 예정으로 밝힌 기능은 참조의 긍정 문구로 복구하지 않는다."""
    tags, _ = classify("# Code Review\n" + claim + "\nRead `references/checks.md`.", {
        "references/checks.md": "Provides input validation and exception handling."})
    assert "input_validation" not in tags["capabilities"]
    assert "exception_handling" in tags["capabilities"]


def test_legacy_capabilities_are_replaced_instead_of_reviving_negated_documentation():
    """과거 추출 규칙과 새 차원의 합집합으로 문서화 오탐이 되살아나지 않는다."""
    tags, _ = classify("# Code Review\nPure documentation changes that do not affect behavior may be skipped.", {})
    assert tags["task_types"] == ["code_review"]
    assert "documentation" not in tags["capabilities"]
    assert tags["capabilities"] == tags["dimensions"]["capabilities"]
    assert not any(item["tag"] == "documentation" for item in tags["evidence"])


@pytest.mark.parametrize("bad_path", ["../outside.md", "/outside.md", "C:/outside.md", "references/../outside.md",
    "https://example.com/rules.md", "references/run.py", "references/run.sh", "references/other/SKILL.md"])
def test_malicious_registry_paths_cannot_escape_or_execute(bad_path):
    """레지스트리의 경로나 YAML 태그 및 스크립트를 실행하거나 번들 밖으로 따라가지 않는다."""
    tags, _ = classify("# Code Review\nRead `references/probes.yaml`.", {
        "references/probes.yaml": "- file: " + bad_path + "\n- file: !!python/object/apply:os.system [echo unsafe]\n",
        "outside.md": "Provides API security.", bad_path: "Provides API security.",
    })
    assert tags["capabilities"] == []
    assert all(link["path"] == "references/probes.yaml" for link in tags["dimensions"]["reference_links"])


def test_code_fenced_links_and_other_skill_catalogs_do_not_activate_reference_features():
    """명령 예시나 다른 스킬 목록을 현재 스킬의 실제 검사 기능으로 바꾸지 않는다."""
    tags, _ = classify("# Code Review\n```text\nRead `references/unused.md`.\n```\n"
        "Read `references/catalog.md`.\n## References\n[Other](references/other/SKILL.md)", {
        "references/unused.md": "Provides API security.",
        "references/catalog.md": "# Collection of agent skills\nRead `references/unused.md`.",
        "references/other/SKILL.md": "Provides browser testing.",
    })
    assert tags["capabilities"] == []


def test_depth_and_cycle_limits_keep_third_hop_unread():
    """최대 두 단계까지만 따르고 순환 링크나 더 먼 파일을 기능 근거로 쓰지 않는다."""
    tags, _ = classify("# Code Review\nRead `references/a.md`.", {
        "references/a.md": "Provides input validation.\nRead [next](b.md).",
        "references/b.md": "Provides exception handling.\nRead `a.md` and `c.md`.",
        "references/c.md": "Provides API security.",
    })
    assert set(tags["capabilities"]) == {"input_validation", "exception_handling"}
    assert tags["dimensions"]["reference_scan"]["truncated"]
    assert max(link["depth"] for link in tags["dimensions"]["reference_links"]) == MAX_DEPTH


def test_file_and_byte_limits_are_bounded_and_report_partial_scan():
    """많은 기본 규칙과 큰 문서도 정한 파일 수 및 바이트 한도를 넘지 않는다."""
    refs = {f"references/rules/{index:02d}.md": "Provides input validation." for index in range(MAX_FILES)}
    refs["references/rules/zz.md"] = "Provides API security."
    tags, _ = classify("# Code Review\nDefaults to built-in `references/rules/`.", refs)
    assert tags["capabilities"] == ["input_validation"]
    assert tags["dimensions"]["reference_scan"]["read_files"] == MAX_FILES
    assert tags["dimensions"]["reference_scan"]["truncated"]
    large, _ = classify("# Code Review\nRead `references/large.md`.", {
        "references/large.md": "Provides API security.\n" + "x" * MAX_FILE_BYTES})
    assert large["capabilities"] == [] and large["dimensions"]["reference_scan"]["truncated"]


def test_unused_directory_note_does_not_scan_every_reference_file():
    """단순 경로 설명이나 실행 스크립트 문자열을 기본 규칙 사용 선언으로 보지 않는다."""
    tags, _ = classify("# Code Review\n`references/` and `scripts/` are relative paths.\n"
        "python run.py `references/unused.md`", {"references/unused.md": "Provides API security."})
    assert tags["capabilities"] == [] and tags["dimensions"]["reference_links"] == []


def test_yaml_example_block_scalar_is_not_an_active_file_entry():
    """레지스트리의 여러 줄 예시 문자열 속 file 항목은 실제 설정으로 읽지 않는다."""
    tags, _ = classify("# Code Review\nRead `references/probes.yaml`.", {
        "references/probes.yaml": "example: |\n  file: references/unused.md\nprobes:\n  - file: references/active.md\n",
        "references/unused.md": "Provides API security.",
        "references/active.md": "Provides input validation.",
    })
    assert tags["capabilities"] == ["input_validation"]


def test_reference_quote_points_to_later_positive_documentation_claim():
    """긴 줄 앞의 참조 지시 대신 실제 문서 생성 문구가 있는 원문 부분을 표시한다."""
    text = "Read the documentation. " + "Background. " * 30 + "Generate documentation from source."
    tags, _ = classify("# Code Review\nRead `references/writing.md`.", {"references/writing.md": text})
    evidence = next(item for item in tags["dimensions"]["evidence"] if item["value"] == "documentation")
    assert "Generate documentation" in evidence["excerpt"] and evidence["excerpt"] in text


def test_explicit_unused_links_and_nested_reference_lists_are_not_active_rules():
    """사용 금지나 미사용 절 및 참조 문서의 참고 목록을 활성 검사 규칙으로 따라가지 않는다."""
    tags, _ = classify("# Code Review\nDo not read `references/forbidden.md`.\n"
        "Read `references/active.md`.\n## Unused references\nRead `references/unused.md`.", {
        "references/forbidden.md": "Provides API security.",
        "references/unused.md": "Provides browser testing.",
        "references/active.md": "# Checks\nProvides input validation.\n## References\nRead `extra.md`.",
        "references/extra.md": "Provides database changes.",
    })
    assert tags["capabilities"] == ["input_validation"]
    assert [item["path"] for item in tags["dimensions"]["reference_links"]] == ["references/active.md"]


def test_copying_external_documentation_exclusion_does_not_disable_documentation_writing():
    """외부 자료 무단 복사 제외를 문서 작성 기능 자체의 미지원으로 해석하지 않는다."""
    tags, _ = classify("---\nname: docs\ndescription: >-\n  Writes or updates engineering documentation.\n"
        "  Not for copying external documentation without provenance.\n---\n# Documentation and ADRs", {})
    assert tags["capabilities"] == ["documentation"] and tags["task_types"] == []


def test_review_markdown_checklist_questions_and_checkbox_rules_are_functional_evidence():
    """리뷰 절차가 명시한 Markdown 체크리스트의 검사 질문만 실제 기능 근거로 읽는다."""
    tags, files = classify("# Code Review\n### Phase 3: Line-by-Line Review\n```markdown\n"
        "2. **Security**\n   - Input validation?\n```\nRead `references/security.md`.", {
        "references/security.md": "# Review patterns\n### Technique: The Checklist Method\n```markdown\n"
            "### Authentication & Authorization\n- [ ] Authentication/authorization checked\n```"})
    assert set(tags["capabilities"]) == {"input_validation", "api_security"}
    assert tags["task_types"] == ["code_review"]
    for item in tags["dimensions"]["evidence"]:
        if item["dimension"] == "capabilities":
            assert item["excerpt"] in files[item["path"]].decode().splitlines()[item["line"] - 1]


@pytest.mark.parametrize("header,language,body", [
    ("Examples", "markdown", "- [ ] API security checked"),
    ("Code Review", "python", "- [ ] API security checked"),
    ("Code Review", "markdown", "### API security\nTODO"),
    ("Code Review", "markdown", "Generate tests with API security."),
])
def test_ordinary_code_fences_and_incomplete_templates_do_not_provide_functions(header, language, body):
    """일반 코드 예제나 미완성 템플릿을 리뷰 체크리스트의 수행 기능으로 승격하지 않는다."""
    tags, _ = classify("# " + header + "\n```" + language + "\n" + body + "\n```", {})
    assert "api_security" not in tags["capabilities"]
