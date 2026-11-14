"""이전 배포 DB의 기기 테이블 누락과 잘못된 정상 상태를 재현해 확인한다."""
import json

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import ProgrammingError

from backend.main import create_app
from backend.migrate import migrate
from backend.models import AgentDevice, Profile
from backend.schema_readiness import SchemaReadinessError, assert_schema_ready, expected_schema_heads
from backend import serve
from backend import schema_readiness


def _migration_environment(monkeypatch, config):
    """마이그레이션 실행을 고유 테스트 스키마와 명시적 로컬 설정에 한정한다."""
    monkeypatch.setenv("DATABASE_URL", config.attributes["database_url"])
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.delenv("RAILWAY_ENVIRONMENT_ID", raising=False)


def test_old_revision_reproduces_missing_agent_and_upgrade_preserves_profile(empty_database, monkeypatch):
    """기기 기능 이전 DB의 실제 조회 오류를 재현하고 기존 프로필을 보존하며 올린다."""
    engine, sessions, config = empty_database
    command.upgrade(config, "e20261114a01")
    with sessions.begin() as session:
        session.add(Profile(id="preserved", role="preserved-role", primary_language="python",
            task_types=["code_review"], goals=["input_validation"], preferences={}))
    with engine.connect() as connection:
        with pytest.raises(ProgrammingError, match="agent_device"):
            connection.execute(select(AgentDevice.id)).all()
    with engine.connect() as connection:
        assert inspect(connection).has_table("source")
        assert "sync_state" in {column["name"] for column in inspect(connection).get_columns("source")}
        with pytest.raises(SchemaReadinessError) as failure:
            assert_schema_ready(connection)
        assert "agent_device" in failure.value.missing_tables
    _migration_environment(monkeypatch, config)
    migrate()
    with engine.connect() as connection:
        assert_schema_ready(connection)
        assert connection.execute(select(AgentDevice.id)).all() == []
    with sessions() as session:
        assert session.get(Profile, "preserved").role == "preserved-role"
    # Railway 사전 배포와 시작 단계가 연속 호출해도 같은 스키마를 유지한다.
    migrate()
    with sessions() as session:
        assert session.get(Profile, "preserved").goals == ["input_validation"]


def test_head_stamp_cannot_hide_missing_agent_table(database, monkeypatch):
    """head로 기록됐어도 테이블이 유실됐으면 빈 테이블을 만들어 손실을 감추지 않는다."""
    engine, sessions, config = database
    with sessions.begin() as session:
        session.add(Profile(id="retained", role="retained", primary_language="python", task_types=[], goals=[]))
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE agent_device CASCADE"))
        assert tuple(connection.execute(text("SELECT version_num FROM alembic_version")).scalars()) == expected_schema_heads()
    _migration_environment(monkeypatch, config)
    with pytest.raises(SchemaReadinessError, match="missing tables: agent_device"):
        migrate()
    with engine.connect() as connection:
        assert not inspect(connection).has_table("agent_device")
    with sessions() as session:
        assert session.get(Profile, "retained").role == "retained"


def test_missing_non_catalog_column_is_reported_and_health_is_not_green(database, monkeypatch, capsys):
    """source만 정상인 부분 DB를 준비 상태로 응답하지 않고 정확한 누락 열을 찾는다."""
    engine, _, config = database
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE agent_device DROP COLUMN codex_ready"))
    with engine.connect() as connection:
        with pytest.raises(SchemaReadinessError) as failure:
            assert_schema_ready(connection)
        assert failure.value.missing_columns == {"agent_device": ("codex_ready",)}
    with TestClient(create_app(database_url=config.attributes["database_url"])) as client:
        response = client.get("/api/health")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "migration_required"
    assert "codex_ready" not in response.text
    assert config.attributes["database_url"] not in response.text
    monkeypatch.setattr(schema_readiness, "configured_engine", lambda: engine)
    assert schema_readiness.main() == 1
    diagnostic = json.loads(capsys.readouterr().out)
    assert diagnostic["status"] == "migration_required"
    assert diagnostic["missing_columns"] == {"agent_device": ["codex_ready"]}
    assert config.attributes["database_url"] not in json.dumps(diagnostic)


def test_healthy_catalog_alone_is_not_enough_for_health(empty_database):
    """기존 health 검사를 통과하던 e 리비전도 기기 및 로그인 스키마가 없으면 실패한다."""
    _, _, config = empty_database
    command.upgrade(config, "e20261114a01")
    with TestClient(create_app(database_url=config.attributes["database_url"])) as client:
        response = client.get("/api/health")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "migration_required"


def test_complete_tables_with_stale_revision_still_require_migration(database):
    """테이블 이름만 모두 있어도 적용 리비전이 맞지 않으면 준비 완료로 인정하지 않는다."""
    engine, _, config = database
    with engine.begin() as connection:
        connection.execute(text("UPDATE alembic_version SET version_num = 'e20261114a01'"))
    with engine.connect() as connection:
        with pytest.raises(SchemaReadinessError) as failure:
            assert_schema_ready(connection)
        assert failure.value.missing_tables == ()
        assert failure.value.missing_columns == {}
        assert not failure.value.revision_matches
    with TestClient(create_app(database_url=config.attributes["database_url"])) as client:
        assert client.get("/api/health").status_code == 503


def test_existing_connection_migration_participates_in_caller_transaction(empty_database):
    """공유 연결의 마이그레이션이 다른 연결에서 먼저 커밋되지 않도록 확인한다."""
    engine, _, config = empty_database
    with engine.connect() as connection:
        transaction = connection.begin()
        config.attributes["connection"] = connection
        try:
            command.upgrade(config, "head")
            assert_schema_ready(connection)
        finally:
            transaction.rollback()
            config.attributes.pop("connection", None)
    with engine.connect() as connection:
        assert not inspect(connection).has_table("profile")


@pytest.mark.parametrize("mode,railway", [("production", False), ("staging", False), ("local", True)])
def test_cloud_start_migrates_before_binding(monkeypatch, mode, railway):
    """배포 환경은 사전 배포 설정과 관계없이 마이그레이션 후 포트를 연다."""
    events = []
    monkeypatch.setenv("APP_ENV", mode)
    monkeypatch.setenv("PORT", "9000")
    if railway:
        monkeypatch.setenv("RAILWAY_ENVIRONMENT_ID", "test-environment")
    else:
        monkeypatch.delenv("RAILWAY_ENVIRONMENT_ID", raising=False)
    monkeypatch.setattr(serve, "load_local_env", lambda: None)
    monkeypatch.setattr(serve, "validate_deployment", lambda: events.append("validate"))
    monkeypatch.setattr(serve, "migrate", lambda: events.append("migrate"))
    monkeypatch.setattr(serve.uvicorn, "run", lambda *args, **kwargs: events.append("bind"))
    serve.main()
    assert events == ["validate", "migrate", "bind"]


def test_failed_cloud_migration_never_binds_api(monkeypatch):
    """마이그레이션 실패를 무시한 채 빈약한 스키마로 API가 열리지 않게 한다."""
    events = []
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("PORT", "9000")
    monkeypatch.setattr(serve, "load_local_env", lambda: None)
    monkeypatch.setattr(serve, "validate_deployment", lambda: None)

    def fail_migration():
        """공개 서버가 실행되기 전에 준비 실패를 재현한다."""
        raise SchemaReadinessError(["agent_device"], {}, False)

    monkeypatch.setattr(serve, "migrate", fail_migration)
    monkeypatch.setattr(serve.uvicorn, "run", lambda *args, **kwargs: events.append("bind"))
    with pytest.raises(SchemaReadinessError):
        serve.main()
    assert events == []


def test_explicit_local_start_does_not_mutate_database(monkeypatch):
    """로컬 시작은 기존처럼 별도 명령으로만 마이그레이션을 실행한다."""
    events = []
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("PORT", "9000")
    monkeypatch.delenv("RAILWAY_ENVIRONMENT_ID", raising=False)
    monkeypatch.setattr(serve, "load_local_env", lambda: None)
    monkeypatch.setattr(serve, "validate_deployment", lambda: events.append("validate"))
    monkeypatch.setattr(serve, "migrate", lambda: events.append("migrate"))
    monkeypatch.setattr(serve.uvicorn, "run", lambda *args, **kwargs: events.append("bind"))
    serve.main()
    assert events == ["validate", "bind"]


def test_diagnostic_connection_failure_does_not_print_configuration(monkeypatch, capsys):
    """잘못된 접속 설정도 진단 JSON에 원문이나 비밀값을 출력하지 않는다."""
    def fail_connection():
        """출력되면 안 되는 설정 오류 원문을 재현한다."""
        raise ValueError("private-connection-secret")

    monkeypatch.setattr(schema_readiness, "configured_engine", fail_connection)
    assert schema_readiness.main() == 2
    output = capsys.readouterr().out
    assert "private-connection-secret" not in output
    result = json.loads(output)
    assert result["status"] == "database_unavailable"
    assert result["revision_matches"] is None


def test_ready_diagnostic_is_read_only(database, monkeypatch, capsys):
    """전체 스키마가 준비된 진단은 리비전과 준비 상태만 출력한다."""
    engine, _, _ = database
    monkeypatch.setattr(schema_readiness, "configured_engine", lambda: engine)
    assert schema_readiness.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {"status": "ready", "missing_tables": [], "missing_columns": {},
        "revision_matches": True, "expected_heads": list(expected_schema_heads())}
