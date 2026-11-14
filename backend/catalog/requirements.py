"""공개 원문이 선언한 추가 설치와 모델 사용 조건을 실행 없이 읽는다."""
import base64
import re

from backend.catalog.inspection import inspect_bundle

CLI = re.compile(r"\brequires?\s+(?:the\s+)?[`\"']?([A-Za-z0-9_.-]{1,40})[`\"']?\s+CLI\b", re.I)
API = re.compile(r"\brequires?\s+(?:a\s+)?(?:configured\s+)?(?:supported\s+)?(?:LLM|API)\s+(?:provider|endpoint|key)\b", re.I)
NO_API = re.compile(r"\bdoes\s+not\s+require\s+(?:a\s+)?(?:configured\s+)?(?:supported\s+)?(?:LLM|API)\s+(?:provider|endpoint|key)\b", re.I)
HOST = re.compile(r"\bhost\s+(?:agent|model)\b[^.!?]{0,100}\b(?:own\s+LLM|own\s+intelligence|existing\s+model)\b", re.I)
PARENT_LINK = re.compile(r"\[[^\]\n]{0,120}\]\(((?:\.\./)+[^\s)#?]{1,200})\)")


def declared_requirements(metadata, digest):
    """검증된 번들의 선언을 보여 주며 설치 여부나 무료 사용을 추정하지 않는다."""
    result = {"items": [], "external_api": "unknown", "cost_status": "unknown", "setup_verified": False}
    if inspect_bundle(metadata, digest)["integrity_status"] != "verified":
        return result
    files = metadata.get("bundle", {}).get("files", {})
    name = next((key for key in files if key.lower() == "skill.md"), None)
    if name is None:
        return result
    try:
        text = base64.b64decode(files[name], validate=True).decode("utf-8-sig")[:64000]
    except (ValueError, TypeError, UnicodeError):
        return result

    def add(kind, label, match):
        """외부 명령과 키 값을 노출하지 않고 짧은 선언과 원문 위치를 남긴다."""
        if any(item["kind"] == kind and item["label"] == label for item in result["items"]):
            return
        result["items"].append({"kind": kind, "label": label, "verification": "declared",
            "evidence": {"path": name, "line": text.count("\n", 0, match.start()) + 1,
                "excerpt": " ".join(match.group(0).split())[:240]}})

    for match in CLI.finditer(text):
        before = text[max(0, match.start() - 20):match.start()]
        if (match.group(1).lower() not in {"no", "not", "any", "additional"}
                and not re.search(r"\b(?:not|never|no)\s*$", before, re.I)):
            add("cli", match.group(1) + " CLI 설치 필요", match)
    negative = list(NO_API.finditer(text))
    positive = [match for match in API.finditer(text) if not any(
        span.start() <= match.start() < span.end() for span in negative)]
    if positive:
        result.update(external_api="declared_required", cost_status="possible")
        add("external_api", "별도 모델 API 제공자 설정과 사용 비용 확인 필요", positive[0])
    elif negative:
        result["external_api"] = "declared_not_required"
    match = HOST.search(text)
    if match and not re.search(r"\b(?:not|never|no)\b", match.group(0), re.I):
        result["cost_status"] = "possible"
        add("host_model", "호스트 모델 사용량과 비용 확인 필요", match)
    match = PARENT_LINK.search(text)
    if match:
        add("package_files", "스킬 폴더 밖 참조 파일의 패키지 조건 확인 필요", match)
    return result
