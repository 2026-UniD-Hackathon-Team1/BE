"""대표 문서에 실제로 적힌 기술과 지원 영역만 근거와 함께 추출한다."""

import re


DIMENSIONS_VERSION = "document-dimensions-v5"
TEST_FIRST_PATTERN = (r"\btest[ -]driven[ -]development\b|\btest[ -]first\b|"
    r"\bred[ -]green[ -]refactor\b|\bwrite\s+(?:(?:a|the|one)\s+)?test\s+first\b|"
    r"테스트\s*주도\s*개발|테스트\s*우선\s*(?:개발|전략|접근)")

# 문서의 기술 언급은 관찰값이며 설치 가능성이나 실행 호환성을 보증하지 않는다.
RULES = {
    "languages": {
        "python": r"\bpython\b|파이썬",
        "javascript": r"\bjavascript\b|(?-i:\bJS\b)|자바스크립트",
        "typescript": r"\btypescript\b|(?-i:\bTS\b)|타입스크립트",
        "rust": r"\brust\b|러스트",
        "go": r"\bgolang\b|\bgo\s+(?:language|code|projects?|modules?|development)\b|\b(?:supports?|targets?|using|for)\s+go\b|\bworks?\s+with\s+go\b|고랭",
        "java": r"\bjava\b|자바(?!스크립트)",
        "csharp": r"(?<!\w)c#(?!\w)|\bc[ -]sharp\b|씨샵",
        "cpp": r"(?<!\w)c\+\+(?!\w)|\bcpp\b",
        "ruby": r"\bruby\b|루비",
        "php": r"\bphp\b",
        "kotlin": r"\bkotlin\b|코틀린",
        "swift": r"\bswift\b|스위프트",
        "sql": r"\bsql\b",
    },
    "frameworks": {
        "fastapi": r"\bfastapi\b",
        "django": r"\bdjango\b|장고",
        "flask": r"\bflask\b|플라스크",
        "react": r"\breact(?:\.js)?\b|리액트",
        "nextjs": r"\bnext(?:\.js|js)\b",
        "vue": r"\bvue(?:\.js)?\b",
        "nuxt": r"\bnuxt(?:\.js|js)?\b",
        "express": r"\bexpress(?:\.js|js)\b|\bexpress\s+(?:framework|applications?|apps?|server)\b",
        "nestjs": r"\bnest(?:\.js|js)\b",
        "spring": r"\bspring\s+(?:boot|framework)\b|스프링\s*부트",
        "svelte": r"\bsvelte(?:kit)?\b",
        "angular": r"\bangular\b|앵귤러",
    },
    "tools": {
        "codex_cli": r"\bcodex(?:[ -]cli)?\b|코덱스",
        "claude_code": r"\bclaude[ -]code\b|클로드\s*코드",
        "cursor": r"\bcursor\s+(?:ide|editor|agent)\b|\bfor\s+cursor\b|커서\s*(?:IDE|에디터)",
        "github_copilot": r"\bgithub[ -]copilot\b|깃허브\s*코파일럿",
        "playwright": r"\bplaywright\b|플레이라이트",
        "pytest": r"\bpytest\b",
        "vitest": r"\bvitest\b",
        "jest": r"\bjest\b",
        "cypress": r"\bcypress\b",
        "docker": r"\bdocker\b|도커",
        "github_actions": r"\bgithub[ -]actions\b|깃허브\s*액션",
        "mypy": r"\bmypy\b",
        "ruff": r"\bruff\b",
        "eslint": r"\beslint\b",
    },
    "capabilities": {
        "input_validation": r"\binput[ -]validation\b|\bvalidat(?:e|es|ing)\s+(?:(?:the|all)\s+)?(?:inputs?|untrusted\s+(?:inputs?|data))\b|입력\s*검증",
        "exception_handling": r"\b(?:exception|error)[ -]handling\b|\bhandl(?:e|es|ing)\s+(?:(?:the|runtime|unexpected)\s+)?(?:errors?|exceptions?)\b|(?:예외|오류)\s*처리",
        "regression_test": r"\bregression[ -]tests?\b|\b(?:writ(?:e|es|ing)|add(?:s|ing)?|creat(?:e|es|ing))\s+(?:(?:a|the|one)\s+)?(?:(?:new|failing|regression|automated|minimal)\s+){0,2}tests?\s+(?:(?:that\s+)?reproduc(?:e|es|ing)|to\s+reproduce)\s+(?:(?:a|the|this|reported|original|same)\s+){0,2}(?:bugs?|failures?)\b|회귀\s*테스트",
        "test_coverage": r"\b(?:test|branch|line|statement)[ -]coverage\b|\bcoverage[ -](?:gaps?|reports?)\b|테스트\s*커버리지",
        "documentation": r"\bdocumentation\b|문서화",
        "api_security": r"\bapi[ -]security\b|\bapi\s+(?:authentication|authorization)\b|\bauthentication(?:\s+and\s+|\s*[&/]\s*)authorization\b|\baccess[ -]control[ -]checks?\b|API\s*보안|인증\s*[·/]?\s*인가\s*검토",
        "accessibility": r"\baccessibility\b|\ba11y\b|\bwcag\b|접근성",
        "performance": r"\bperformance\b|성능\s*(?:개선|최적화|분석|측정|검토|테스트)",
        "browser_testing": r"\bbrowser[ -](?:tests?|testing|automation)\b|\bend[ -]to[ -]end[ -](?:tests?|testing)\b|\be2e[ -](?:tests?|testing)\b|브라우저\s*(?:테스트|자동화)|종단간\s*테스트",
        "test_design": r"\btest[ -]design\b|\b(?:design|designing)\s+test\s+cases\b|\btest(?:ing)?[ -]strateg(?:y|ies)\b|테스트\s*(?:설계|전략)|경계값\s*(?:분석|테스트)|" + TEST_FIRST_PATTERN,
        "database_changes": r"\bdatabase[ -](?:changes?|migrations?)\b|\bschema[ -](?:changes?|migrations?)\b|\bsql[ -]migrations?\b|데이터베이스\s*(?:변경|마이그레이션)|스키마\s*(?:변경|마이그레이션)",
        "typing": r"\btype[ -](?:hints?|safety|checking|annotations?)\b|\bstatic[ -]typing\b|타입\s*(?:힌트|안전성|검사|검증)|정적\s*타이핑",
    },
}
COMPILED_RULES = {
    # 영문 기술명 뒤의 한국어 조사도 경계로 인식한다.
    dimension: {value: re.compile(pattern, re.I | re.ASCII) for value, pattern in values.items()}
    for dimension, values in RULES.items()
}
CATALOG = re.compile(
    r"\b(?:catalog|collection|directory|curated|awesome|list)\b.{0,70}\b(?:skills|tools|agents|repositories)\b|"
    r"\b(?:skills|tools|agents)\b.{0,40}\b(?:catalog|collection|directory|list)\b|"
    r"\b(?:skill|tool|agent)[ -](?:catalog|collection)\b|"
    r"(?:스킬|도구|에이전트)\s*(?:목록|모음|카탈로그)", re.I,
)
NEGATIVE = re.compile(
    r"\b(?:no|not|never|unsupported|cannot|without)\b|doesn['’]t|can['’]t|"
    r"지원\s*(?:하지|안)|미지원|미구현|불가|제외|(?:하지|되지)\s*않", re.I,
)
FUTURE = re.compile(
    r"\b(?:roadmap|planned|planning|plans|coming soon|upcoming|todo|to do|future|not implemented)\b|"
    r"\b(?:will|would)\s+(?:support|add|include|implement|provide|be\s+(?:supported|added|included|implemented|provided))\b|\bplan\s+to\b|"
    r"예정|추후|향후|계획", re.I,
)
SECRET_ASSIGNMENT = re.compile(
    r"\b[\w-]*(?:secret|password|passwd|token|api[_-]?key|credential)[\w-]*\s*[:=]", re.I,
)
UNSUPPORTED_SECTION = re.compile(
    r"\b(?:limitations?|non-goals?|out of scope)\b|제약|제한|지원\s*범위\s*밖", re.I,
)


def _primary_document(files: dict[str, bytes]) -> tuple[str, bytes] | None:
    """하위 문서나 경로를 따라가지 않고 루트 대표 문서 하나만 고른다."""
    priorities = {"skill.md": 0, "readme.md": 1, "readme.markdown": 2,
                  "readme": 3, "readme.rst": 4, "readme.txt": 5}
    choices = [path for path in files if isinstance(path, str)
               and "/" not in path and "\\" not in path and path.lower() in priorities]
    if not choices:
        return None
    path = min(choices, key=lambda item: (priorities[item.lower()], item))
    return path, files[path]


def _is_catalog(text: str) -> bool:
    """여러 스킬을 소개하는 색인의 언급을 해당 번들의 지원 근거로 쓰지 않는다."""
    preview = text[:16000]
    skill_links = re.findall(r"\[[^\]]+\]\([^\s)]*skills?/[^\s)]+\)", preview, re.I)
    return bool(CATALOG.search(preview)) or len(skill_links) >= 3


def _document_lines(text: str):
    """YAML 메타데이터에서는 설명 값만 남기고 원래 줄 번호를 유지한다."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        yield from enumerate(lines, 1)
        return
    closing = next((index for index in range(1, len(lines))
                    if lines[index].strip() in {"---", "..."}), None)
    if closing is None:
        return
    description_block = False
    for index in range(1, closing):
        line = lines[index]
        description = re.match(r"^description\s*:\s*(.*)$", line, re.I)
        if description:
            value = description.group(1)
            description_block = bool(re.fullmatch(r"[|>][+-]?\d?", value))
            if not description_block:
                yield index + 1, value.strip("\"'")
        elif description_block and line.startswith((" ", "\t")):
            yield index + 1, line.lstrip()
        elif line.strip():
            description_block = False
    yield from enumerate(lines[closing + 1:], closing + 2)


def _prose(line: str) -> str | None:
    """명령과 비밀값 및 링크 목록을 버리고 단순한 인라인 기술명만 남긴다."""
    stripped = line.strip()
    if (not stripped or len(line) > 2000 or line.startswith(("    ", "\t"))
            or stripped.startswith((">", "|", "$", "PS>", "&", "./", ".\\", "<"))
            or SECRET_ASSIGNMENT.search(line)):
        return None
    item = re.sub(r"^(?:[-*+]\s+|\d+[.)]\s+)", "", stripped)
    if (re.match(r"^\[[ xX]\]\s", item)
            or re.match(r"^\[[^\]]+\](?:\(|:)", item)
            or (item != stripped and re.search(r"\[[^\]]+\]\(", item))
            or re.match(r"^[\w-]+\s*:\s*", item)
            or re.match(r"^[A-Z_][A-Z0-9_]*\s*=", item)):
        return None
    if re.match(
        r"^(?:(?:pip|pip3|uv|npm|npx|pnpm|yarn|docker|git|curl|wget|export|set|powershell|pwsh|bash|sh|cmd)\s+|"
        r"(?:PS\s+\S+>|(?:Set|Get|Invoke|Start)-[\w-]+\s+)|"
        r"(?:python\d*|node)\s+(?:-|[.\\/]|\S+\.(?:py|js)\b))", item, re.I,
    ):
        return None
    stripped = re.sub(r"!\[[^\]]*\]\([^\s)]*\)", "", stripped)
    stripped = re.sub(r"\[([^\]]+)\]\([^\s)]*\)", r"\1", stripped)
    stripped = re.sub(r"https?://[^\s<>]+", "", stripped, flags=re.I)
    return re.sub(r"(`+)(.*?)\1", lambda match: match.group(2)
                  if re.fullmatch(r"[\w.+#-]{1,40}", match.group(2)) else "", stripped)


def _positive_clause(clause: str) -> bool:
    """부정과 미래 계획이 있는 절은 현재 지원 근거에서 제외한다."""
    clause = re.sub(r"\bnot\s+only\b", "", clause, flags=re.I)
    return not (NEGATIVE.search(clause) or FUTURE.search(clause))


def declares_target(dimension, value, excerpt):
    """기술의 단순 언급과 스킬 자체의 명시적인 지원 대상 선언을 구분한다."""
    pattern = RULES.get(dimension, {}).get(value)
    if dimension not in {"languages", "frameworks", "tools"} or not pattern:
        return False
    # Go의 일반 언급 판별에 포함된 지원 동사를 기술 이름 매칭에서 다시 소비하지 않는다.
    if dimension == "languages" and value == "go":
        pattern = r"\b(?:go|golang)\b|고랭"
    for clause in re.split(r";|[.!?](?=\s|$)|[。；]", excerpt):
        if not _positive_clause(clause):
            continue
        for match in re.finditer(pattern, clause, re.I | re.ASCII):
            before, after = clause[:match.start()], clause[match.end():]
            # 예제나 조건부 조언, 읽는 파일 및 구현 의존성은 프로젝트 지원 대상이 아니다.
            if re.search(r"\b(?:if|when|unless|examples?|tutorials?|reads?|uses?|parses?|renders?|helpers?|components?|implementation|internally|docs?|documentation|references?|such as)\b|예시|예제|경우|파일을?\s*읽", before, re.I):
                continue
            prefix = re.sub(r"^[\s#*\-]+", "", before)
            direct = re.search(r"^(?:(?:this|the)\s+)?(?:(?:skill|tool|agent|plugin|extension)\s+)?"
                r"(?:(?:supports?|targets?|works?\s+with|(?:is\s+)?designed\s+for)\s+|"
                r"(?:supported|target)\s+(?:languages?|frameworks?|tools?|platforms?)\s*:\s*)[^:;]{0,100}$", prefix, re.I)
            only = (re.search(r"^(?:for\s+)?[^:;]{0,60}$", prefix, re.I)
                and re.match(r"(?:\s+|-)only\b(?!\s+(?:examples?|helpers?|components?|implementation|runtime)\b)", after, re.I))
            korean = re.search(r"(?:지원\s*(?:언어|프레임워크|도구)|대상\s*(?:언어|프레임워크|도구))\s*:\s*[^:;]{0,60}$", prefix)
            korean = korean or re.match(r"(?:\s|및|와|과|[,/]|[A-Za-z0-9_.+#-]){0,60}(?:만\s*)?전용", after)
            korean = korean or (re.search(r"스킬|도구|플러그인", clause)
                and re.match(r"[^;:]{0,40}(?:지원|대상)", after))
            requires = dimension == "tools" and re.search(r"^(?:(?:this|the)\s+)?"
                r"(?:(?:skill|tool|agent|plugin)\s+)?requires?\s+[^:;]{0,60}$", prefix, re.I)
            if direct or only or korean or requires:
                return True
    return False


def capability_claimed(capability, clause, match):
    """문서 참고 지시를 문서 작성 기능으로 바꾸지 않고 실제 생성 동작은 유지한다."""
    if capability != "documentation":
        return True
    before = clause[:match.start()]
    references = list(re.finditer(r"\b(?:read(?:s|ing)?|see|refer(?:ring)?\s+(?:back\s+)?to|consult|check|follow|use)\b", before, re.I))
    if not references:
        return True
    writers = list(re.finditer(r"\b(?:writ(?:e|es|ing)|generat(?:e|es|ing)|creat(?:e|es|ing)|updat(?:e|es|ing)|maintain(?:s|ing)?|produc(?:e|es|ing)|provid(?:e|es|ed|ing))\b", before, re.I))
    return bool(writers and writers[-1].start() > references[-1].start())


def _excerpt(line: str, pattern: re.Pattern, clause=None, match=None) -> str:
    """근거 기술명이 보이도록 원문 일부를 최대 240자로 자른다."""
    offset = line.find(clause) if clause is not None else -1
    source_match = pattern.search(line)
    position = offset + match.start() if offset >= 0 and match else source_match.start() if source_match else 0
    start = max(0, position - 80)
    return line[start:start + 240]


def _target_excerpt(line, clause, pattern):
    """같은 줄의 약한 첫 언급 대신 실제 선언 절과 지원 동사를 보존한다."""
    offset = line.find(clause)
    return line[offset:offset + 240] if offset >= 0 else _excerpt(line, pattern)


def extract_dimensions(files: dict[str, bytes]) -> dict:
    """대표 문서의 명시적 기술과 지원 영역 및 첫 번째 긍정 근거를 반환한다."""
    result = {dimension: [] for dimension in RULES}
    result["evidence"] = []
    result["target_evidence"] = []
    target_values = set()
    primary = _primary_document(files)
    if primary is None:
        return result
    path, content = primary
    try:
        text = content.decode("utf-8-sig")
    except (UnicodeDecodeError, AttributeError):
        return result
    if _is_catalog(text):
        return result
    fence, comment, blocked_depth = None, False, None
    original_lines = text.splitlines()
    for number, line in _document_lines(text):
        stripped = line.strip()
        if comment or "<!--" in stripped:
            comment = "-->" not in stripped
            continue
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if marker:
            current = marker.group(1)
            if fence is None:
                fence = current
            elif (current[0] == fence[0] and len(current) >= len(fence)
                  and not line[marker.end():].strip()):
                fence = None
            continue
        if fence:
            continue
        heading = re.match(r"^ {0,3}(#{1,6})\s+(.*)$", line)
        if heading:
            depth, title = len(heading.group(1)), heading.group(2)
            if blocked_depth is not None and depth <= blocked_depth:
                blocked_depth = None
            if NEGATIVE.search(title) or FUTURE.search(title) or UNSUPPORTED_SECTION.search(title):
                blocked_depth = depth if blocked_depth is None else min(depth, blocked_depth)
        if blocked_depth is not None:
            continue
        prose = _prose(line)
        if prose is None:
            continue
        for clause in re.split(r"(?<=[.!?])\s+|;\s*|\s+(?:but|whereas|however)\s+|하지만|반면", prose, flags=re.I):
            if not _positive_clause(clause):
                continue
            for dimension, values in COMPILED_RULES.items():
                for value, pattern in values.items():
                    match = next((item for item in pattern.finditer(clause) if dimension != "capabilities"
                        or capability_claimed(value, clause, item)), None)
                    if match is None:
                        continue
                    if value not in result[dimension]:
                        result[dimension].append(value)
                        result["evidence"].append({"dimension": dimension, "value": value,
                            "path": path, "line": number,
                            "excerpt": _excerpt(original_lines[number - 1], pattern, clause, match)})
                    if ((dimension, value) not in target_values and len(result["target_evidence"]) < 120
                            and declares_target(dimension, value, clause)):
                        result["target_evidence"].append({"dimension": dimension, "value": value,
                            "path": path, "line": number,
                            "excerpt": _target_excerpt(original_lines[number - 1], clause, pattern)})
                        target_values.add((dimension, value))
    for dimension in RULES:
        result[dimension].sort()
    return result
