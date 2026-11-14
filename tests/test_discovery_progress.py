"""발견 결과 없음과 실패 및 미시도를 구분하고 실패 시 검색 순환을 보존한다."""
import copy

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.catalog.config import CollectionConfig
from backend.catalog.github_client import GithubClient
from backend.catalog.refresh import refresh_router, run_refresh_tick
from backend.catalog.schemas import CollectionResult
from backend.catalog.service import CatalogCollector
from backend.models import Asset, CatalogRuntime, CatalogSyncJob, Profile, Source
from test_catalog import FakeGithub


def test_successful_empty_search_is_done_and_progress_keeps_unattempted_queries(database):
    """검색이 성공한 빈 결과와 아직 실행하지 않은 검색을 서로 다른 상태로 기록한다."""
    calls, snapshots = [], []

    def empty(request):
        """실제 외부 검색 없이 정상적인 빈 결과를 반환한다."""
        calls.append(request)
        return httpx.Response(200, json={"incomplete_results": False, "items": []})

    client = GithubClient(transport=httpx.MockTransport(empty), max_requests=2)
    try:
        result = CatalogCollector(database[1], client).run(
            CollectionConfig(discovery_queries=["python skill", "react skill"]), progress=snapshots.append)
    finally:
        client.close()
    assert result["items"] == result["discovery_errors"] == []
    assert [item["status"] for item in snapshots[0]["discovery"]] == ["not_attempted", "not_attempted"]
    assert [item["status"] for item in snapshots[1]["discovery"]] == ["done", "not_attempted"]
    assert [item["status"] for item in result["discovery"]] == ["done", "done"]
    assert all(item["repositories"] == [] and item["error"] is None for item in result["discovery"])
    assert len(calls) == 2
    assert CollectionResult.model_validate(result).discovery[0].status == "done"
    assert CollectionResult.model_validate({}).discovery == []


def test_budget_stop_marks_failure_and_unattempted_and_preserves_discovered_queue(database):
    """예산 중단 뒤 검색을 미시도로 남기고 이미 발견한 저장소는 다음 수집까지 보존한다."""
    remote = FakeGithub()
    limited = GithubClient(transport=httpx.MockTransport(remote.handle), max_requests=1)
    try:
        result = CatalogCollector(database[1], limited).run(CollectionConfig(
            discovery_queries=["first", "second", "third"], discovery_limit=1))
    finally:
        limited.close()
    assert [item["status"] for item in result["discovery"]] == ["done", "failed", "not_attempted"]
    assert result["discovery"][0]["repositories"] == ["example/review"]
    assert result["discovery"][1]["error"] == "request_budget_exceeded"
    assert result["discovery"][2]["repositories"] == [] and result["discovery"][2]["error"] is None
    assert result["items"] == [] and len(remote.requests) == 1
    with database[1]() as session:
        source = session.get(Source, "github:example/review")
        assert source.sync_state["queued_config"]["repository"] == "example/review"
        assert session.scalar(select(func.count()).select_from(Asset)) == 0
    resumed = GithubClient(transport=httpx.MockTransport(remote.handle), max_requests=10)
    try:
        continued = CatalogCollector(database[1], resumed).run(CollectionConfig(resume_pending=True))
    finally:
        resumed.close()
    assert continued["discovery"] == [] and continued["items"][0]["status"] == "done"
    with database[1]() as session:
        assert "queued_config" not in session.get(Source, "github:example/review").sync_state
        assert session.scalar(select(func.count()).select_from(Asset)) == 2


def test_non_stopping_query_failure_does_not_hide_later_success(database):
    """특정 검색의 서버 오류와 그 뒤 정상 검색을 쿼리별로 구분한다."""
    calls = []

    def remote(request):
        """첫 검색만 서버 오류이고 다음 검색은 빈 결과인 상황을 만든다."""
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(500)
        return httpx.Response(200, json={"incomplete_results": False, "items": []})

    client = GithubClient(transport=httpx.MockTransport(remote), max_requests=2)
    try:
        result = CatalogCollector(database[1], client).run(CollectionConfig(discovery_queries=["first", "second"]))
    finally:
        client.close()
    assert [item["status"] for item in result["discovery"]] == ["failed", "done"]
    assert result["discovery"][0]["error"] == "http_500" and result["discovery"][1]["error"] is None
    assert result["discovery_errors"][0]["query"] == "first" and len(calls) == 2


@pytest.fixture
def scheduled_context(database):
    """서로 다른 두 사용자와 순환 예약을 실제 PostgreSQL에 준비한다."""
    sessions = database[1]
    now = [1794700800]
    with sessions.begin() as session:
        session.add_all([Profile(id="alice", role="backend", primary_language="python",
            task_types=["code_review"], goals=["input_validation"], context_json={"frameworks": ["fastapi"]}),
            Profile(id="bob", role="frontend", primary_language="typescript",
            task_types=["test_generation"], goals=["test_coverage"], context_json={"frameworks": ["react"]})])
    app = FastAPI()
    app.include_router(refresh_router(sessions, lambda: "local", clock=lambda: now[0]))
    with TestClient(app) as api:
        api.put("/api/catalog/refresh/schedule", json={"enabled": True, "contextual_discovery": True})
        yield sessions, now


def read_runtime(sessions):
    """중첩 커서 비교를 위해 런타임 상태를 독립 복사한다."""
    with sessions() as session:
        return copy.deepcopy(session.get(CatalogRuntime, "github").state_json)


def test_transient_failure_restores_previous_cursor_and_retries_unattempted_plan(scheduled_context):
    """일시 제한으로 멈춘 회차는 이전 검색 위치에서 재시도하고 성공할 때만 전진한다."""
    sessions, now = scheduled_context
    calls, limited = [], [True]

    def remote(request):
        """한 번의 제한 뒤 동일 검색을 정상 수행할 수 있도록 응답을 바꾼다."""
        calls.append(request)
        if limited[0]:
            return httpx.Response(429, headers={"Retry-After": "60"})
        return httpx.Response(200, json={"incomplete_results": False, "items": []})

    def factory(**kwargs):
        """네트워크 대신 요청과 응답만 관찰하는 클라이언트를 만든다."""
        return GithubClient(transport=httpx.MockTransport(remote), **kwargs)

    before = read_runtime(sessions)["contextual_cursor"]
    first = run_refresh_tick(sessions, clock=lambda: now[0], client_factory=factory)
    failed_state = read_runtime(sessions)
    assert failed_state["contextual_cursor"] == before
    assert failed_state["due"]["discovery"] == now[0] + 300
    with sessions() as session:
        job = session.get(CatalogSyncJob, first["job_id"])
        assert job.status == "failed"
        assert [item["status"] for item in job.result_json["discovery"]] == [
            "failed", "not_attempted", "not_attempted", "not_attempted", "not_attempted"]
    limited[0] = False
    now[0] += 301
    second = run_refresh_tick(sessions, clock=lambda: now[0], client_factory=factory)
    assert second["mode"] == "discovery" and second["job_id"] != first["job_id"]
    assert calls[0].url.params["q"] == calls[1].url.params["q"]
    assert read_runtime(sessions)["contextual_cursor"]["query_offset"] == 5


@pytest.mark.parametrize("changed_field", ["revision", "last_job_id"])
def test_old_failed_job_cannot_restore_cursor_over_newer_schedule_or_job(scheduled_context, monkeypatch, changed_field):
    """새 설정이나 새 작업이 기록된 뒤 이전 실패가 커서와 재시도 기한을 덮지 못한다."""
    from backend.catalog import refresh
    sessions, now = scheduled_context

    def finish_after_newer_state(sessions, job_id, client_factory):
        """작업 실패 직전에 다른 실행이 런타임을 갱신하는 순서를 재현한다."""
        with sessions.begin() as session:
            job = session.get(CatalogSyncJob, job_id)
            job.status = "failed"
            job.active_slot = None
            job.result_json = {"items": [], "discovery_errors": [{"query": "query", "error": "network_error"}]}
            row = session.get(CatalogRuntime, "github")
            row.state_json = {**row.state_json, changed_field: "newer_value"}

    monkeypatch.setattr(refresh, "run_job", finish_after_newer_state)
    run_refresh_tick(sessions, clock=lambda: now[0])
    current = read_runtime(sessions)
    assert current[changed_field] == "newer_value"
    assert current["contextual_cursor"]["query_offset"] == 5
    assert current["due"]["discovery"] == now[0] + 86400
