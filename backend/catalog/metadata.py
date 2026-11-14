"""원본 메타데이터를 제한된 공개 사실로 정규화한다."""
import base64
import re
from datetime import datetime, timezone


def timestamp(value):
    """시간대가 명시된 유효한 원본 시각만 UTC로 변환한다."""
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (ValueError, OverflowError):
        return None


def license_label(value):
    """문서나 API가 명시한 단순 라이선스 표기만 표시용으로 허용한다."""
    if (isinstance(value, str) and value not in {"NOASSERTION", "NONE"}
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+-]{0,79}", value)):
        return value
    return None


def repository_facts(repo, commit):
    """게시자와 저장소 정보 및 커밋 시각을 구분하고 개인 이메일은 저장하지 않는다."""
    owner = repo.get("owner")
    login = owner.get("login") if isinstance(owner, dict) else None
    login = publisher_login(login)
    license_info = repo.get("license")
    details = commit.get("commit", {})
    committer = details.get("committer") if isinstance(details, dict) else None
    return {"publisher": login, "publisher_url": "https://github.com/" + login if login else None,
        "repository_license": license_label(license_info.get("spdx_id")) if isinstance(license_info, dict) else None,
        "repository_created_at": timestamp(repo.get("created_at")),
        "repository_pushed_at": timestamp(repo.get("pushed_at")),
        "revision_committed_at": timestamp(committer.get("date")) if isinstance(committer, dict) else None}


def publisher_login(value):
    """프로필 경로나 인증 정보를 섞을 수 없는 게시 계정 식별자만 허용한다."""
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,99}", value) else None


def public_facts(metadata, verified):
    """상세와 추천에서 같은 정규화 규칙으로 수집 사실만 제공한다."""
    repository = metadata.get("repository", {})
    facts = repository.get("facts", {}) if isinstance(repository, dict) and verified else {}
    facts = facts if isinstance(facts, dict) else {}
    login = publisher_login(facts.get("publisher"))
    return {"publisher": login, "publisher_url": "https://github.com/" + login if login else None,
        "repository_license": license_label(facts.get("repository_license")),
        **{key: timestamp(facts.get(key)) for key in (
            "repository_created_at", "repository_pushed_at", "revision_committed_at")},
        "metadata_observed_at": timestamp(metadata.get("metadata_observed_at")) if verified else None}


def manifest_license(metadata):
    """무결성 확인 후 호출하며 단순 manifest 라이선스 선언만 읽는다."""
    try:
        encoded = metadata["bundle"]["files"]["SKILL.md"]
        if not isinstance(encoded, str) or len(encoded) > 350000:
            return None
        lines = base64.b64decode(encoded, validate=True).decode("utf-8-sig").splitlines()
    except (KeyError, TypeError, ValueError, UnicodeError):
        return None
    if not lines or lines[0].strip() != "---":
        return None
    end = next((index for index in range(1, min(101, len(lines))) if lines[index].strip() == "---"), None)
    if end is None:
        return None
    found = [line.split(":", 1)[1].strip() for line in lines[1:end]
             if re.match(r"^license\s*:", line)]
    if len(found) != 1:
        return None
    value = found[0]
    if value.startswith(("'", '"')):
        if len(value) < 2 or value[-1] != value[0]:
            return None
        value = value[1:-1]
    return license_label(value)
