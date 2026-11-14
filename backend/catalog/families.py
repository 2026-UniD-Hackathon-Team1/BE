"""같은 원본의 스킬 미러를 묶되 선언된 이름을 실행 승인이나 출처 검증에 쓰지 않는다."""
import base64
import binascii
import hashlib
import re


MAX_MANIFEST_BYTES = 256 * 1024
MAX_FRONTMATTER_LINES = 128
NAME_KEY = re.compile(r"^(?:name|'name'|\"name\")\s*:\s*(.*)$")
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


def _get(value, key, default=None):
    """ORM 객체와 순수 사전 입력에서 같은 속성을 읽는다."""
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _identifier(value):
    """서버 식별자가 짧고 비어 있지 않으며 제어문자를 포함하지 않는지 확인한다."""
    return isinstance(value, str) and 0 < len(value) <= 512 and not any(ord(char) < 32 for char in value)


def _scalar_name(value):
    """짧은 일반 문자열과 단순 따옴표 이름만 읽고 YAML 객체와 명령 구문은 거부한다."""
    match = re.fullmatch(r"(?:([A-Za-z0-9][A-Za-z0-9_-]{0,63})|'([^']*)'|\"([^\"]*)\")"
        r"(?:\s+#.*)?\s*", value)
    if not match:
        return None
    plain = match.group(1)
    if plain is not None and (plain.lower() in {"null", "true", "false", "yes", "no", "on", "off"}
            or re.fullmatch(r"\d+|0[xob][0-9a-f_]+|\d{4}-\d{2}-\d{2}", plain, re.I)):
        return None
    name = next((part for part in match.groups() if part is not None), "")
    return name if SAFE_NAME.fullmatch(name) else None


def _manifest_name(version):
    """루트 manifest의 제한된 frontmatter에서 모호하지 않은 name 하나만 추출한다."""
    metadata = _get(version, "metadata_json", {})
    bundle = metadata.get("bundle", {}) if isinstance(metadata, dict) else {}
    files = bundle.get("files", {}) if isinstance(bundle, dict) else {}
    if not isinstance(files, dict):
        return None
    candidates = [value for path, value in files.items() if isinstance(path, str) and path.lower() == "skill.md"]
    if len(candidates) != 1:
        return None
    encoded = candidates[0]
    if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_MANIFEST_BYTES + 2) // 3):
        return None
    try:
        content = base64.b64decode(encoded, validate=True)
        if len(content) > MAX_MANIFEST_BYTES:
            return None
        lines = content.decode("utf-8-sig").splitlines()
    except (binascii.Error, UnicodeError, ValueError):
        return None
    if not lines or lines[0].strip() != "---":
        return None
    closing = next((index for index in range(1, min(len(lines), MAX_FRONTMATTER_LINES + 1))
        if lines[index].strip() in {"---", "..."}), None)
    if closing is None:
        return None
    values = [match.group(1) for line in lines[1:closing] if (match := NAME_KEY.match(line))]
    if len(values) != 1:
        return None
    return _scalar_name(values[0])


def _family_key(basis, *values):
    """개인 경로나 원문 이름을 공개 키에 노출하지 않고 결정적인 식별자를 만든다."""
    payload = "\x00".join(("candidate-family-v1", basis, *values))
    return "family_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def candidate_family(asset, version):
    """같은 소스의 선언 이름 계열과 별도 번들 해시를 반환하며 신뢰 수준은 부여하지 않는다."""
    asset_id, source_id = _get(asset, "id"), _get(asset, "source_id")
    if not _identifier(asset_id):
        raise ValueError("candidate family requires an asset id")
    belongs_to_asset = _get(version, "asset_id", asset_id) == asset_id
    name = _manifest_name(version) if belongs_to_asset and _identifier(source_id) else None
    if name:
        key, basis = _family_key("source_manifest_name", source_id, name), "source_manifest_name"
    else:
        key, basis = _family_key("asset_id", asset_id), "asset_id"
    digest = _get(version, "bundle_sha256")
    digest = digest.lower() if isinstance(digest, str) and re.fullmatch(r"[a-fA-F0-9]{64}", digest) else None
    return {"family_key": key, "basis": basis, "declared_name": name, "bundle_sha256": digest}


def canonical_family(asset, version):
    """이름이 모호하면 자산 단위로 유지하고 확인된 같은 소스의 미러만 한 계열로 묶는다."""
    return candidate_family(asset, version)["family_key"]
