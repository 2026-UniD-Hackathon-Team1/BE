"""원문에 명시된 작업 설명만 규칙으로 분류하고 파일별 근거를 보존한다."""
import re
from pathlib import PurePosixPath
from backend.catalog.dimensions import (TEST_FIRST_PATTERN, UNSUPPORTED_SECTION, _is_catalog,
                                        capability_claimed, extract_dimensions)
from backend.catalog.references import enrich_referenced_capabilities

CLASSIFIER_VERSION = "explicit-context-v10"
RULES = {
    "task_types": {
        "code_review": r"\bcode[ -]review\b|\breview(?:s|ing)?\s+(?:(?:the|a|your)\s+)?(?:(?:github|gitlab)\s+)?(?:code|pull requests?|diff|changes|branch|pr)\b|코드\s*리뷰",
        "test_generation": r"\btest[ -]generation\b|\b(?:generat(?:e|es|ing)|writ(?:e|es|ing))\s+(?:(?:a|the|one)\s+)?(?:(?:unit|automated|minimal|failing|integration)\s+){0,2}tests?\b|테스트\s*생성|" + TEST_FIRST_PATTERN,
    },
    "capabilities": {
        "input_validation": r"\binput[ -]validation\b|입력\s*검증",
        "exception_handling": r"\bexception[ -]handling\b|예외\s*처리",
        "regression_test": r"\bregression\s+tests?\b|회귀\s*테스트",
        "test_coverage": r"\btest[ -]coverage\b|테스트\s*커버리지",
        "documentation": r"\bdocumentation\b|문서화",
    },
}
NEGATED = re.compile(r"\b(?:no|not|never|unsupported|cannot)\b|doesn['’]t|can['’]t|지원하지|지원\s*안|제외", re.I)
FUTURE_TASK_CLAIM = re.compile(r"\b(?:roadmap|planned|planning|coming soon|todo|not implemented|future work)\b|"
    r"\b(?:will|would)\s+(?:support|add|include|implement|provide|be\s+(?:supported|added|included|implemented|provided))\b|\bplan\s+to\b|"
    r"지원\s*예정|추가\s*예정|추후|향후", re.I)


def supported_clause(clause, match):
    """작업 문구 주변의 부정과 미구현 계획을 실제 지원 주장과 구분한다."""
    before = re.sub(r"\bnot\s+only\b", "", clause[:match.start()], flags=re.I)
    after = clause[match.end():]
    if NEGATED.search(before) or re.search(r"^\s+(?:is\s+)?(?:not supported|unsupported|지원하지|미지원)", after, re.I):
        return False
    if re.search(r"^(?:를|을|는|은)?\s*(?:지원하지|미지원|불가)", after):
        return False
    if re.search(r"\bwithout\s+(?:(?:supporting|providing|performing)\s+)?$", before, re.I):
        return False
    return not FUTURE_TASK_CLAIM.search(clause)


def _bounded_excerpt(line, clause, match):
    """긍정 주장이 나온 원문 위치 주변만 짧게 보존한다."""
    offset = line.find(clause)
    position = offset + match.start() if offset >= 0 else line.find(match.group(0))
    start = max(0, position - 80)
    return line[start:start + 240]


def _ancillary_review_role(files):
    """명시적 첫 설명이 리뷰 요청 준비나 받은 피드백 처리인 경우 독립 리뷰와 구분한다."""
    raw = next((data for path, data in files.items() if path.lower() == "skill.md"), None)
    if raw is None:
        return None
    try:
        lines = raw.decode("utf-8-sig").splitlines()
    except UnicodeError:
        return None
    description, active = [], False
    for line in lines[1:128] if lines and lines[0].strip() == "---" else []:
        if line.strip() == "---":
            break
        if line.startswith("description:"):
            active = True
            description.append(re.sub(r"^[>|+-]+$", "", line.split(":", 1)[1].strip()))
        elif active and line.startswith((" ", "\t")):
            description.append(line.strip())
        else:
            active = False
    text = " ".join(description).strip(" \"'")
    if re.match(r"(?:prepares?|drafts?|creates?|writes?)\b[^.!?]{0,80}\b(?:code[ -]review|review)\s+requests?\b", text, re.I):
        return "review_request_preparation"
    if re.match(r"(?:processes?|handles?|responds?\s+to)\b[^.!?]{0,60}\b(?:code[ -]review\s+)?feedback\b", text, re.I):
        return "review_feedback_processing"
    return None


def classify_bundle(files: dict[str, bytes]) -> dict:
    """대표 문서의 긍정적인 명시 문구를 태그로 바꾸고 코드 예시는 제외한다."""
    tags = {"task_types": [], "capabilities": [], "languages": ["unknown"],
            "supported_tools": [], "evidence": []}
    manifests = {path: content for path, content in files.items() if path.lower() == "skill.md"}
    documents = manifests or files
    for path, content in sorted(documents.items()):
        if "/" in path or PurePosixPath(path).name.lower() not in {"skill.md", "readme.md", "readme"}:
            continue
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            continue
        if _is_catalog(text):
            continue
        fence, frontmatter, comment = None, False, False
        blocked_depth = None
        description = False
        for number, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if number == 1 and stripped == "---":
                frontmatter = True
                continue
            if frontmatter:
                if stripped == "---":
                    frontmatter, description = False, False
                    continue
                if line.startswith("description:"):
                    description = True
                    stripped = line.split(":", 1)[1].strip()
                elif not description or not line.startswith((" ", "\t")):
                    description = False
                    continue
            if comment or "<!--" in stripped:
                comment = "-->" not in stripped
                continue
            marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
            if marker:
                current = marker.group(1)
                if fence is None:
                    fence = current
                elif fence[0] == current[0] and len(current) >= len(fence) and not line[marker.end():].strip():
                    fence = None
                continue
            if (fence or len(line) > 2000 or stripped.startswith((">", "|")) or
                (not frontmatter and line.startswith(("    ", "\t"))) or
                re.search(r"\[[^\]]+\]\(|^\s*\[[^\]]+\]:|^(?:example|예시)\s*:", stripped, re.I)):
                continue
            heading = re.match(r"^ {0,3}(#{1,6})\s+(.*)$", line)
            if heading:
                depth, title = len(heading.group(1)), heading.group(2)
                if blocked_depth is not None and depth <= blocked_depth:
                    blocked_depth = None
                if NEGATED.search(title) or FUTURE_TASK_CLAIM.search(title) or UNSUPPORTED_SECTION.search(title):
                    blocked_depth = depth if blocked_depth is None else min(depth, blocked_depth)
            if blocked_depth is not None:
                continue
            prose = re.sub(r"(`+).*?\1", "", stripped)
            for clause in re.split(r"(?<=[.!?])\s+|;\s*|\s+(?:but|whereas|however)\s+|하지만|반면", prose, flags=re.I):
                for category, rules in RULES.items():
                    for tag, pattern in rules.items():
                        if tag in tags[category]:
                            continue
                        match = next((item for item in re.finditer(pattern, clause, re.I)
                            if supported_clause(clause, item) and (category != "capabilities"
                                or capability_claimed(tag, clause, item))), None)
                        if match:
                            tags[category].append(tag)
                            tags["evidence"].append({"category": category, "tag": tag,
                                "path": path, "line": number, "excerpt": _bounded_excerpt(line, clause, match)})
    dimensions = enrich_referenced_capabilities(files, extract_dimensions(files))
    tags["dimensions"] = dimensions
    tags["languages"] = dimensions["languages"] or ["unknown"]
    tags["supported_tools"] = dimensions["tools"]
    # 과거 핵심 규칙의 오탐을 새 차원과 합쳐 되살리지 않고 최종 기능 근거만 사용한다.
    tags["capabilities"] = list(dimensions["capabilities"])
    tags["evidence"] = [item for item in tags["evidence"] if item["category"] != "capabilities"]
    tags["evidence"].extend({"category": "capabilities", "tag": item["value"],
        "path": item["path"], "line": item["line"], "excerpt": item["excerpt"]}
        for item in dimensions["evidence"] if item["dimension"] == "capabilities")
    role = _ancillary_review_role(files)
    if role:
        tags["task_types"] = [task for task in tags["task_types"] if task != "code_review"]
        tags["evidence"] = [item for item in tags["evidence"]
            if item["category"] != "task_types" or item["tag"] != "code_review"]
        tags["task_scope"] = {"code_review": role}
        dimensions["review_actions"], dimensions["action_evidence"] = [], []
    return tags
