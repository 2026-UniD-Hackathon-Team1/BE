"""원문에 명시된 작업 설명만 규칙으로 분류하고 파일별 근거를 보존한다."""
import re
from pathlib import PurePosixPath

CLASSIFIER_VERSION = "explicit-phrases-v1"
RULES = {
    "task_types": {
        "code_review": r"\bcode[ -]review\b|\breview(?:s|ing)?\s+(?:code|pull requests?)\b|코드\s*리뷰",
        "test_generation": r"\btest[ -]generation\b|\b(?:generate|generates|write|writes)\s+(?:unit\s+)?tests\b|테스트\s*생성",
    },
    "capabilities": {
        "input_validation": r"\binput[ -]validation\b|입력\s*검증",
        "exception_handling": r"\bexception[ -]handling\b|예외\s*처리",
        "regression_test": r"\bregression\s+tests?\b|회귀\s*테스트",
        "test_coverage": r"\btest[ -]coverage\b|테스트\s*커버리지",
        "documentation": r"\bdocumentation\b|문서화",
    },
}
NEGATED = re.compile(r"\b(?:not|never|unsupported|without)\b|doesn['’]t|지원하지|지원\s*안|제외", re.I)


def classify_bundle(files: dict[str, bytes]) -> dict:
    """대표 문서의 긍정적인 명시 문구를 태그로 바꾸고 코드 예시는 제외한다."""
    tags = {"task_types": [], "capabilities": [], "languages": ["unknown"],
            "supported_tools": [], "evidence": []}
    for path, content in sorted(files.items()):
        if "/" in path or PurePosixPath(path).name.lower() not in {"skill.md", "readme.md", "readme"}:
            continue
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            continue
        fence = None
        for number, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith(("```", "~~~")):
                marker = stripped[:3]
                if fence is None:
                    fence = marker
                elif fence == marker:
                    fence = None
                continue
            if fence or len(line) > 500 or NEGATED.search(line):
                continue
            for category, rules in RULES.items():
                for tag, pattern in rules.items():
                    if tag not in tags[category] and re.search(pattern, line, re.I):
                        tags[category].append(tag)
                        tags["evidence"].append({"category": category, "tag": tag,
                            "path": path, "line": number, "excerpt": line})
    return tags
