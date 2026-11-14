"""개인 검색 계획의 관련성, 개인정보 경계와 실제 수집 작업 연결을 확인한다."""
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from backend.catalog.discovery import discovery_router, plan_discovery, shared_folder_hints, shared_queries
from backend.catalog.github_client import GithubClient
from backend.catalog.jobs import catalog_router
from backend.models import CatalogSyncJob, Profile, WorkMethod
from test_catalog import FakeGithub


def persona(name="backend", language="python", framework="fastapi", goal="input_validation"):
    """비밀 원문과 공개 기술명이 함께 있는 사용자 맥락을 만든다."""
    return SimpleNamespace(id=name, role="PRIVATE_ROLE_acme", primary_language=language,
        task_types=["code_review"], goals=[goal], version=1,
        context_json={"frameworks": [framework], "tools": ["codex_cli"],
            "focus_areas": [], "excluded_capabilities": []}, preferences={})


def method(goal="input_validation"):
    """검색에 원문이 노출되면 안 되는 기존 작업 방식과 명시적 부족 검사를 만든다."""
    return SimpleNamespace(id="wm", version=1, task_type="code_review",
        prompt_text="PRIVATE_PROMPT /home/acme/project confidential_key=ABC123",
        required_checks=["PRIVATE_CHECK_조직 내부 검사"], scoring_preferences={},
        workflow_json=[{"id": "validation", "label": "PRIVATE_STEP", "capabilities": [goal],
                        "status": "gap", "weight": 5}])


def test_plans_use_stack_gap_and_separate_recent_from_evergreen():
    """스택과 부족 검사별 검색이 달라지고 최신 활동과 기간 제한 없는 경로를 함께 제공한다."""
    now = datetime(2026, 11, 15, tzinfo=timezone.utc)
    backend = plan_discovery(persona(), {"code_review": method()}, None, now=now)
    frontend = persona("frontend", "typescript", "react", "test_coverage")
    frontend.task_types = ["test_generation"]
    other = plan_discovery(frontend, {}, None, now=now)
    assert backend.network_executed is False
    assert {item.scope for item in backend.queries} == {"recent", "evergreen"}
    assert "pushed:>=2026-08-17" in backend.queries[0].query
    assert all("pushed:" not in item.query for item in backend.queries if item.scope == "evergreen")
    assert "fastapi" in backend.queries[0].query and "react" in other.queries[0].query
    assert "code_review" in backend.folder_hints and "test_generation" in other.folder_hints
    assert {"fastapi", "python", "codex_cli", "input_validation"} <= set(backend.folder_hints)
    assert {"react", "typescript", "test_coverage"} <= set(other.folder_hints)
    assert backend.queries[1].query.startswith('"code review" ')
    assert other.queries[1].query.startswith('"test-driven" ')
    assert backend.queries[1].sort == other.queries[1].sort == "best_match"
    assert backend.queries[2].query == other.queries[2].query
    assert backend.queries[2].query.startswith("skills in:name ")
    assert all("topic:agent-skills" in item.query and "archived:false" in item.query
        and "fork:false" in item.query for item in backend.queries + other.queries)
    assert any('"input validation"' in item.query for item in backend.queries)
    assert any("coverage" in item.query for item in other.queries)
    assert backend.context_fingerprint != other.context_fingerprint
    serialized = backend.model_dump_json()
    assert not any(value in serialized for value in ["PRIVATE_", "/home/acme", "confidential_key", "ABC123"])


def test_unrecognized_user_strings_never_become_remote_query_terms():
    """기술명에 비밀값이나 검색 연산자를 넣어도 허용 목록 밖 문자열을 보내지 않는다."""
    profile = persona(language="private_host:secret", framework="org:secret-acme")
    profile.context_json = {"frameworks": ["org:secret-acme"], "tools": ["token:abc123"]}
    profile.goals = []
    plan = plan_discovery(profile, {}, None)
    assert all("org:" not in item.query and "secret" not in item.query and "token:" not in item.query
               for item in plan.queries)
    assert any("기술 환경이 없어" in text for text in plan.limitations)


def test_observed_primary_project_stack_drives_discovery():
    """기본 기기의 프로젝트 관측이 있으면 프로필의 예전 프레임워크 대신 사용한다."""
    environment = SimpleNamespace(id="env", device_id="device", runner="codex_cli", runner_version="1",
        model_id="unknown", scanned_at="2026-11-15T00:00:00Z", settings_hash="digest", installed_skills=[],
        metadata_json={"languages": ["typescript"], "frameworks": ["react"]})
    plan = plan_discovery(persona(), {}, environment)
    assert "react" in plan.queries[0].query
    assert "fastapi" not in plan.queries[0].query


def test_shared_queries_are_deduplicated_and_round_robin_bounded():
    """다수 사용자에서 한 사람의 전체 검색보다 사용자별 첫 검색을 먼저 배정한다."""
    profiles = [persona(), persona("frontend", "typescript", "react", "test_coverage"),
                persona("backend_copy")]
    profiles[1].task_types = ["test_generation"]
    plans = [plan_discovery(profile, {}, None) for profile in profiles]
    selected = shared_queries(plans, limit=5)
    assert len(selected) <= 5 and len(selected) == len(set(selected))
    assert selected[0] == plans[0].queries[0].query
    assert selected[1] == plans[1].queries[0].query
    assert selected.count(plans[2].queries[0].query) == 1
    assert plans[0].queries[1].query in selected
    assert plans[1].queries[1].query in selected
    assert selected[-1] == plans[0].queries[2].query
    assert all(plan.queries[3].query not in selected for plan in plans)


def test_shared_folder_hints_are_bounded_known_and_fair():
    """첫 프로필의 긴 힌트 목록이 다른 프로필의 기술명과 검사 항목을 밀어내지 않는다."""
    values = [["python", "fastapi", "pytest", "code_review"],
              ["react", "typescript", "test_generation", "test_coverage"]]
    assert shared_folder_hints(values, limit=4) == ["python", "react", "fastapi", "typescript"]
    assert shared_folder_hints([["python", "PRIVATE_TOKEN=abc", "org:secret", "python"]]) == ["python"]


@pytest.fixture
def discovery_api(database):
    """개인과 운영 권한을 다르게 주고 외부 전송만 모의 응답으로 교체한다."""
    sessions = database[1]
    remote = FakeGithub()
    with sessions.begin() as session:
        session.add_all([Profile(id="alice", role="backend", primary_language="python",
            task_types=["code_review"], goals=["input_validation"],
            context_json={"frameworks": ["fastapi"]}),
            Profile(id="bob", role="frontend", primary_language="typescript",
            task_types=["test_generation"], goals=["test_coverage"], context_json={"frameworks": ["react"]})])
        session.flush()
        session.add(WorkMethod(id="private_method", version=1, profile_id="alice", task_type="code_review",
            prompt_text="PRIVATE_PROMPT should never be searched", required_checks=["입력 검증"],
            workflow_json=[]))

    def factory(**kwargs):
        """명시한 예산을 그대로 두고 실제 GitHub 대신 고정 원문을 읽는다."""
        return GithubClient(transport=httpx.MockTransport(remote.handle), **kwargs)

    def web(user: str | None = Header(None, alias="X-Test-User")):
        """개인 계획 조회에만 사용하는 사용자 인증을 제공한다."""
        if user not in {"alice", "bob", "missing"}:
            raise HTTPException(401)
        return user

    def operator(value: str | None = Header(None, alias="X-Test-Operator")):
        """공통 수집 예산은 별도 운영 자격이 있어야 사용한다."""
        if value != "operator":
            raise HTTPException(403)
        return "local"

    app = FastAPI()
    app.include_router(discovery_router(sessions, web, operator, client_factory=factory))
    app.include_router(catalog_router(sessions, operator, client_factory=factory))
    with TestClient(app) as client:
        yield client, sessions, remote


def test_personal_plan_is_private_read_only_and_cannot_execute_collection(discovery_api):
    """개인 계획은 자신의 맥락만 읽으며 일반 사용자가 공통 수집을 실행하지 못한다."""
    client, _, remote = discovery_api
    assert client.get("/api/catalog/discovery-plan").status_code == 401
    alice = client.get("/api/catalog/discovery-plan?profile_id=bob", headers={"X-Test-User": "alice"})
    bob = client.get("/api/catalog/discovery-plan", headers={"X-Test-User": "bob"})
    assert alice.status_code == bob.status_code == 200
    assert alice.json()["profile_id"] == "alice" and bob.json()["profile_id"] == "bob"
    assert "fastapi" in alice.json()["queries"][0]["query"]
    assert "react" in bob.json()["queries"][0]["query"]
    assert client.get("/api/catalog/discovery-plan", headers={"X-Test-User": "missing"}).status_code == 404
    assert client.post("/api/catalog/discovery-sync", json={"profile_ids": ["alice"]},
        headers={"X-Test-User": "alice", "Idempotency-Key": "forbidden"}).status_code == 403
    assert remote.requests == []


def test_profile_sync_records_context_and_reuses_idempotent_job(discovery_api):
    """생성한 검색어로 실제 수집기를 거치고 같은 키는 원문을 다시 요청하지 않는다."""
    client, sessions, remote = discovery_api
    headers = {"X-Test-Operator": "operator", "Idempotency-Key": "planned-first"}
    payload = {"profile_ids": ["alice", "bob"], "max_requests": 20, "discovery_limit": 1}
    response = client.post("/api/catalog/discovery-sync", headers=headers, json=payload)
    assert response.status_code == 202
    job_id = response.json()["id"]
    job = client.get("/api/catalog/jobs/" + job_id, headers=headers).json()
    assert job["status"] == "done" and job["request_count"] == len(remote.requests)
    assert job["request_count"] <= 20
    assert set(job["context_snapshot"]) == {"alice", "bob"}
    assert all(value["covered"] for value in job["context_snapshot"].values())
    assert all(value["fingerprint"] and value["queries"] for value in job["context_snapshot"].values())
    assert "fastapi" in job["context_snapshot"]["alice"]["folder_hints"]
    assert "react" in job["context_snapshot"]["bob"]["folder_hints"]
    assert "PRIVATE_PROMPT" not in str([request.url for request in remote.requests])
    count = len(remote.requests)
    assert client.post("/api/catalog/discovery-sync", headers=headers, json=payload).json()["id"] == job_id
    assert len(remote.requests) == count
    with sessions() as session:
        stored = session.get(CatalogSyncJob, job_id)
        assert len(stored.config_json["discovery_queries"]) <= 5
        assert stored.config_json["resume_pending"] is True
        assert {"fastapi", "python", "react", "typescript", "code_review", "test_generation"} <= set(
            stored.config_json["discovery_hints"])
        assert stored.context_snapshot == job["context_snapshot"]
    with sessions.begin() as session:
        session.get(Profile, "alice").version += 1
    assert client.post("/api/catalog/discovery-sync", headers=headers, json=payload).status_code == 409
    assert len(remote.requests) == count


def test_profile_sync_validates_targets_before_any_network(discovery_api):
    """없는 대상과 임의 쿼리 주입 및 초과 예산은 수집 실행 전에 거부한다."""
    client, _, remote = discovery_api
    headers = {"X-Test-Operator": "operator", "Idempotency-Key": "bad"}
    assert client.post("/api/catalog/discovery-sync", headers=headers,
        json={"profile_ids": ["absent"]}).status_code == 404
    for payload in ({"profile_ids": []}, {"profile_ids": ["alice"], "max_requests": 51},
                    {"profile_ids": ["alice"], "discovery_queries": ["org:private"]}):
        assert client.post("/api/catalog/discovery-sync", headers=headers, json=payload).status_code == 422
    assert remote.requests == []
