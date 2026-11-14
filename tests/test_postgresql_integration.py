"""명시적인 POSTGRES_TEST_URL이 있을 때만 별도 스키마에서 실제 DB를 검증한다."""
import os
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateSchema, DropSchema

from backend.db import make_engine, session_factory
from backend.models import ApplyRecord, Base, Comparison, EventLog, Finding, Run, Snapshot, WorkMethod
from backend.seed import seed


@pytest.fixture
def postgres_database():
    """명시한 테스트 DB에 고유 스키마만 생성하고 종료 시 그 스키마만 제거한다."""
    url = os.environ.get("POSTGRES_TEST_URL")
    if not url:
        pytest.skip("POSTGRES_TEST_URL 미설정: 실제 PostgreSQL 통합 검증은 아직 실행하지 않음")
    if make_url(url).get_backend_name() not in {"postgres", "postgresql"}:
        pytest.fail("POSTGRES_TEST_URL은 PostgreSQL 테스트 DB여야 합니다.")
    schema = "galatalkka_test_" + uuid4().hex
    admin = make_engine(url)
    with admin.begin() as connection:
        connection.execute(CreateSchema(schema))
    parsed = make_url(url)
    options = parsed.query.get("options", "") + f" -csearch_path={schema}"
    isolated_url = parsed.update_query_dict({"options": options.strip()}).render_as_string(hide_password=False)
    engine = make_engine(isolated_url)
    config = Config("alembic.ini")
    config.attributes["database_url"] = isolated_url
    try:
        command.upgrade(config, "head")
        yield engine, session_factory(engine), config
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        admin.dispose()


def test_unified_catalog_comparison_and_apply_schema(postgres_database):
    """실제 PostgreSQL에서 A/B 테이블과 JSON 및 복합 FK, 적용 이력 연결을 검증한다."""
    engine, sessions, config = postgres_database
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
