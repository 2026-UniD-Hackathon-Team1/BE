"""스킬이 사용한다고 명시한 번들 내부 참조만 읽어 기능과 연결 근거를 보완한다."""
import re
from collections import deque
from pathlib import PurePosixPath

from backend.catalog.dimensions import (COMPILED_RULES, FUTURE, NEGATIVE, SECRET_ASSIGNMENT,
    UNSUPPORTED_SECTION, _document_lines, _is_catalog, _primary_document, _prose, extract_dimensions)


MAX_FILES = 20
MAX_FILE_BYTES = 64 * 1024
MAX_DEPTH = 2
NON_FUNCTIONAL_SECTION = re.compile(r"^(?:\d+[.)]?\s*)?(?:inputs?|references?|resources?|examples?)\b|^(?:입력|참고|참조|예시)", re.I)
COMMAND = re.compile(r"^(?:\$|PS>|&|(?:python\d*|node|bash|sh|pwsh|powershell|cmd|pip|npm|npx|curl|wget)\s)", re.I)


def _markdown_lines(text):
    """코드 울타리와 주석을 실행하거나 해석하지 않고 원래 줄 번호의 본문만 반환한다."""
    fence, comment = None, False
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
            elif current[0] == fence[0] and len(current) >= len(fence) and not line[marker.end():].strip():
                fence = None
            continue
        if not fence and len(line) <= 4000 and not SECRET_ASSIGNMENT.search(line):
            yield number, line


def _safe_path(raw, source):
    """URL과 절대 경로 및 상위 이동을 거부하고 references 내부의 문자 경로만 정규화한다."""
    if (not isinstance(raw, str) or len(raw) > 256 or not raw
            or not re.fullmatch(r"[A-Za-z0-9_./#-]+", raw) or raw.startswith("/")
            or ".." in raw.split("/") or "." in raw.split("/")):
        return None
    path = raw.split("#", 1)[0].rstrip("/")
    if not path.startswith("references/"):
        if source.lower() == "skill.md":
            return None
        path = str(PurePosixPath(source).parent / path)
    if not path.startswith("references/") or PurePosixPath(path).name.lower() == "skill.md":
        return None
    return path


def _links(text, source, files):
    """본문의 리터럴 파일 링크와 기본 내장 규칙 폴더만 활성 참조로 선택한다."""
    if PurePosixPath(source).suffix.lower() in {".yaml", ".yml"}:
        scalar_indent = None
        for number, line in enumerate(text.splitlines(), 1):
            indent = len(line) - len(line.lstrip())
            if scalar_indent is not None:
                if not line.strip() or indent > scalar_indent:
                    continue
                scalar_indent = None
            if re.search(r":\s*[|>][+-]?\d?\s*(?:#.*)?$", line):
                scalar_indent = indent
                continue
            match = re.fullmatch(r"\s*(?:-\s*)?file\s*:\s*(?:'([^']+)'|\"([^\"]+)\"|([^\s#]+))\s*(?:#.*)?", line)
            if match:
                raw = next(value for value in match.groups() if value is not None)
                path = _safe_path(raw, source)
                if path and path.endswith(".md"):
                    yield number, path, "registry", line
        return
    blocked = None
    for number, line in _markdown_lines(text):
        heading = re.match(r"^ {0,3}(#{1,6})\s+(.*)$", line)
        if heading:
            depth, title = len(heading.group(1)), heading.group(2)
            if blocked is not None and depth <= blocked:
                blocked = None
            inactive = re.search(r"\b(?:unused|disabled|inactive|examples?|roadmap|future)\b|미사용|예시|예정", title, re.I)
            if inactive or (source.lower() != "skill.md" and NON_FUNCTIONAL_SECTION.search(title)):
                blocked = depth if blocked is None else min(blocked, depth)
        if blocked is not None:
            continue
        if COMMAND.search(line.strip()):
            continue
        semantic = re.sub(r"`[^`]*`|\[[^\]]*\]\([^)]*\)", "", line)
        if re.search(r"\b(?:do\s+not|don't|never|no\s+need\s+to)\s+(?:read|use|load|follow|include|apply)\b|"
                     r"\b(?:unused|disabled|inactive)\b|\bnot\s+(?:used|loaded|active|included)\b", semantic, re.I):
            continue
        literals = re.findall(r"`([^`\n]+)`", line)
        links = re.findall(r"\[[^\]]*\]\(([^\s)]+)\)", line)
        for raw, kind in [(value, "literal") for value in literals] + [(value, "markdown") for value in links]:
            path = _safe_path(raw, source)
            if not path:
                continue
            if path in files and PurePosixPath(path).suffix.lower() in {".md", ".yaml", ".yml"}:
                yield number, path, kind, line
            elif (not PurePosixPath(path).suffix and path != "references"
                    and re.search(r"\b(?:defaults?|built[ -]in)\b|기본|내장", line, re.I)):
                for child in sorted(files):
                    if (isinstance(child, str) and str(PurePosixPath(child).parent) == path
                            and child.endswith(".md") and _safe_path(child, source)):
                        yield number, child, "default_directory", line


def _unsupported_capabilities(text):
    """루트가 부정하거나 예정으로 남긴 기능을 참조 문서로 되살리지 않는다."""
    result, blocked = set(), None
    for _, line in _markdown_lines(text):
        heading = re.match(r"^ {0,3}(#{1,6})\s+(.*)$", line)
        if heading:
            depth, title = len(heading.group(1)), heading.group(2)
            if blocked is not None and depth <= blocked:
                blocked = None
            if NEGATIVE.search(title) or FUTURE.search(title) or UNSUPPORTED_SECTION.search(title):
                blocked = depth if blocked is None else min(blocked, depth)
        prose = _prose(line)
        if prose is None:
            continue
        for clause in re.split(r"(?<=[.!?])\s+|;\s*|\s+(?:but|whereas|however)\s+|하지만|반면", prose, flags=re.I):
            clause = re.sub(r"\bnot\s+only\b", "", clause, flags=re.I)
            if blocked is not None or NEGATIVE.search(clause) or FUTURE.search(clause):
                result.update(value for value, pattern in COMPILED_RULES["capabilities"].items() if pattern.search(clause))
    return result


def _reference_document(text):
    """입력과 참고 절 및 코드 예시는 빼고 규칙 표의 검사명과 설명만 원래 줄에 남긴다."""
    originals = text.splitlines()
    lines, spans = [""] * len(originals), {}
    blocked, table_columns = None, None
    for number, line in _markdown_lines(text):
        heading = re.match(r"^ {0,3}(#{1,6})\s+(.*)$", line)
        if heading:
            depth, title = len(heading.group(1)), heading.group(2)
            table_columns = None
            if blocked is not None and depth <= blocked:
                blocked = None
            if NON_FUNCTIONAL_SECTION.search(title):
                blocked = depth if blocked is None else min(blocked, depth)
        if blocked is not None:
            continue
        if line.strip().startswith("|"):
            cells = list(re.finditer(r"(?<=\|)([^|]*)(?=\|)", line))
            names = [cell.group(1).strip().lower() for cell in cells]
            if any(name in {"rule", "check", "check item", "principle", "criterion", "requirement"} for name in names):
                table_columns = [index for index, name in enumerate(names) if name in {
                    "rule", "check", "check item", "principle", "criterion", "requirement", "description"}]
                continue
            if not table_columns or all(re.fullmatch(r"[: -]*", name) for name in names):
                continue
            selected = [cells[index] for index in table_columns if index < len(cells)]
            lines[number - 1] = " ".join(cell.group(1).strip() for cell in selected)
            spans[number] = [(cell.start(1), cell.end(1)) for cell in selected]
        else:
            table_columns = None
            lines[number - 1] = line
            spans[number] = [(0, len(line))]
    return "\n".join(lines), spans


def _reference_evidence(item, path, text, spans):
    """변환한 규칙 문구 대신 원본 참조 파일의 실제 셀 또는 문장을 근거로 반환한다."""
    line = text.splitlines()[item["line"] - 1]
    pattern = COMPILED_RULES["capabilities"][item["value"]]
    for start, end in spans.get(item["line"], []):
        cell = line[start:end]
        if item["excerpt"] in cell:
            return {**item, "path": path}
        match = pattern.search(cell)
        if match and item["value"] in extract_dimensions({"README.md": cell.encode("utf-8")})["capabilities"]:
            offset = max(0, match.start() - 80)
            return {**item, "path": path, "excerpt": cell[offset:offset + 240]}
    return None


def enrich_referenced_capabilities(files, dimensions):
    """검증된 번들 인자로부터 명시된 참조 그래프만 따라 기능과 출처 연결을 보완한다."""
    result = {**dimensions, "capabilities": list(dimensions["capabilities"]),
        "evidence": list(dimensions["evidence"]), "reference_links": [],
        "reference_scan": {"max_files": MAX_FILES, "max_depth": MAX_DEPTH,
            "max_file_bytes": MAX_FILE_BYTES, "read_files": 0, "truncated": False}}
    primary = _primary_document(files)
    if primary is None or primary[0].lower() != "skill.md":
        return result
    source, content = primary
    try:
        text = content.decode("utf-8-sig")
    except (UnicodeError, AttributeError):
        return result
    if _is_catalog(text):
        return result
    unsupported = _unsupported_capabilities(text)
    queue = deque([(source, text, 0, None)])
    visited = set()
    while queue:
        origin, document, depth, root = queue.popleft()
        if depth >= MAX_DEPTH:
            if any(path not in visited for _, path, _, _ in _links(document, origin, files)):
                result["reference_scan"]["truncated"] = True
            continue
        for number, path, kind, line in _links(document, origin, files):
            if path in visited:
                continue
            if len(visited) >= MAX_FILES:
                result["reference_scan"]["truncated"] = True
                continue
            content = files.get(path)
            if not isinstance(content, bytes) or len(content) > MAX_FILE_BYTES:
                result["reference_scan"]["truncated"] = True
                continue
            try:
                referenced = content.decode("utf-8-sig")
            except UnicodeError:
                continue
            visited.add(path)
            root_link = root or {"source_path": origin, "source_line": number}
            result["reference_links"].append({**root_link, "from_path": origin, "line": number,
                "path": path, "kind": kind, "depth": depth + 1, "excerpt": line[:240]})
            if _is_catalog(referenced):
                continue
            queue.append((path, referenced, depth + 1, root_link))
            if not path.endswith(".md"):
                continue
            prepared, spans = _reference_document(referenced)
            extracted = extract_dimensions({"README.md": prepared.encode("utf-8")})
            for item in extracted["evidence"]:
                if item["dimension"] != "capabilities" or item["value"] in unsupported:
                    continue
                mapped = _reference_evidence(item, path, referenced, spans)
                if mapped and item["value"] not in result["capabilities"]:
                    result["capabilities"].append(item["value"])
                    result["evidence"].append(mapped)
    result["capabilities"] = sorted(set(result["capabilities"]) - unsupported)
    result["evidence"] = [item for item in result["evidence"]
        if item["dimension"] != "capabilities" or item["value"] in result["capabilities"]]
    result["reference_scan"]["read_files"] = len(visited)
    return result
