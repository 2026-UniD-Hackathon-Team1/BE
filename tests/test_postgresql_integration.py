"""공통 PostgreSQL 테스트 스키마에서 데이터와 연결 인증을 검증한다."""

import pytest
from concurrent.futures import ThreadPoolExecutor
from fastapi.testclient import TestClient
from alembic import command
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from backend.models import ApplyRecord, Base, Comparison, EventLog, Finding, Run, Snapshot, WorkMethod
from backend.seed import seed
from backend.main import create_app


def test_unified_catalog_comparison_and_apply_schema(database):
    """실제 PostgreSQL에서 A/B 테이블과 JSON 및 복합 FK, 적용 이력 연결을 검증한다."""
    engine, sessions, config = database
    assert set(Base.metadata.tables) <= set(inspect(engine).get_table_names())
    with sessions.begin() as session:
        seed(session)
        session.add(WorkMethod(id="wm_pg", version=1, profile_id="local", task_type="code_review",
                               prompt_text="old", is_active=False))
        session.add(WorkMethod(id="wm_pg", version=2, profile_id="local", task_type="code_review",
                               prompt_text="new", is_active=True))
        session.flush()
        session.add(Comparison(id="cmp_pg", profile_id="local", method_id="wm_pg", method_version=1,
            candidate_version_id="demo_ver_01", input_manifest=[{"path": "fixture.py"}], input_sha256="a" * 64,
            plan={"model_id": "test-model"}, status="done"))
        session.add(Snapshot(id="snap_pg", target_path="fixture/SKILL.md", backup_path="backup/SKILL.md", verified=True))
        session.flush()
        session.add(Run(id="run_pg", comparison_id="cmp_pg", arm="baseline", status="completed", result_json={"findings": []}))
        session.flush()
        session.add(Finding(id="finding_pg", comparison_id="cmp_pg", run_id="run_pg", description="test evidence"))
        session.add(ApplyRecord(id="apply_pg", comparison_id="cmp_pg", snapshot_id="snap_pg",
            candidate_version_id="demo_ver_01", target_path="fixture/SKILL.md", status="rolled_back"))
        session.add(EventLog(id="event_pg", profile_id="local", event_type="rollback", subject_id="apply_pg", payload={"test": True}))
    with sessions() as session:
        assert session.get(Comparison, "cmp_pg").plan["model_id"] == "test-model"
        assert session.get(Snapshot, "snap_pg").verified is True
        assert session.get(ApplyRecord, "apply_pg").status == "rolled_back"
        assert session.get(EventLog, "event_pg").payload == {"test": True}
    with pytest.raises(IntegrityError), sessions.begin() as session:
        session.add(Comparison(id="cmp_bad", profile_id="local", method_id="wm_pg", method_version=999,
            candidate_version_id="demo_ver_01", input_manifest=[], input_sha256="a" * 64, plan={}))
    with pytest.raises(IntegrityError), sessions.begin() as session:
        session.add(WorkMethod(id="wm_other", version=1, profile_id="local", task_type="code_review", prompt_text="x", is_active=True))
    command.downgrade(config, "base")
    assert inspect(engine).get_table_names() == ["alembic_version"]
    command.upgrade(config, "head")
    assert set(Base.metadata.tables) <= set(inspect(engine).get_table_names())


def test_agent_pairing_concurrency_and_revocation_on_postgres(database):
    """실제 DB의 동시 코드 교환과 영속 요청 제한 및 토큰 무효화를 검증한다."""
    _, _, config = database
    headers = {"X-Local-Token": "test-local-token"}
    with TestClient(create_app(database_url=config.attributes["database_url"], local_token="test-local-token")) as client:
        assert client.put("/api/profile", headers=headers, json={"role": "developer", "primary_language": "python",
            "task_types": ["code_review"]}).status_code == 200
        code = client.post("/api/agent/pairing-code", headers=headers).json()["code"]
        body = {"code": code, "device_name": "test-device", "agent_version": "0.1.0"}
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(lambda _: client.post("/api/agent/pair", json=body), range(2)))
        assert sorted(response.status_code for response in responses) == [200, 400]
        token = next(response.json()["agent_token"] for response in responses if response.status_code == 200)
        assert client.post("/api/agent/heartbeat", headers={"Authorization": "Bearer " + token}, json={}).status_code == 204
        assert client.delete("/api/agent", headers=headers).status_code == 204
        assert client.post("/api/agent/heartbeat", headers={"Authorization": "Bearer " + token}, json={}).status_code == 401
        for _ in range(8):
            assert client.post("/api/agent/pair", json=body).status_code == 400
        assert client.post("/api/agent/pair", json=body).status_code == 429
    command.check(config)
