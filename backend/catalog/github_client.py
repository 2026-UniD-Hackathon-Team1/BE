"""고정 GitHub 호스트의 GET 요청만 지원하는 수집 클라이언트."""
import base64
import binascii
import hashlib
import json
import re
import time
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from backend.catalog.config import RepositoryConfig, validate_repo_path

MAX_FILE_BYTES = 1_000_000
MAX_BUNDLE_BYTES = 4_000_000
MAX_FILES = 100
MAX_RESPONSE_BYTES = 8_000_000


class CollectionError(Exception):
    def __init__(self, code: str, *, retry_after: str | None = None, reset_at: str | None = None):
        """외부 응답 원문 대신 안전한 오류 코드와 재시도 안내만 보관한다."""
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after
        self.reset_at = reset_at


@dataclass(frozen=True)
class SkillBundle:
    folder: str
    files: dict[str, bytes]
    kind: str = "skill"


@dataclass(frozen=True)
class RepositorySnapshot:
    repository: str
    commit_sha: str
    metadata: dict
    bundles: list[SkillBundle]


def require_sha(value):
    """API 응답의 Git 객체 식별자가 고정 SHA인지 확인한다."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise CollectionError("invalid_git_sha")
    return value


class GithubClient:
    def __init__(self, token: str | None = None, *, transport=None, max_requests=50, timeout_seconds=120):
        """명시적으로 전달된 토큰만 사용하며 PC의 인증 설정은 읽지 않는다."""
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "galatalkka-catalog"}
        if not 1 <= max_requests <= 100 or not 1 <= timeout_seconds <= 120:
            raise ValueError("invalid collection budget")
        self.max_requests = max_requests
        self.request_count = 0
        self.deadline = time.monotonic() + timeout_seconds
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.client = httpx.Client(base_url="https://api.github.com", headers=headers,
                                   timeout=15, follow_redirects=False, trust_env=False, transport=transport)

    def close(self):
        """수집에 사용한 HTTP 연결을 닫는다."""
        self.client.close()

    def get(self, path: str, *, params=None):
        """응답 크기를 제한하고 인증 오류와 요청 제한을 구분해 JSON을 읽는다."""
        if (not path.startswith("/repos/") and path != "/search/repositories") or "?" in path or "#" in path:
            raise CollectionError("invalid_api_path")
        self.check_budget()
        if self.request_count >= self.max_requests:
            raise CollectionError("request_budget_exceeded")
        self.request_count += 1
        try:
            with self.client.stream("GET", path, params=params,
                                    timeout=min(15, max(0.1, self.deadline - time.monotonic()))) as response:
                status = response.status_code
                if status == 429 or (status == 403 and (
                    response.headers.get("X-RateLimit-Remaining") == "0" or response.headers.get("Retry-After")
                )):
                    raise CollectionError("rate_limited", retry_after=response.headers.get("Retry-After"),
                                          reset_at=response.headers.get("X-RateLimit-Reset"))
                if status in (401, 403):
                    raise CollectionError("authentication_failed" if status == 401 else "forbidden")
                if status != 200:
                    raise CollectionError(f"http_{status}")
                chunks, total = [], 0
                for chunk in response.iter_bytes():
                    self.check_budget()
                    total += len(chunk)
                    if total > MAX_RESPONSE_BYTES:
                        raise CollectionError("response_too_large")
                    chunks.append(chunk)
                self.check_budget()
                return json.loads(b"".join(chunks))
        except httpx.RequestError:
            raise CollectionError("network_error") from None
        except (ValueError, UnicodeError):
            raise CollectionError("invalid_json") from None

    def check_budget(self):
        """응답 대기와 파일 읽기를 포함한 전체 수집의 제한 시간을 검사한다."""
        if time.monotonic() >= self.deadline:
            raise CollectionError("collection_deadline_exceeded")

    def discover(self, query: str, limit: int) -> list[RepositoryConfig]:
        """작업 관련 공개 검색에서 중복 없는 저장소 후보를 제한된 수만큼 발견한다."""
        if not 1 <= limit <= 20 or not query.strip() or len(query) > 200:
            raise CollectionError("invalid_search")
        data = self.get("/search/repositories", params={"q": query + " is:public", "per_page": limit})
        try:
            if data["incomplete_results"] is not False:
                raise CollectionError("incomplete_search")
            items = data["items"]
            if not isinstance(items, list) or len(items) > limit:
                raise CollectionError("invalid_search_payload")
            found = {}
            for item in items:
                if item.get("private") is not False or item.get("archived") is not False:
                    continue
                config = RepositoryConfig(repository=item["full_name"])
                found[config.repository] = config
            return list(found.values())
        except (KeyError, TypeError, ValueError, AttributeError):
            raise CollectionError("invalid_search_payload") from None

    def snapshot(self, config: RepositoryConfig) -> RepositorySnapshot:
        """한 커밋에서 발견한 스킬과 일반 저장소 정보를 읽고 무결성을 확인한다."""
        prefix = f"/repos/{config.repository}"
        try:
            repo = self.get(prefix)
            if repo.get("private") is not False:
                raise CollectionError("public_repository_required")
            branch = repo["default_branch"]
            if not isinstance(branch, str) or not branch:
                raise CollectionError("invalid_default_branch")
            commits = self.get(f"{prefix}/commits", params={"sha": branch, "per_page": 1})
            commit = commits[0]
            sha = require_sha(commit["sha"])
            tree_sha = require_sha(commit["commit"]["tree"]["sha"])
            tree = self.get(f"{prefix}/git/trees/{tree_sha}", params={"recursive": "1"})
            if tree.get("truncated") is not False:
                raise CollectionError("truncated_tree")
            entries = tree["tree"]
            if not isinstance(entries, list):
                raise CollectionError("invalid_tree")
            releases = self.get(f"{prefix}/releases", params={"per_page": 5})
            if not isinstance(releases, list):
                raise CollectionError("invalid_releases")
            metadata = {
                "description": repo.get("description"), "default_branch": branch,
                "language": repo.get("language"), "archived": repo.get("archived", False),
                "releases": [{"tag_name": release["tag_name"], "published_at": release.get("published_at")}
                             for release in releases],
            }
            folders = config.skill_folders or sorted({
                item["path"].rsplit("/", 1)[0] if "/" in item["path"] else "."
                for item in entries if item["path"].split("/")[-1] == "SKILL.md"
            })
            if len(folders) > 20:
                raise CollectionError("too_many_skills")
            bundles = [self._bundle(prefix, sha, folder, entries) for folder in folders]
            readmes = [item for item in entries if item["path"].lower() == "readme.md"]
            files = {item["path"]: self._read_file(prefix, sha, item) for item in readmes}
            bundles.append(SkillBundle(".", files, kind="repo"))
            return RepositorySnapshot(config.repository, sha, metadata, bundles)
        except (KeyError, TypeError, IndexError, AttributeError, ValueError):
            raise CollectionError("invalid_payload") from None

    def _bundle(self, prefix, sha, folder, entries) -> SkillBundle:
        """폴더의 일반 파일만 읽고 경로, 파일 수와 크기 및 Git 해시를 검증한다."""
        folder_prefix = "" if folder == "." else folder + "/"
        selected = []
        seen = set()
        total = 0
        for entry in entries:
            path = entry["path"]
            validate_repo_path(path)
            if not path.startswith(folder_prefix) or entry["type"] == "tree":
                continue
            if path in seen:
                raise CollectionError("duplicate_tree_path")
            seen.add(path)
            if entry["type"] != "blob" or entry["mode"] not in ("100644", "100755"):
                raise CollectionError("unsupported_file_type")
            size = entry["size"]
            if not isinstance(size, int) or not 0 <= size <= MAX_FILE_BYTES:
                raise CollectionError("file_too_large")
            total += size
            if len(selected) >= MAX_FILES or total > MAX_BUNDLE_BYTES:
                raise CollectionError("bundle_too_large")
            selected.append(entry)
        if folder_prefix + "SKILL.md" not in seen:
            raise CollectionError("missing_skill_manifest")
        files = {}
        for entry in sorted(selected, key=lambda item: item["path"]):
            path = entry["path"]
            files[path[len(folder_prefix):]] = self._read_file(prefix, sha, entry)
        try:
            files["SKILL.md"].decode("utf-8")
        except UnicodeError:
            raise CollectionError("invalid_manifest_encoding") from None
        return SkillBundle(folder, files)

    def _read_file(self, prefix, sha, entry):
        """고정 커밋의 일반 파일을 크기와 Git blob 해시까지 확인해 반환한다."""
        path = entry["path"]
        validate_repo_path(path)
        if entry.get("type") != "blob" or entry.get("mode") not in ("100644", "100755"):
            raise CollectionError("unsupported_file_type")
        if not isinstance(entry["size"], int) or not 0 <= entry["size"] <= MAX_FILE_BYTES:
            raise CollectionError("file_too_large")
        data = self.get(f"{prefix}/contents/{quote(path, safe='/')}", params={"ref": sha})
        if data.get("type") != "file" or data.get("encoding") != "base64" or data.get("path") != path:
            raise CollectionError("invalid_file_payload")
        try:
            content = base64.b64decode("".join(data["content"].split()), validate=True)
        except (binascii.Error, ValueError, TypeError):
            raise CollectionError("invalid_base64") from None
        if len(content) != entry["size"]:
            raise CollectionError("file_size_mismatch")
        blob_sha = hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()
        if blob_sha != require_sha(entry["sha"]) or blob_sha != require_sha(data["sha"]):
            raise CollectionError("file_hash_mismatch")
        return content
