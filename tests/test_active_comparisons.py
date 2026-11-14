"""상세 화면을 열지 않아도 홈의 진행 중 비교가 실제 기기 보고를 반영하는지 검증한다."""
import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.comparisons import comparison_router
from backend.models import AgentDevice, AgentJob, Asset, AssetVersion, Comparison, Profile, Run, Source, WorkMethod


@pytest.fixture
def active_comparisons(database):
    """파일 실행 없이 계정 두 개와 공통 카탈로그 후보 및 기기를 준비한다."""
    sessions = database[1]
    with sessions.begin() as session:
        session.add_all([Profile(id=owner, role="developer", primary_language="python", task_types=["code_review"], goals=[])
            for owner in ("alice", "bob")])
        session.add(Source(id="shared", kind="demo", url="https://example.invalid/shared", enabled=False))
        session.flush()
        session.add(Asset(id="asset", kind="skill", canonical_key="demo:review", display_name="공통 리뷰 후보", source_id="shared"))
        session.flush()
        session.add(AssetVersion(id="version", asset_id="asset", revision="fixture", bundle_sha256="a" * 64,
            metadata_json={}, tags={"task_types": ["code_review"]}))
        for owner in ("alice", "bob"):
            session.add(WorkMethod(id="method_" + owner, version=1, profile_id=owner,
                task_type="code_review", prompt_text="코드 리뷰", is_active=True))
            session.add(AgentDevice(id="device_" + owner, profile_id=owner, token_hash="fixture_hash_" + owner,
                device_name="검증 기기", agent_version="fixture", is_primary=True,
                last_seen_at="2026-11-15T00:00:00.000000Z"))

    def auth(owner: str | None = Header(None, alias="X-Test-Profile")):
        """경로 인자로 다른 계정의 소유권을 선택하지 못하는 인증 의존성을 제공한다."""
        if owner not in {"alice", "bob"}:
            raise HTTPException(401)
        return owner

    app = FastAPI()
    app.include_router(comparison_router(sessions, auth))
    with TestClient(app) as client:
        yield client, sessions


def add_comparison(sessions, comparison_id, *, owner="alice", status="queued", job_status="done"):
    """이미 보고된 작업 결과만 DB에 넣고 실제 모델과 연결 프로그램은 실행하지 않는다."""
    manifest = [{"input_id": "input_" + "1" * 32, "task_type": "code_review",
        "relative_paths": ["app.py"], "input_sha256": "b" * 64}]
    plan = {"agent_device_id": "device_" + owner, "model_id": "fixture-model", "runner_version": "fixture-runner"}
    with sessions.begin() as session:
        session.add(Comparison(id=comparison_id, profile_id=owner, method_id="method_" + owner,
            method_version=1, candidate_version_id="version", input_manifest=manifest,
            input_sha256="b" * 64, plan=plan, status=status))
        session.flush()
        for arm in ("baseline", "candidate"):
            session.add(Run(id=comparison_id + "_" + arm, comparison_id=comparison_id, arm=arm,
                status="queued", result_json={"inputs": []}, metrics_json={}, skill_usage_status="unconfirmed"))
        if job_status is not None:
            result = {"comparison_id": comparison_id, "input_sha256": "b" * 64,
                "model_id": "fixture-model", "runner_version": "fixture-runner", "runs": {
                    arm: {"inputs": [{"input_id": manifest[0]["input_id"], "status": "completed",
                        "result": {"summary": "고정 결과", "findings": []}, "seconds": 0.1,
                        "skill_usage_status": "not_expected" if arm == "baseline" else "confirmed"}]}
                    for arm in ("baseline", "candidate")}}
            session.add(AgentJob(id="job_" + comparison_id, profile_id=owner, device_id="device_" + owner,
                kind="run_comparison", request_key="comparison:" + comparison_id, request_hash="fixture-hash",
                payload={}, status=job_status, result_json=result if job_status == "done" else None,
                lease_expires_at="2999-01-01T00:00:00.000000Z" if job_status == "running" else None))


@pytest.mark.parametrize("previous_status", ["queued", "running", "evaluating"])
def test_completed_agent_job_disappears_without_opening_detail(active_comparisons, previous_status):
    """모든 활성 비교 상태에서 완료 보고를 목록 조회만으로 반영하고 영속화한다."""
    client, sessions = active_comparisons
    add_comparison(sessions, "completed", status=previous_status)
    response = client.get("/api/comparisons?active=true", headers={"X-Test-Profile": "alice"})
    assert response.status_code == 200 and response.json() == []
    with sessions() as session:
        comparison = session.get(Comparison, "completed")
        assert comparison.status == "done" and comparison.completed_at
        assert {run.status for run in session.scalars(select(Run).where(Run.comparison_id == "completed"))} == {"completed"}


@pytest.mark.parametrize("job_status", ["failed", "cancelled"])
def test_failed_agent_job_disappears_without_opening_detail(active_comparisons, job_status):
    """실패한 기기 작업도 상세 조회 없이 기존 동기화 규칙대로 종료한다."""
    client, sessions = active_comparisons
    add_comparison(sessions, "failed", status="running", job_status=job_status)
    assert client.get("/api/comparisons", headers={"X-Test-Profile": "alice"}).json() == []
    with sessions() as session:
        assert session.get(Comparison, "failed").status == "failed"
        assert {run.failure_code for run in session.scalars(select(Run).where(Run.comparison_id == "failed"))} == {"agent_job_failed"}


def test_pending_jobs_remain_with_synchronized_active_status(active_comparisons):
    """실행 중과 대기 중인 실제 작업은 목록에 남고 현재 작업 상태로 표시한다."""
    client, sessions = active_comparisons
    add_comparison(sessions, "running", status="queued", job_status="running")
    add_comparison(sessions, "queued", status="evaluating", job_status="queued")
    response = client.get("/api/comparisons", headers={"X-Test-Profile": "alice"})
    assert {item["comparison_id"]: item["status"] for item in response.json()} == {"running": "running", "queued": "queued"}


def test_owner_scope_prevents_other_account_exposure_and_synchronization(active_comparisons):
    """다른 계정의 비교를 노출하거나 해당 계정의 진행 상태를 대신 변경하지 않는다."""
    client, sessions = active_comparisons
    add_comparison(sessions, "alice_pending", job_status=None)
    add_comparison(sessions, "bob_completed", owner="bob", status="evaluating")
    assert client.get("/api/comparisons").status_code == 401
    assert client.get("/api/comparisons?active=false", headers={"X-Test-Profile": "alice"}).status_code == 422
    items = client.get("/api/comparisons?profile_id=bob", headers={"X-Test-Profile": "alice"}).json()
    assert [item["comparison_id"] for item in items] == ["alice_pending"]
    with sessions() as session:
        assert session.get(Comparison, "bob_completed").status == "evaluating"
    assert client.get("/api/comparisons", headers={"X-Test-Profile": "bob"}).json() == []


def test_terminal_comparison_is_not_resynchronized_by_active_list(active_comparisons):
    """이미 종료된 비교는 활성 목록의 처리 대상에서 제외한다."""
    client, sessions = active_comparisons
    add_comparison(sessions, "terminal", status="done", job_status="failed")
    assert client.get("/api/comparisons", headers={"X-Test-Profile": "alice"}).json() == []
    with sessions() as session:
        assert session.get(Comparison, "terminal").status == "done"
