import io
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.schema import CreateIndex
from sqlalchemy.dialects import postgresql

from backend.db import configured_engine, make_engine
from backend.models import WorkMethod
from backend.settings import database_url, validate_deployment


@pytest.mark.parametrize("scheme", ["postgres", "postgresql", "postgresql+psycopg"])
def test_postgres_url_uses_same_driver_without_connecting(scheme):
    """PostgreSQL URL 표현이 달라도 공통 드라이버를 쓰는지 접속 없이 확인한다."""
    engine = make_engine(f"{scheme}://example:example@localhost/test")
    assert engine.url.drivername == "postgresql+psycopg"
    engine.dispose()


def test_deployment_rejects_missing_database_and_weak_settings(monkeypatch):
    """배포 설정 누락으로 임시 DB나 무인증 개인 API가 실행되지 않도록 검증한다."""
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="DATABASE_URL"):
        database_url()
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    with pytest.raises(ValueError, match="PostgreSQL"):
        validate_deployment()
    monkeypatch.setenv("DATABASE_URL", "postgresql://example:example@localhost/test")
    monkeypatch.setenv("APP_LOCAL_TOKEN", "short")
    with pytest.raises(ValueError, match="APP_LOCAL_TOKEN"):
        validate_deployment()
    monkeypatch.setenv("APP_LOCAL_TOKEN", "test-" + "x" * 32)
    monkeypatch.setenv("CORS_ORIGINS", "*")
    with pytest.raises(ValueError, match="CORS_ORIGINS"):
        validate_deployment()
    monkeypatch.setenv("CORS_ORIGINS", "http://localhost:5173")
    validate_deployment()


def test_common_url_is_used_by_cli_engine(monkeypatch, tmp_path):
    """수집 및 seed에 사용하는 엔진도 공통 DATABASE_URL을 따르는지 검증한다."""
    target = (tmp_path / "nested" / "shared.db").as_posix()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{target}")
    engine = configured_engine()
    assert engine.url.database == target
    assert Path(target).parent.exists()
    engine.dispose()


def test_all_migrations_compile_for_postgres_without_connecting():
    """A/B 전체 스키마와 복합 FK 및 활성 부분 인덱스의 PostgreSQL DDL을 확인한다."""
    output = io.StringIO()
    config = Config("alembic.ini", output_buffer=output)
    config.attributes["database_url"] = "postgresql://example:example@localhost/test"
    command.upgrade(config, "head", sql=True)
    ddl = output.getvalue()
    for table in ("profile", "asset_version", "recommendation", "comparison", "run", "finding", "apply_record", "snapshot", "event_log"):
        assert f"CREATE TABLE {table}" in ddl or f'CREATE TABLE "{table}"' in ddl
    assert "FOREIGN KEY(method_id, method_version)" in ddl
    assert "WHERE is_active IS TRUE" in ddl
    index = next(index for index in WorkMethod.__table__.indexes if index.name == "uq_active_method_task")
    assert "WHERE is_active IS TRUE" in str(CreateIndex(index).compile(dialect=postgresql.dialect()))


