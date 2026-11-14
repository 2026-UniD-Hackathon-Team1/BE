"""남은 고정 번들을 현재 작업에 맞게 골라도 원본 목록과 완료 위치가 유지되는지 확인한다."""
import httpx
import pytest

from backend.catalog.config import RepositoryConfig
from backend.catalog.github_client import GithubClient, folder_priority, prioritize_folders
from backend.catalog.service import CatalogCollector
from backend.models import Source
from test_catalog_batching import PinnedGithub


def test_exact_code_review_precedes_generic_audit_and_unrelated_goal_words():
    """일반 audit라는 단어가 실제 code-review 목적의 원문보다 먼저 수집되지 않게 한다."""
    folders = ["plugins/accessibility-compliance/skills/wcag-audit-patterns",
        "plugins/blockchain-web3/skills/solidity-security",
        "plugins/database-design/skills/postgresql-table-design",
        "plugins/developer-essentials/skills/code-review-excellence",
        "plugins/python-development/skills/python-error-handling"]
    hints = ["code_review", "api_security", "input_validation", "exception_handling",
        "regression_test", "database_changes", "documentation"]
    ranked = prioritize_folders(folders, hints)
    assert ranked[0] == "plugins/developer-essentials/skills/code-review-excellence"
    assert ranked.index("plugins/python-development/skills/python-error-handling") < ranked.index(folders[0])
    assert set(ranked) == set(folders)


def test_generic_audit_is_not_api_security_evidence_or_priority():
    """접근성이나 회계 감사 단어를 API 보안 지원으로 바꾸지 않는다."""
    assert folder_priority("skills/wcag-audit", ["api_security"]) == folder_priority("skills/unrelated", ["api_security"])
    assert folder_priority("skills/api-security-review", ["api_security"]) < folder_priority("skills/wcag-audit", ["api_security"])


def test_exact_task_and_capability_combination_precedes_generic_multiword_overlap():
    """정확한 작업과 목표 검사가 일반 기술 단어가 여러 개인 폴더보다 우선한다."""
    folders = ["skills/python-security-docs", "skills/pr-code-reviewer", "skills/python-code-review-input-validation"]
    ranked = prioritize_folders(folders, ["python", "code_review", "input_validation", "api_security", "documentation"])
    assert ranked == [folders[2], folders[1], folders[0]]


@pytest.mark.parametrize("explicit", [False, True])
def test_current_hints_choose_unread_unit_without_changing_pinned_plan(database, explicit):
    """자동 회차의 미완료 위치만 재평가하고 명시한 운영자 순서는 그대로 존중한다."""
    remote = PinnedGithub(skills=0)
    folders = ["skills/a-generic-audit", "skills/b-solidity-security", "skills/c-database-design", "skills/z-code-review-excellence"]
    remote.files = {folder + "/SKILL.md": b"Code review provides input validation." for folder in folders}
    remote.files["README.md"] = b"Skills"
    remote.history = {remote.sha: dict(remote.files)}
    scope = folders if explicit else []

    def collect(hints):
        """별도 HTTP 클라이언트로 같은 고정 회차의 한 번들만 저장한다."""
        remote.requests.clear()
        client = GithubClient(transport=httpx.MockTransport(remote.handle), max_requests=6)
        try:
            return CatalogCollector(database[1], client).collect(RepositoryConfig(
                repository="example/skills", skill_folders=scope, folder_hints=hints), batch_size=1)
        finally:
            client.close()

    assert collect(["api_security"])["completed_units"] == 1
    with database[1]() as session:
        before = dict(session.get(Source, "github:example/skills").sync_state["batch"])
    assert collect(["code_review", "input_validation"])["completed_units"] == 2
    with database[1]() as session:
        after = session.get(Source, "github:example/skills").sync_state["batch"]
    for key in ("folders", "generation", "commit_sha", "tree_sha", "folder_hints", "scope"):
        assert after[key] == before[key]
    contents = [request for request in remote.requests if "/contents/" in request.url.path]
    assert len(contents) == 1 and contents[0].url.params["ref"] == "a" * 40
    expected = before["folders"][1] if explicit else folders[-1]
    assert contents[0].url.path.endswith("/" + expected + "/SKILL.md")
    assert after["completed_indices"] == sorted([0, before["folders"].index(expected)])
    assert after["next"] == (2 if explicit else 1)
