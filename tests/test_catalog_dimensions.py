"""외부 API와 데이터베이스 없이 문서 근거 추출의 경계를 확인한다."""

import pytest

from backend.catalog.dimensions import extract_dimensions


def _extract(text: str, path: str = "SKILL.md") -> dict:
    """UTF-8 대표 문서를 만들어 순수 추출 함수를 실행한다."""
    return extract_dimensions({path: text.encode("utf-8")})


def test_extracts_explicit_dimensions_with_original_evidence():
    """영문과 한국어 기술 언급에 실제 파일 위치가 함께 제공된다."""
    result = _extract("# Review\nPython과 TypeScript 프로젝트를 검토한다.\n"
                      "FastAPI와 React를 지원하며 Codex CLI와 pytest를 활용한다.\n"
                      "입력 검증과 예외 처리 및 회귀 테스트와 테스트 커버리지와 문서화를 제공한다.\n"
                      "API 보안과 접근성 및 성능 최적화와 브라우저 테스트를 수행한다.\n"
                      "테스트 설계와 스키마 변경 및 타입 힌트를 검토한다.")
    assert result["languages"] == ["python", "typescript"]
    assert result["frameworks"] == ["fastapi", "react"]
    assert result["tools"] == ["codex_cli", "pytest"]
    assert set(result["capabilities"]) == {
        "input_validation", "exception_handling", "regression_test", "test_coverage",
        "documentation", "api_security", "accessibility", "performance",
        "browser_testing", "test_design", "database_changes", "typing",
    }
    evidence = next(item for item in result["evidence"] if item["value"] == "typescript")
    assert evidence == {"dimension": "languages", "value": "typescript", "path": "SKILL.md",
                        "line": 2, "excerpt": "Python과 TypeScript 프로젝트를 검토한다."}
    assert all(set(item) == {"dimension", "value", "path", "line", "excerpt"}
               for item in result["evidence"])


def test_primary_skill_ignores_readme_and_nested_documents():
    """하위 스킬이나 루트 README의 별도 기술이 대표 스킬에 섞이지 않는다."""
    result = extract_dimensions({"README.md": b"Supports React and TypeScript.",
                                 "skills/other/SKILL.md": b"Supports Rust and Django.",
                                 "SKILL.md": b"Supports Python and FastAPI."})
    assert result["languages"] == ["python"]
    assert result["frameworks"] == ["fastapi"]
    assert {item["path"] for item in result["evidence"]} == {"SKILL.md"}


def test_all_capabilities_have_explicit_english_positive_claims():
    """기존 다섯 영역과 새 일곱 영역의 영문 지원 표현을 모두 검증한다."""
    result = _extract("Provides input validation, exception handling, regression tests, "
                      "test coverage and documentation.\n"
                      "Supports API security, accessibility, performance analysis, browser automation, "
                      "test design, database migrations and type safety.")
    assert set(result["capabilities"]) == {
        "input_validation", "exception_handling", "regression_test", "test_coverage",
        "documentation", "api_security", "accessibility", "performance",
        "browser_testing", "test_design", "database_changes", "typing",
    }
    assert len(result["evidence"]) == 12


def test_readme_fallback_is_case_insensitive_and_deterministic():
    """대표 스킬이 없으면 이름 우선순위에 따라 루트 README 하나를 고른다."""
    files = {"ReadMe.MD": b"Supports Python.", "README": b"Supports Rust."}
    assert extract_dimensions(files) == extract_dimensions(dict(reversed(list(files.items()))))
    assert extract_dimensions(files)["languages"] == ["python"]


@pytest.mark.parametrize("text", [
    "# A curated collection of agent skills\nSupports Python and React.",
    "# 스킬 목록\nPython과 React를 지원한다.",
    "[One](skills/one/SKILL.md)\n[Two](skills/two/SKILL.md)\n"
    "[Three](skills/three/SKILL.md)\nSupports Python and React.",
])
def test_catalogs_do_not_inherit_listed_skills(text):
    """목록 저장소의 기술 이름을 단일 스킬의 지원 목록으로 만들지 않는다."""
    assert not any(_extract(text, "README.md").values())


def test_only_frontmatter_description_is_read_with_original_line_numbers():
    """실행 설정이나 YAML 이름 대신 설명 값에 적힌 근거만 사용한다."""
    result = _extract("---\nname: Python React\nallowed-tools: pytest\n"
                      "description: >\n  Supports TypeScript and FastAPI.\n"
                      "metadata:\n  language: Rust\n---\n")
    assert result["languages"] == ["typescript"]
    assert result["frameworks"] == ["fastapi"]
    assert result["tools"] == []
    assert all(item["line"] == 5 for item in result["evidence"])


def test_frontmatter_scalar_description_and_unclosed_frontmatter():
    """한 줄 설명을 허용하고 닫히지 않은 YAML은 보수적으로 비운다."""
    assert _extract("---\ndescription: 'Supports Python.'\n---")["languages"] == ["python"]
    assert not any(_extract("---\ndescription: Supports Python.\nname: React").values())


def test_skips_commands_code_links_comments_secrets_and_yaml_keys():
    """문서의 실행 예시와 숨겨진 내용 및 비밀값은 추출과 근거에서 제외한다."""
    result = _extract("```python\nPython FastAPI\n```\n"
                      "~~~text\nTypeScript React\n~~~\n"
                      "    Rust Django\n\tRuby Flask\n"
                      "- [Python review](https://example.com) supports FastAPI\n"
                      "[React](https://example.com)\n"
                      "<!-- Python\nTypeScript -->\n"
                      "pip install pytest\n$ npm install playwright\npython -m pytest\n"
                      "API_KEY=Python\nAWS_SECRET_ACCESS_KEY=TypeScript\n"
                      "metadata: Python\n> Python supports React\n"
                      "Supports `Kotlin` and [Vue](https://example.com).")
    assert result["languages"] == ["kotlin"]
    assert result["frameworks"] == ["vue"]
    assert result["tools"] == []
    assert all("SECRET" not in item["excerpt"] and "API_KEY" not in item["excerpt"]
               for item in result["evidence"])


@pytest.mark.parametrize("claim", [
    "Python is not supported.", "No TypeScript support.", "Cannot use Python.",
    "Python 지원하지 않는다.", "TypeScript는 미지원이다.", "Python 지원 예정이다.",
    "We plan to add TypeScript.", "Will support Python.", "Without Python support.",
    "API security is unsupported.", "브라우저 테스트는 제공하지 않는다.",
])
def test_negated_and_future_claims_do_not_create_dimensions(claim):
    """부정하거나 구현을 약속한 문구를 현재 제공하는 기능으로 오인하지 않는다."""
    assert not any(_extract(claim).values())


def test_roadmap_section_stays_blocked_until_next_peer_heading():
    """계획과 미지원 하위 절의 목록도 현재 지원으로 분류하지 않는다."""
    result = _extract("# Review\nSupports Python.\n## Roadmap\nTypeScript and React.\n"
                      "### Details\nRust and Django.\n## Current support\nSupports FastAPI.\n"
                      "## Unsupported\nRuby and Flask.\n## Available\nUses pytest.")
    assert result["languages"] == ["python"]
    assert result["frameworks"] == ["fastapi"]
    assert result["tools"] == ["pytest"]


def test_future_and_limitations_sections_and_checklists_are_excluded():
    """미래 항목과 제약 목록 및 체크박스도 지원 기술로 취급하지 않는다."""
    result = _extract("# Future\nPython and React\n# Current\nUses FastAPI.\n"
                      "- [ ] TypeScript support\n- [x] Rust experiment\n"
                      "# Limitations\nDjango and pytest\n# Available\nUses Flask.")
    assert result["languages"] == []
    assert result["frameworks"] == ["fastapi", "flask"]
    assert result["tools"] == []


def test_fence_with_trailing_language_does_not_close_an_existing_fence():
    """닫는 울타리 뒤에 문자가 있으면 코드 블록을 계속 제외한다."""
    result = _extract("```text\n```python\nPython FastAPI\n```\nSupports TypeScript.")
    assert result["languages"] == ["typescript"]
    assert result["frameworks"] == []


def test_distinct_positive_clause_survives_negated_claim():
    """같은 줄의 별도 긍정 주장과 부정 주장을 구분한다."""
    result = _extract("Supports Python, but TypeScript is not supported.\n"
                      "Not only FastAPI but also Django are supported.")
    assert result["languages"] == ["python"]
    assert result["frameworks"] == ["django", "fastapi"]


@pytest.mark.parametrize("claim", [
    "Python, FastAPI, React and pytest are not supported.",
    "We plan to add Python, FastAPI and pytest.",
    "Python과 FastAPI, React는 지원하지 않는다.",
])
def test_negation_and_plan_apply_to_entire_comma_separated_list(claim):
    """쉼표 뒤의 나열 항목도 앞뒤의 부정과 계획 범위에 포함한다."""
    assert not any(_extract(claim).values())


def test_numeric_link_lists_and_command_variants_are_not_prose():
    """번호 링크와 셸별 실행 문법 및 URL 문자열을 기술 근거로 쓰지 않는다."""
    result = _extract("1. Supports [Python](https://example.com)\n"
                      "& python -m pytest\nPS C:\\work> python -m pytest\n"
                      ".\\python.exe -m pytest\npwsh ./python.ps1\n"
                      "![Python](https://example.com/python.svg)\n"
                      "See https://example.com/python/react/pytest for details.\n"
                      "Supports JS and TS.")
    assert result["languages"] == ["javascript", "typescript"]
    assert result["frameworks"] == []
    assert result["tools"] == []


def test_does_not_guess_languages_or_capabilities_from_tools_or_source():
    """저장소 코드나 도구명만으로 언어와 세부 기능을 추론하지 않는다."""
    result = extract_dimensions({"SKILL.md": b"Uses pytest and Playwright. Let's go review changes.",
                                 "main.py": b"import fastapi", "package.json": b"typescript"})
    assert result["tools"] == ["playwright", "pytest"]
    assert result["languages"] == []
    assert result["frameworks"] == []
    assert result["capabilities"] == []


def test_conservative_ambiguous_names_and_common_aliases():
    """일반 동사와 식별자를 피하면서 명시된 기술 별칭을 처리한다."""
    result = _extract("Go language and C# and C++ projects are supported.\n"
                      "Uses Next.js with Claude Code and Cursor IDE.\n"
                      "We express ideas and move the cursor.")
    assert result["languages"] == ["cpp", "csharp", "go"]
    assert result["frameworks"] == ["nextjs"]
    assert result["tools"] == ["claude_code", "cursor"]


def test_evidence_is_bounded_contains_match_and_deduplicates_values():
    """긴 설명은 기술명 주변만 보존하고 동일 관찰값을 반복하지 않는다."""
    result = _extract("A " * 180 + "supports Python with FastAPI.\nSupports Python again.")
    assert result["languages"] == ["python"]
    assert len(result["evidence"]) == 2
    assert all(len(item["excerpt"]) <= 240 for item in result["evidence"])
    assert all(item["line"] == 1 for item in result["evidence"])
    assert "Python" in result["evidence"][0]["excerpt"]


@pytest.mark.parametrize("files", [{}, {"nested/README.md": b"Python"},
                                     {"nested\\SKILL.md": b"Python"},
                                     {"SKILL.md": b"\xff", "README.md": b"Python"}])
def test_missing_or_invalid_primary_document_has_no_inferred_values(files):
    """대표 문서를 읽을 수 없으면 다른 파일이나 인코딩을 추측하지 않는다."""
    assert not any(extract_dimensions(files).values())


@pytest.mark.parametrize("text", [
    "# Test-Driven Development (TDD)",
    "Use test-first for selected behavior where regression risk warrants it.",
    "Apply the red-green-refactor cycle to one behavior.",
    "Write the test first. Watch it fail. Write minimal code to pass.",
    "테스트 주도 개발로 변경할 동작을 먼저 확인한다.",
])
def test_explicit_test_first_procedures_are_test_design_without_other_inference(text):
    """명시적인 테스트 우선 절차를 설계 기능으로 인식하되 다른 검증 기능은 만들지 않는다."""
    result = _extract(text)
    assert result["capabilities"] == ["test_design"]
    assert result["languages"] == [] and result["tools"] == []
    evidence = result["evidence"][0]
    assert evidence["value"] == "test_design" and evidence["line"] == 1
    assert evidence["excerpt"] in text and len(evidence["excerpt"]) <= 240


@pytest.mark.parametrize("text", [
    "Test-driven development is unsupported.", "No test-first workflow is provided.",
    "We plan to add test-first development.", "테스트 주도 개발은 지원하지 않는다.",
    "# Roadmap\nTest-driven development\n# Current\nNo testing features.",
    "```text\nTest-Driven Development\n```", "TDD is an acronym in this glossary.",
])
def test_test_first_negations_plans_code_and_ambiguous_acronyms_stay_unknown(text):
    """동의어도 부정과 계획 및 코드와 단독 약어에서는 현재 기능으로 분류하지 않는다."""
    assert _extract(text)["capabilities"] == []


def test_single_skill_directory_note_is_not_a_catalog_declaration():
    """실제 브라우저 스킬의 단수 폴더 참조 때문에 대표 문서 전체를 버리지 않는다."""
    text = ("---\nname: agent-browser\ndescription: Browser automation CLI for AI agents.\n---\n"
            "# Browser Automation with agent-browser\n"
            "Read LOCAL-INSIGHTS.md in this skill directory for tested workflows.")
    result = _extract(text)
    assert result["capabilities"] == ["browser_testing"]
    assert result["evidence"][0]["line"] == 3


@pytest.mark.parametrize("title", [
    "A collection of agent skills", "A curated list of tools", "Skill catalog", "Skills directory",
])
def test_real_catalog_stays_excluded_even_with_skill_manifest_name(title):
    """대표 파일 이름이 SKILL.md여도 실제 목록 설명의 기능을 개별 스킬에 섞지 않는다."""
    assert not any(_extract("# " + title + "\nSupports test-first development and browser automation.").values())


def test_later_explicit_target_survives_first_mention_in_title():
    """제목의 첫 기술 언급을 유지하면서 뒤의 실제 지원 선언도 별도 보존한다."""
    result = _extract("# rust-test-design for codex-cli examples\n"
                      "Supports Rust and Codex CLI.\nProvides test generation and test design.")
    first = next(item for item in result["evidence"] if item["value"] == "rust")
    assert first["line"] == 1 and first["excerpt"].startswith("# rust-test-design")
    targets = result["target_evidence"]
    assert {(item["dimension"], item["value"], item["line"]) for item in targets} == {
        ("languages", "rust", 2), ("tools", "codex_cli", 2)}
    assert all(item["excerpt"] == "Supports Rust and Codex CLI." for item in targets)


def test_first_strong_target_per_technology_is_deterministic_and_bounded():
    """반복된 지원 문장은 중복 없이 첫 강한 선언 위치만 유지한다."""
    text = "# Python and Rust notes\nSupports Python and Rust and Codex CLI.\n" + "Supports Rust.\n" * 150
    result = _extract(text)
    assert len(result["target_evidence"]) == 3
    assert all(item["line"] == 2 and len(item["excerpt"]) <= 240 for item in result["target_evidence"])
    assert all(set(item) == {"dimension", "value", "path", "line", "excerpt"}
               for item in result["target_evidence"])


@pytest.mark.parametrize("claim", [
    "Rust is not supported.", "We plan to support Rust.",
    "The tutorial uses Rust examples.", "When the project uses Rust, inspect the source.",
    "```text\nSupports Rust.\n```", "## Roadmap\nSupports Rust.",
])
def test_weak_title_does_not_upgrade_unsupported_or_incidental_claim(claim):
    """제목의 언급 뒤에 부정이나 계획과 예제가 와도 지원 대상으로 승격하지 않는다."""
    result = _extract("# Rust notes\n" + claim)
    assert result["languages"] == ["rust"]
    assert result["target_evidence"] == []


def test_strong_claim_excerpt_keeps_later_clause_and_its_support_verb():
    """같은 긴 줄에 먼저 나온 단순 언급보다 뒤의 지원 선언 문구를 정확히 보존한다."""
    text = "Rust appears in examples. " + "Background context. " * 25 + "Supports Rust and Codex CLI."
    result = _extract(text)
    assert {item["value"] for item in result["target_evidence"]} == {"rust", "codex_cli"}
    assert all("Supports Rust and Codex CLI" in item["excerpt"] for item in result["target_evidence"])
    assert all(item["excerpt"] in text and len(item["excerpt"]) <= 240 for item in result["target_evidence"])


def test_explicit_only_heading_is_distinct_from_plain_technology_title():
    """기술명이 들어간 제목과 명시적인 전용 대상 제목을 구분한다."""
    assert _extract("# Python notes")["target_evidence"] == []
    assert _extract("# Python-only")["target_evidence"][0]["value"] == "python"


@pytest.mark.parametrize("claim", ["Supports Go and Codex CLI.", "This skill targets Go.", "Works with Go."])
def test_go_context_match_does_not_consume_its_explicit_support_verb(claim):
    """일반 동사와 구분하는 Go 문맥 규칙 때문에 실제 지원 선언의 동사를 잃지 않는다."""
    result = _extract(claim)
    assert any(item["dimension"] == "languages" and item["value"] == "go"
        for item in result["target_evidence"])
