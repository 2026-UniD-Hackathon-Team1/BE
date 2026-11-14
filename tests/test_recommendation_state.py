"""현재 기기의 완료 설치 기록만 추천에서 제외하는지 PostgreSQL에서 확인한다."""
from types import SimpleNamespace as NS

import pytest

from backend.models import (AgentDevice, AgentJob, ApplyRecord, AssetVersion, Comparison,
                            Profile, Snapshot, WorkMethod)
from backend.recommendation_state import known_applied_versions
from backend.seed import seed


@pytest.fixture
def applied_state(database):
    """실제 설치 API와 같은 식별자 및 해시 연결을 테스트 스키마에 만든다."""
    sessions = database[1]
    digest = "d" * 64
    with sessions.begin() as session:
        seed(session)
        session.add(Profile(id="other", role="other", primary_language="python", task_types=[], goals=[]))
        session.flush()
        session.add_all([AgentDevice(id="device_a", profile_id="local", token_hash="device-a",
                device_name="a", agent_version="test", is_primary=True, last_seen_at="2026-11-15T00:00:00Z"),
            AgentDevice(id="device_b", profile_id="local", token_hash="device-b",
                device_name="b", agent_version="test", last_seen_at="2026-11-15T00:00:00Z"),
            AgentDevice(id="device_other", profile_id="other", token_hash="device-other",
                device_name="other", agent_version="test", last_seen_at="2026-11-15T00:00:00Z")])
        session.add(WorkMethod(id="wm_state", version=1, profile_id="local", task_type="code_review",
            prompt_text="review", is_active=True))
        session.get(AssetVersion, "demo_ver_01").bundle_sha256 = digest
        session.flush()
        session.add(Comparison(id="cmp_state", profile_id="local", method_id="wm_state", method_version=1,
            candidate_version_id="demo_ver_01", input_manifest=[], input_sha256="i" * 64,
            plan={}, status="done", user_decision="candidate"))
        session.add(Snapshot(id="snapshot_state", target_path=".agents/skills/review",
            backup_path=".galtalka/backups/apply_state/before", before_sha256=None, verified=True))
        session.flush()
        session.add(ApplyRecord(id="apply_state", comparison_id="cmp_state", snapshot_id="snapshot_state",
            candidate_version_id="demo_ver_01", target_path=".agents/skills/review",
            applied_sha256=digest, status="applied", applied_at="2026-11-15T00:00:00Z"))
        session.add(AgentJob(id="install_state", profile_id="local", device_id="device_a", kind="apply",
            request_key="install:apply_state", request_hash="request", status="done",
            payload={"phase": "install", "apply_id": "apply_state", "candidate_sha256": digest,
                "expected_current_sha256": None},
            result_json={"status": "applied", "apply_id": "apply_state", "applied_sha256": digest,
                "before_sha256": None}))
    return sessions, NS(profile_id="local", device_id="device_a")


def test_completed_exact_install_is_read_without_mutating_records(applied_state):
    """완료된 전체 번들만 마지막 확인 설치 상태로 읽고 B 기록은 변경하지 않는다."""
    sessions, env = applied_state
    with sessions() as session:
        assert known_applied_versions(session, "local", env) == {"demo_ver_01"}
        assert not session.dirty and not session.new
        assert session.get(ApplyRecord, "apply_state").status == "applied"


def test_only_current_device_and_profile_can_suppress_recommendation(applied_state):
    """다른 기기나 사용자 및 기기 없는 환경으로 설치 상태를 넘겨 추정하지 않는다."""
    sessions, env = applied_state
    with sessions() as session:
        for state in (None, NS(profile_id="local", device_id=None),
                NS(profile_id="local", device_id="device_b"), NS(profile_id="other", device_id="device_other")):
            assert known_applied_versions(session, "local", state) == set()
        assert known_applied_versions(session, "other", env) == set()
    with sessions.begin() as session:
        session.get(Comparison, "cmp_state").profile_id = "other"
    with sessions() as session:
        assert known_applied_versions(session, "local", env) == set()


@pytest.mark.parametrize("status", ["applying", "rolled_back", "recovery_required"])
def test_incomplete_or_recovered_record_is_not_an_installation_fact(applied_state, status):
    """적용 중이나 복원 및 복구 필요 상태를 설치 완료로 취급하지 않는다."""
    sessions, env = applied_state
    with sessions.begin() as session:
        session.get(ApplyRecord, "apply_state").status = status
    with sessions() as session:
        assert known_applied_versions(session, "local", env) == set()


@pytest.mark.parametrize("target", ["record", "payload", "result", "version", "version_id", "phase", "apply_id"])
def test_any_candidate_hash_or_identity_mismatch_abstains(applied_state, target):
    """후보 버전과 적용 기록 및 기기 보고의 식별자나 해시가 다르면 추천을 숨기지 않는다."""
    sessions, env = applied_state
    with sessions.begin() as session:
        record, job = session.get(ApplyRecord, "apply_state"), session.get(AgentJob, "install_state")
        if target == "record":
            record.applied_sha256 = "e" * 64
        elif target == "payload":
            job.payload = {**job.payload, "candidate_sha256": "e" * 64}
        elif target == "result":
            job.result_json = {**job.result_json, "applied_sha256": "e" * 64}
        elif target == "version":
            session.get(AssetVersion, "demo_ver_01").bundle_sha256 = "e" * 64
        elif target == "version_id":
            job.payload = {**job.payload, "candidate_version_id": "demo_ver_02"}
        elif target == "phase":
            job.payload = {**job.payload, "phase": "preview"}
        else:
            job.result_json = {**job.result_json, "apply_id": "another"}
    with sessions() as session:
        assert known_applied_versions(session, "local", env) == set()


@pytest.mark.parametrize("status", ["queued", "running", "failed", "cancelled"])
def test_install_job_must_be_completed(applied_state, status):
    """완료되지 않은 작업의 결과나 적용 상태 값만으로 설치를 인정하지 않는다."""
    sessions, env = applied_state
    with sessions.begin() as session:
        session.get(AgentJob, "install_state").status = status
    with sessions() as session:
        assert known_applied_versions(session, "local", env) == set()


def test_rollback_job_prevents_stale_applied_state_without_reconciliation(applied_state):
    """복원 결과를 아직 조회하지 않았어도 과거 적용 기록으로 추천을 숨기지 않는다."""
    sessions, env = applied_state
    with sessions.begin() as session:
        session.add(AgentJob(id="rollback_state", profile_id="local", device_id="device_a", kind="rollback",
            request_key="rollback:apply_state", request_hash="rollback", status="done",
            payload={"apply_id": "apply_state"}, result_json={"status": "rolled_back", "apply_id": "apply_state"}))
    with sessions() as session:
        assert known_applied_versions(session, "local", env) == set()
        assert session.get(ApplyRecord, "apply_state").status == "applied"
        assert not session.dirty


def test_comparison_and_record_must_refer_to_same_candidate(applied_state):
    """비교에서 고른 후보와 적용 기록의 후보가 달라진 조합은 무시한다."""
    sessions, env = applied_state
    with sessions.begin() as session:
        session.get(Comparison, "cmp_state").candidate_version_id = "demo_ver_02"
    with sessions() as session:
        assert known_applied_versions(session, "local", env) == set()
