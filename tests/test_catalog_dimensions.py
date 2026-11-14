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
