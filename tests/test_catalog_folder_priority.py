"""원격 요청과 DB 없이 고정 커밋의 폴더 수집 순서를 검증한다."""
import httpx
import pytest
from pydantic import ValidationError

from backend.catalog.config import CollectionConfig, FOLDER_HINT_KEYS, RepositoryConfig
from backend.catalog.github_client import GithubClient


class FolderRemote:
    def __init__(self, folders):
        """문서 원문을 읽지 않는 저장소 메타데이터와 트리만 준비한다."""
        self.folders = folders
        self.sha = "a" * 40
        self.requests = []

    def handle(self, request):
        """목록 준비에 필요한 GitHub 모양의 응답을 메모리에서 반환한다."""
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/commits"):
            data = [{"sha": self.sha, "commit": {"tree": {"sha": "b" * 40}}}]
        elif "/git/trees/" in path:
            data = {"truncated": False, "tree": [{"path": (folder + "/" if folder != "." else "") + "SKILL.md",
                "type": "blob", "mode": "100644", "size": 10, "sha": "c" * 40} for folder in self.folders]}
        elif path.endswith("/releases"):
            data = []
        elif "/contents/" in path:
            pytest.fail("폴더 순서를 정할 때 문서 원문을 요청하면 안 됩니다.")
        else:
            data = {"private": False, "default_branch": "main", "description": "Fixture"}
        return httpx.Response(200, json=data)


@pytest.fixture
def remote_client():
    """테스트에서 만든 모의 HTTP 클라이언트만 마지막에 정리한다."""
    clients = []

    def create(folders):
        """필요한 폴더 이름만 가진 순수 테스트 클라이언트를 만든다."""
        remote = FolderRemote(folders)
        client = GithubClient(transport=httpx.MockTransport(remote.handle))
        clients.append(client)
        return client, remote

    yield create
    for client in clients:
        client.close()


def test_no_hints_keeps_existing_alphabetical_order(remote_client):
    """관심 기술을 지정하지 않으면 기존 자동 발견 순서가 유지된다."""
    folders = ["skills/z-security", "skills/a-docs", ".", "skills/b-review"]
    client, _ = remote_client(folders)
    plan = client.prepare(RepositoryConfig(repository="example/skills"))
    assert plan.state["folders"] == sorted(folders)
    assert plan.state["folder_hints"] == []


def test_matching_folder_is_first_without_removing_or_duplicating_others(remote_client):
    """관심 기술에 맞는 폴더를 먼저 읽되 다른 후보도 계속 수집 대상으로 남긴다."""
    folders = ["skills/a-docs", "skills/b-planning", "skills/z-api-security", "skills/y-auth"]
    client, _ = remote_client(folders)
    plan = client.prepare(RepositoryConfig(repository="example/skills", folder_hints=["api_security", "api_security"]))
    assert plan.state["folders"] == ["skills/y-auth", "skills/z-api-security", "skills/a-docs", "skills/b-planning"]
    assert set(plan.state["folders"]) == set(folders)
    assert len(plan.state["folders"]) == len(folders)
    assert plan.state["folder_hints"] == ["api_security"]


def test_multiple_hints_and_camel_case_give_relevant_bundle_priority(remote_client):
    """언어와 프레임워크와 검사 목적을 함께 만족하는 이름을 우선한다."""
    folders = ["skills/a-security", "skills/b-python", "skills/z-pythonFastapiSecurity", "skills/docs"]
    client, _ = remote_client(folders)
    plan = client.prepare(RepositoryConfig(repository="example/skills",
        folder_hints=["python", "fastapi", "api_security"]))
    assert plan.state["folders"] == ["skills/z-pythonFastapiSecurity", "skills/a-security", "skills/b-python", "skills/docs"]


@pytest.mark.parametrize("hint,unrelated,related", [
    ("go", "goals", "go-review"), ("java", "javascript", "java-review"),
    ("react", "reactive-design", "react-review"), ("sql", "nosql-guide", "sql-review"),
])
def test_hint_matches_tokens_instead_of_substrings(remote_client, hint, unrelated, related):
    """짧은 기술명을 다른 단어의 부분 문자열과 혼동하지 않는다."""
    folders = ["skills/a-" + unrelated, "skills/z-" + related]
    client, _ = remote_client(folders)
    plan = client.prepare(RepositoryConfig(repository="example/skills", folder_hints=[hint]))
    assert plan.state["folders"] == list(reversed(folders))


@pytest.mark.parametrize("hint,folder", [
    ("code_review", "security-audit"), ("test_generation", "tdd-guide"),
    ("input_validation", "validator"), ("exception_handling", "error-handling"),
    ("regression_test", "regression"), ("test_coverage", "coverage"),
    ("documentation", "docs"), ("api_security", "oauth"), ("accessibility", "a11y"),
    ("performance", "profiling"), ("browser_testing", "playwright"),
    ("test_design", "testing-strategy"), ("database_changes", "schema-migrations"),
    ("typing", "mypy"), ("nextjs", "next.js"), ("codex_cli", "codex"),
    ("claude_code", "claude-code"), ("typescript", "ts"), ("cpp", "c++"),
    ("typescript", "TypeScript"), ("fastapi", "FastAPI"), ("pytest", "PyTest"),
])
def test_known_aliases_prioritize_folder_names_only(remote_client, hint, folder):
    """약한 폴더 별칭은 목록 준비에만 사용하고 원문을 읽거나 태그를 만들지 않는다."""
    folders = ["skills/a-unrelated", "skills/z-" + folder]
    client, remote = remote_client(folders)
    plan = client.prepare(RepositoryConfig(repository="example/skills", folder_hints=[hint]))
    assert plan.state["folders"] == list(reversed(folders))
    assert all("/contents/" not in request.url.path for request in remote.requests)
    assert "tags" not in plan.state


def test_explicit_folders_preserve_given_order_even_with_hints(remote_client):
    """운영자가 직접 고른 폴더 순서를 관심 기술의 우선순위로 덮어쓰지 않는다."""
    folders = ["skills/b-unrelated", "skills/a-security"]
    client, _ = remote_client(list(reversed(folders)))
    plan = client.prepare(RepositoryConfig(repository="example/skills", skill_folders=folders, folder_hints=["api_security"]))
    assert plan.state["folders"] == folders
    assert plan.state["scope"] == sorted(folders)


def test_pending_commit_and_folder_order_are_fixed_when_hints_change(remote_client):
    """진행 중인 회차는 관심 기술과 최신 커밋이 바뀌어도 저장한 순서를 이어 읽는다."""
    folders = ["skills/a-docs", "skills/z-security"]
    client, remote = remote_client(folders)
    first = client.prepare(RepositoryConfig(repository="example/skills", folder_hints=["api_security"]))
    continuation = {**first.state, "next": 1}
    remote.sha = "f" * 40
    remote.requests.clear()
    resumed = client.prepare(RepositoryConfig(repository="example/skills", folder_hints=["documentation"]), continuation)
    assert resumed.state == continuation
    assert resumed.state is not continuation
    assert resumed.state["folder_hints"] == ["api_security"]
    assert resumed.state["folders"] == ["skills/z-security", "skills/a-docs"]
    assert all(not request.url.path.endswith("/commits") for request in remote.requests)
    assert next(request.url.path for request in remote.requests if "/git/trees/" in request.url.path).endswith("b" * 40)


def test_new_round_can_change_priority_after_previous_round(remote_client):
    """새 회차를 만들 때만 변경한 관심 기술에 따라 전체 목록 순서를 다시 정한다."""
    client, _ = remote_client(["skills/a-docs", "skills/z-security"])
    security = client.prepare(RepositoryConfig(repository="example/skills", folder_hints=["api_security"]))
    docs = client.prepare(RepositoryConfig(repository="example/skills", folder_hints=["documentation"]))
    assert security.state["folders"] == list(reversed(docs.state["folders"]))
    assert docs.state["folder_hints"] == ["documentation"]


@pytest.mark.parametrize("model,field", [(RepositoryConfig, "folder_hints"), (CollectionConfig, "discovery_hints")])
def test_hint_validation_deduplicates_before_limiting(model, field):
    """중복은 먼저 없애되 서로 다른 정규 기술 이름은 20개까지만 저장한다."""
    base = {"repository": "example/skills"} if model is RepositoryConfig else {}
    assert getattr(model(**base, **{field: ["python"] * 25}), field) == ["python"]
    with pytest.raises(ValidationError):
        model(**base, **{field: sorted(FOLDER_HINT_KEYS)[:21]})


@pytest.mark.parametrize("value", [["private customer project"], ["../../private"], ["Python"], [""], "python", [1], ["next.js"]])
def test_hint_validation_rejects_noncanonical_or_free_text_values(value):
    """검색 우선순위에 자유 문장과 경로 및 미정규 별칭이 섞이지 않도록 거부한다."""
    with pytest.raises(ValidationError):
        RepositoryConfig(repository="example/skills", folder_hints=value)
    with pytest.raises(ValidationError):
        CollectionConfig(discovery_hints=value)
