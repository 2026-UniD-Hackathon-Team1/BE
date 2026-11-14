"""고정 GitHub 호스트의 GET 요청만 지원하는 수집 클라이언트."""
import base64
import binascii
import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from backend.catalog.config import FOLDER_HINT_KEYS, RepositoryConfig, is_fixture_folder, validate_repo_path
from backend.catalog.dimensions import RULES
from backend.catalog.metadata import repository_facts

MAX_FILE_BYTES = 1_000_000
MAX_BUNDLE_BYTES = 4_000_000
MAX_FILES = 100
MAX_RESPONSE_BYTES = 8_000_000

# 폴더명은 읽기 순서를 정할 약한 신호이며 후보의 기능과 호환성 태그에는 쓰지 않는다.
FOLDER_ALIASES = {
    "code_review": ("review", "reviews", "reviewer", "reviewers", "audit", "리뷰", "코드리뷰"),
    "test_generation": ("test", "tests", "testing", "tdd", "테스트"),
    "input_validation": ("validation", "validator", "validate", "입력검증"),
    "exception_handling": ("exception", "exceptions", "error-handling", "예외처리"),
    "regression_test": ("regression", "회귀테스트"),
    "test_coverage": ("coverage", "커버리지"),
    "documentation": ("documentation", "docs", "doc", "문서화"),
    "api_security": ("security", "auth", "authentication", "authorization", "oauth", "owasp", "보안"),
    "accessibility": ("accessibility", "a11y", "wcag", "접근성"),
    "performance": ("performance", "perf", "optimization", "optimisation", "profiling", "성능"),
    "browser_testing": ("browser", "playwright", "cypress", "e2e", "브라우저테스트"),
    "test_design": ("test-design", "testing-strategy", "test-plan", "boundary", "테스트설계"),
    "database_changes": ("database", "db", "migration", "migrations", "schema", "sql", "마이그레이션"),
    "typing": ("typing", "types", "type-check", "type-hints", "type-safety", "mypy", "타입검사"),
    "python": ("python", "py", "파이썬"),
    "javascript": ("javascript", "java-script", "js", "자바스크립트"),
    "typescript": ("typescript", "type-script", "ts", "타입스크립트"),
    "rust": ("rust", "rustlang", "러스트"),
    "go": ("go", "golang", "고랭"),
    "java": ("java", "자바"),
    "csharp": ("csharp", "c-sharp", "cs", "c#"),
    "cpp": ("cpp", "c++", "cplusplus"),
    "ruby": ("ruby", "루비"),
    "php": ("php",),
    "kotlin": ("kotlin", "코틀린"),
    "swift": ("swift", "스위프트"),
    "sql": ("sql",),
    "fastapi": ("fastapi", "fast-api"),
    "react": ("react", "reactjs", "react.js", "리액트"),
    "nextjs": ("nextjs", "next.js", "next-js"),
    "vue": ("vue", "vuejs", "vue.js"),
    "nuxt": ("nuxt", "nuxtjs", "nuxt.js"),
    "express": ("express", "expressjs", "express.js"),
    "nestjs": ("nestjs", "nest.js", "nest-js"),
    "spring": ("spring", "springboot", "spring-boot", "스프링"),
    "codex_cli": ("codex", "codex-cli", "코덱스"),
    "claude_code": ("claude", "claude-code", "클로드"),
    "github_copilot": ("copilot", "github-copilot", "코파일럿"),
    "github_actions": ("github-actions", "actions"),
    "pytest": ("pytest", "py-test"),
}
FOLDER_EXACT_ALIASES = {
    "code_review": ("code-review", "code-reviewer", "review-code", "review-pr", "pr-review",
        "pull-request-review", "코드리뷰"),
    "test_generation": ("test-generation", "test-driven", "tdd", "generate-tests", "write-tests",
        "unit-testing", "unit-tests", "테스트생성"),
    "input_validation": ("input-validation", "input-validator", "request-validation", "입력검증"),
    "exception_handling": ("exception-handling", "error-handling", "예외처리"),
    "regression_test": ("regression-test", "regression-tests", "regression-testing", "회귀테스트"),
    "test_coverage": ("test-coverage", "code-coverage", "테스트커버리지"),
    "documentation": ("documentation", "문서화"),
    "api_security": ("api-security", "api-authentication", "api-authorization", "security-review",
        "auth-implementation", "authentication", "authorization", "oauth", "owasp", "appsec"),
    "accessibility": ("accessibility", "a11y", "wcag", "접근성"),
    "performance": ("performance", "profiling", "optimization", "성능"),
    "browser_testing": ("browser-testing", "browser-automation", "e2e-testing", "playwright", "cypress"),
    "test_design": ("test-design", "testing-strategy", "test-plan", "boundary-testing", "테스트설계"),
    "database_changes": ("database-migration", "schema-migration", "migrations", "마이그레이션"),
    "typing": ("type-safety", "type-checking", "type-check", "type-hints", "static-typing", "mypy", "타입검사"),
}


def _folder_words(value):
    """구분자와 CamelCase를 단어로 나눠 go를 goals 같은 부분 문자열과 구별한다."""
    value = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value)
    return " ".join(re.findall(r"[a-z0-9]+(?:\+\+|#)?|[가-힣]+", value.lower()))


def folder_priority(folder, hints):
    """정확한 작업명과 검사명을 일반 단어보다 우선하되 원문 지원 근거로 해석하지 않는다."""
    words = " " + _folder_words(folder) + " "
    exact_tasks, exact_capabilities, score = 0, 0, 0
    for hint in dict.fromkeys(hints):
        exact = any(" " + _folder_words(alias) + " " in words for alias in FOLDER_EXACT_ALIASES.get(hint, ()))
        generic = any(" " + _folder_words(alias) + " " in words for alias in FOLDER_ALIASES.get(hint, (hint,)))
        if exact:
            if hint in {"code_review", "test_generation"}:
                exact_tasks += 1
                score += 16
            else:
                exact_capabilities += 1
                score += 12
        elif generic:
            score += 1 if hint in {"code_review", "test_generation"} else 4 if hint in RULES["capabilities"] else 2
    return -exact_tasks, -exact_capabilities, -score


def prioritize_folders(folders, hints):
    """모든 폴더를 보존하면서 정확한 작업과 목표 이름이 맞는 폴더부터 읽는다."""
    return sorted(folders, key=lambda folder: (*folder_priority(folder, hints), folder)) if hints else folders


def repository_hint_score(item, hints):
    """공개 검색 설명은 원문을 읽을 순서에만 사용하고 후보 기능의 증거로 저장하지 않는다."""
    name = item.get("name")
    if not isinstance(name, str):
        full_name = item.get("full_name", "")
        name = full_name.rsplit("/", 1)[-1] if isinstance(full_name, str) else ""
    description = item.get("description")
    description = description[:2000] if isinstance(description, str) else ""
    topics = item.get("topics")
    topics = [topic for topic in topics[:50] if isinstance(topic, str)
        and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,49}", topic)] if isinstance(topics, list) else []
    words = " " + _folder_words(" ".join([name[:200], description, *topics])) + " "
    score = 0
    for hint in hints:
        aliases = FOLDER_ALIASES.get(hint, (hint,))
        if any(" " + _folder_words(alias) + " " in words for alias in aliases):
            score += (4 if hint in {"code_review", "test_generation"} else
                3 if hint in RULES["capabilities"] else 1 if hint in RULES["tools"] else 2)
    return score


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


@dataclass(frozen=True)
class RepositoryPlan:
    repository: str
    state: dict
    entries: list[dict]


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
        self.last_quota = {}
        self.last_discovery_stats = {"source_pool_count": 0, "known_skipped": 0, "selected_count": 0}
        self.before_request = None
        self.observe_response = None
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
        if self.before_request:
            self.before_request()
        self.check_budget()
        self.request_count += 1
        try:
            with self.client.stream("GET", path, params=params,
                                    timeout=min(15, max(0.1, self.deadline - time.monotonic()))) as response:
                status = response.status_code
                self.last_quota = {key: int(value) for header, key in (
                    ("X-RateLimit-Limit", "limit"), ("X-RateLimit-Remaining", "remaining"),
                    ("X-RateLimit-Reset", "reset"), ("X-RateLimit-Used", "used"))
                    if (value := response.headers.get(header, "")).isdigit() and len(value) <= 12}
                if self.observe_response:
                    self.observe_response(status, response.headers)
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

    def discover(self, query: str, limit: int, *, sort="updated", hints=None,
                 exclude_repositories=None) -> list[RepositoryConfig]:
        """작업 관련 공개 검색에서 중복 없는 저장소 후보를 제한된 수만큼 발견한다."""
        self.last_discovery_stats = {"source_pool_count": 0, "known_skipped": 0, "selected_count": 0}
        if not 1 <= limit <= 20 or not query.strip() or len(query) > 200 or sort not in {"updated", "best_match"}:
            raise CollectionError("invalid_search")
        hints = [] if hints is None else hints
        if not isinstance(hints, list) or any(not isinstance(hint, str) or hint not in FOLDER_HINT_KEYS for hint in hints):
            raise CollectionError("invalid_search")
        hints = list(dict.fromkeys(hints))
        if len(hints) > 20:
            raise CollectionError("invalid_search")
        excluded = [] if exclude_repositories is None else exclude_repositories
        if not isinstance(excluded, (list, tuple, set, frozenset)):
            raise CollectionError("invalid_search")
        try:
            excluded = {RepositoryConfig(repository=value).repository for value in excluded}
        except (ValueError, TypeError):
            raise CollectionError("invalid_search") from None
        fetch_limit = max(limit, min(20, limit * 5)) if hints else limit
        params = {"q": query + " is:public", "per_page": fetch_limit}
        if sort == "updated":
            params.update(sort="updated", order="desc")
        data = self.get("/search/repositories", params=params)
        try:
            if data["incomplete_results"] is not False:
                raise CollectionError("incomplete_search")
            items = data["items"]
            if not isinstance(items, list) or len(items) > fetch_limit:
                raise CollectionError("invalid_search_payload")
            self.last_discovery_stats["source_pool_count"] = len(items)
            found = {}
            seen = set()
            for index, item in enumerate(items):
                if item.get("private") is not False or item.get("archived") is not False:
                    continue
                config = RepositoryConfig(repository=item["full_name"])
                if config.repository in seen:
                    continue
                seen.add(config.repository)
                if config.repository in excluded:
                    self.last_discovery_stats["known_skipped"] += 1
                    continue
                found[config.repository] = (config, repository_hint_score(item, hints), index)
            selected = [config for config, _, _ in sorted(found.values(), key=lambda row: (-row[1], row[2]))[:limit]]
            self.last_discovery_stats["selected_count"] = len(selected)
            return selected
        except (KeyError, TypeError, ValueError, AttributeError):
            raise CollectionError("invalid_search_payload") from None

    def prepare(self, config: RepositoryConfig, continuation=None) -> RepositoryPlan:
        """고정 커밋의 대상 목록을 준비하고 이어받을 때 최신 커밋과 섞지 않는다."""
        prefix = f"/repos/{config.repository}"
        try:
            if continuation and continuation["scope"] != sorted(config.skill_folders):
                raise CollectionError("collection_scope_conflict")
            repo = self.get(prefix)
            if repo.get("private") is not False:
                raise CollectionError("public_repository_required")
            branch = repo["default_branch"]
            if not isinstance(branch, str) or not branch:
                raise CollectionError("invalid_default_branch")
            if continuation:
                sha = require_sha(continuation["commit_sha"])
                tree_sha = require_sha(continuation["tree_sha"])
            else:
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
            for entry in entries:
                validate_repo_path(entry["path"])
            if len({entry["path"] for entry in entries}) != len(entries):
                raise CollectionError("duplicate_tree_path")
            if continuation:
                return RepositoryPlan(config.repository, dict(continuation), entries)
            releases = self.get(f"{prefix}/releases", params={"per_page": 5})
            if not isinstance(releases, list):
                raise CollectionError("invalid_releases")
            metadata = {
                "description": repo.get("description"), "default_branch": branch,
                "language": repo.get("language"), "archived": repo.get("archived", False),
                "facts": repository_facts(repo, commit),
                "releases": [{"tag_name": release["tag_name"], "published_at": release.get("published_at")}
                             for release in releases],
            }
            folders = config.skill_folders or sorted({
                item["path"].rsplit("/", 1)[0] if "/" in item["path"] else "."
                for item in entries if item["path"].split("/")[-1] == "SKILL.md"
            })
            if not config.skill_folders:
                folders = [folder for folder in folders if not is_fixture_folder(folder)]
                folders = prioritize_folders(folders, config.folder_hints)
            if len(folders) > 2000:
                raise CollectionError("too_many_skills")
            return RepositoryPlan(config.repository, {"commit_sha": sha, "tree_sha": tree_sha,
                "metadata": metadata, "metadata_observed_at": datetime.now(timezone.utc).isoformat(),
                "folders": folders, "folder_hints": list(config.folder_hints),
                "scope": sorted(config.skill_folders), "next": 0}, entries)
        except (KeyError, TypeError, IndexError, AttributeError, ValueError):
            raise CollectionError("invalid_payload") from None

    def unit_entries(self, plan, index):
        """한 번들에 필요한 파일을 미리 검증해 요청 예산을 계산한다."""
        folders = plan.state["folders"]
        if index < len(folders):
            return self._bundle_entries(folders[index], plan.entries)
        return [item for item in plan.entries if item["path"].lower() == "readme.md"]

    def read_unit(self, plan, index):
        """현재 커서의 번들 하나를 고정 원본에서 읽는다."""
        prefix = f"/repos/{plan.repository}"
        sha = plan.state["commit_sha"]
        if index < len(plan.state["folders"]):
            return self._bundle(prefix, sha, plan.state["folders"][index], plan.entries)
        files = {item["path"]: self._read_file(prefix, sha, item) for item in self.unit_entries(plan, index)}
        return SkillBundle(".", files, kind="repo")

    def snapshot(self, config: RepositoryConfig) -> RepositorySnapshot:
        """작은 저장소의 전체 스냅샷이 필요한 호출에 기존 인터페이스를 제공한다."""
        plan = self.prepare(config)
        if len(plan.state["folders"]) > 20:
            raise CollectionError("too_many_skills")
        bundles = [self.read_unit(plan, index) for index in range(len(plan.state["folders"]) + 1)]
        return RepositorySnapshot(config.repository, plan.state["commit_sha"], plan.state["metadata"], bundles)

    def _bundle(self, prefix, sha, folder, entries) -> SkillBundle:
        """폴더의 일반 파일만 읽고 경로, 파일 수와 크기 및 Git 해시를 검증한다."""
        folder_prefix = "" if folder == "." else folder + "/"
        selected = self._bundle_entries(folder, entries)
        files = {}
        for entry in sorted(selected, key=lambda item: item["path"]):
            path = entry["path"]
            files[path[len(folder_prefix):]] = self._read_file(prefix, sha, entry)
        try:
            files["SKILL.md"].decode("utf-8")
        except UnicodeError:
            raise CollectionError("invalid_manifest_encoding") from None
        return SkillBundle(folder, files)

    def _bundle_entries(self, folder, entries):
        """네트워크 호출 전에 폴더 경로와 파일 수 및 전체 크기를 확인한다."""
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
        return selected

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
