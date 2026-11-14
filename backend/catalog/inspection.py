"""고정 번들의 무결성과 선언된 도구만 확인하며 외부 코드를 실행하지 않는다."""
import base64
import hashlib
import json
import re
from urllib.parse import urlsplit

from backend.catalog.config import validate_repo_path

TOOL_KINDS = {"Read": "file_read", "Write": "file_write", "Edit": "file_write",
              "Bash": "command_exec", "WebFetch": "network", "WebSearch": "network"}
PERMISSION_LABELS = {"file_read": "문서에 파일 읽기 도구 사용이 선언돼 있습니다.",
    "file_write": "문서에 파일 쓰기 도구 사용이 선언돼 있습니다.",
    "command_exec": "문서에 명령 실행 도구 사용이 선언돼 있습니다.",
    "network": "문서에 외부 네트워크 도구 사용이 선언돼 있습니다."}


def public_url(value):
    """출처 링크에서 로컬 경로와 URL 인증 정보 및 쿼리 비밀값을 제외한다."""
    if not isinstance(value, str) or any(ord(char) < 32 for char in value):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.hostname in {"localhost", "127.0.0.1", "::1"}):
            return None
        parsed.port
    except ValueError:
        return None
    return value


def verified_source(metadata, revision, source_kind, source_url, source_base):
    """수집 확인 플래그와 원본 저장소의 고정 커밋 URL을 함께 확인한다."""
    base = public_url(source_base)
    pinned = (base or "").rstrip("/") + f"/tree/{revision}"
    return bool(metadata.get("demo") is not True and metadata.get("source_verified") is True
        and source_kind == "github_repo" and re.fullmatch(r"[a-f0-9]{40}", revision)
        and base and source_url and urlsplit(source_url).hostname == "github.com"
        and (source_url == pinned or source_url.startswith(pinned + "/")))


def tool_tokens(value):
    """괄호와 인용문 내부는 유지하며 공백 및 쉼표로 도구 선언을 나눈다."""
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    elif len(value) >= 2 and value[0] in {"'", '"'} and value[-1] == value[0]:
        value = value[1:-1]
    tokens, current, depth, quote, escaped, valid = [], [], 0, None, False, True
    for char in value:
        if escaped:
            escaped = False
        elif quote:
            if char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in {"'", '"'}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            valid = valid and depth > 0
            depth = max(0, depth - 1)
        elif depth == 0 and (char.isspace() or char == ","):
            if current:
                tokens.append("".join(current))
                current = []
            continue
        current.append(char)
    if current:
        tokens.append("".join(current))
    return tokens, valid and depth == 0 and quote is None and not escaped


def permission_declarations(content):
    """제한된 frontmatter의 allowed-tools에서 권한 종류와 근거 위치만 추출한다."""
    result = {"declared": [], "verification": "unknown"}
    try:
        if len(content) > 256 * 1024:
            return result
        lines = content.decode("utf-8-sig").splitlines()
    except UnicodeDecodeError:
        return result
    if not lines or lines[0].strip() != "---":
        return result
    end = next((index for index in range(1, min(len(lines), 101)) if lines[index].strip() == "---"), None)
    if end is None:
        return result
    declarations, found, partial, multiline = {}, False, False, False
    for index, line in enumerate(lines[1:end], 2):
        match = re.match(r"^allowed-tools\s*:\s*(.*)$", line, re.I)
        if match:
            found = True
            value = match.group(1).strip()
            multiline = not value
        elif multiline and re.match(r"^\s+-\s+", line):
            value = re.sub(r"^\s+-\s+", "", line)
        else:
            multiline = multiline and (not line.strip() or line.startswith(" "))
            continue
        if not value:
            continue
        if len(value) > 500:
            partial = True
            continue
        tokens, complete = tool_tokens(value)
        partial = partial or not complete
        for token in tokens:
            token = token.strip(" []'\"")
            if not token:
                continue
            name = re.match(r"^([A-Za-z][A-Za-z0-9_-]*)(?:\s*$|\()", token)
            kind = TOOL_KINDS.get(name.group(1)) if name else None
            if kind is None:
                partial = True
                continue
            reference = {"path": "SKILL.md", "line": index}
            if reference not in declarations.setdefault(kind, []):
                declarations[kind].append(reference)
    if found:
        result["verification"] = "partial" if partial or multiline and not declarations else "declared"
        result["declared"] = [{"kind": kind, "description": PERMISSION_LABELS[kind],
            "additional": None, "evidence": evidence} for kind, evidence in sorted(declarations.items())]
    return result


def inspect_bundle(metadata, expected_hash):
    """번들 해시를 확인한 뒤 manifest 선언과 실행 파일 존재만 정적으로 확인한다."""
    result = {"integrity_status": "unknown", "permissions": {"declared": [], "verification": "unknown"},
              "manifest_present": False, "executable_files": False}
    payload = metadata.get("bundle")
    if not isinstance(payload, dict):
        return result
    try:
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode()
        if hashlib.sha256(encoded).hexdigest() != expected_hash:
            result["integrity_status"] = "mismatch"
            return result
        files = payload["files"]
        if not isinstance(files, dict):
            raise ValueError
        for path, content in files.items():
            validate_repo_path(path)
            if not isinstance(content, str):
                raise ValueError
        result["integrity_status"] = "verified"
        result["executable_files"] = any(path.lower().endswith((".py", ".sh", ".ps1", ".cmd", ".bat", ".js", ".mjs", ".exe")) for path in files)
        if "SKILL.md" in files:
            result["manifest_present"] = True
            result["permissions"] = permission_declarations(base64.b64decode(files["SKILL.md"], validate=True))
    except (ValueError, TypeError, KeyError, RecursionError):
        result["integrity_status"] = "invalid"
        result["permissions"] = {"declared": [], "verification": "unknown"}
    return result
