"""원문에 명시된 작업 설명만 규칙으로 분류하고 파일별 근거를 보존한다."""
import re
from pathlib import PurePosixPath
from backend.catalog.dimensions import extract_dimensions

CLASSIFIER_VERSION = "explicit-context-v3"
RULES = {
    "task_types": {
        "code_review": r"\bcode[ -]review\b|\breview(?:s|ing)?\s+(?:(?:the|a|your)\s+)?(?:code|pull requests?|diff|changes|branch|pr)\b|코드\s*리뷰",
        "test_generation": r"\btest[ -]generation\b|\btest[ -]driven development\b|\b(?:generat(?:e|es|ing)|writ(?:e|es|ing))\s+(?:(?:a|one)\s+)?(?:(?:unit|automated|minimal|failing|integration)\s+){0,2}tests?\b|테스트\s*생성",
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
CATALOG = re.compile(r"\b(?:catalog|collection|directory|curated|list)\b.{0,60}\b(?:skills?|tools?|agents?|resources|repositories)\b|"
                     r"\b(?:skills?|tools?|agents?)\b.{0,30}\b(?:catalog|collection|directory)\b", re.I)


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
    return not re.search(r"\b(?:roadmap|planned|coming soon|todo|not implemented)\b|지원\s*예정|추가\s*예정", clause, re.I)


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
        if not manifests:
            skill_links = re.findall(r"\[[^\]]+\]\([^\s)]*skills/[^\s)]+\)", text, re.I)
            if CATALOG.search(text[:16000]) or len(skill_links) >= 3:
                continue
        fence, frontmatter, comment = None, False, False
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
                marker = marker.group(1)
                if fence is None:
                    fence = marker
                elif fence[0] == marker[0] and len(marker) >= len(fence):
                    fence = None
                continue
            if (fence or len(line) > 500 or stripped.startswith((">", "|")) or
                (not frontmatter and line.startswith(("    ", "\t"))) or
                re.search(r"\[[^\]]+\]\(|^\s*\[[^\]]+\]:|^(?:example|예시)\s*:", stripped, re.I)):
                continue
            prose = re.sub(r"(`+).*?\1", "", stripped)
            for clause in re.split(r"(?<=[.!?;])\s+|[,;]\s*", prose):
                for category, rules in RULES.items():
                    for tag, pattern in rules.items():
                        match = re.search(pattern, clause, re.I)
                        if tag not in tags[category] and match and supported_clause(clause, match):
                            tags[category].append(tag)
                            tags["evidence"].append({"category": category, "tag": tag,
                                "path": path, "line": number, "excerpt": line})
    dimensions = extract_dimensions(files)
    tags["dimensions"] = dimensions
    tags["languages"] = dimensions["languages"] or ["unknown"]
    tags["supported_tools"] = dimensions["tools"]
    for capability in dimensions["capabilities"]:
        if capability not in tags["capabilities"]:
            tags["capabilities"].append(capability)
            evidence = next(item for item in dimensions["evidence"]
                            if item["dimension"] == "capabilities" and item["value"] == capability)
            tags["evidence"].append({"category": "capabilities", "tag": capability,
                "path": evidence["path"], "line": evidence["line"], "excerpt": evidence["excerpt"]})
    return tags
