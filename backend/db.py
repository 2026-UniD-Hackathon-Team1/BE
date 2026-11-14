"""SQLite와 PostgreSQL의 공통 연결. 스키마 변경은 Alembic으로 관리한다."""
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from backend.settings import database_url


def make_engine(url: str):
    """DB 종류별 연결 설정을 적용하고 PostgreSQL URL을 psycopg 드라이버로 통일한다."""
    parsed = make_url(url)
    if parsed.drivername in {"postgres", "postgresql", "postgresql+psycopg"}:
        return create_engine(parsed.set(drivername="postgresql+psycopg"), pool_pre_ping=True,
                             connect_args={"connect_timeout": 10})
    if parsed.get_backend_name() != "sqlite":
        raise ValueError("Only SQLite and PostgreSQL are supported")
    engine = create_engine(parsed, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def configure_sqlite(connection, _):
        """새 DB 연결마다 외래키 검사와 최대 잠금 대기 시간을 설정한다."""
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    return engine


def configured_engine(url: str | None = None):
    """공통 DATABASE_URL을 읽고 로컬 SQLite인 경우에만 데이터 폴더를 준비한다."""
    value = database_url(url)
    parsed = make_url(value)
    if parsed.get_backend_name() == "sqlite" and parsed.database not in {None, "", ":memory:"}:
        Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
    return make_engine(value)


def local_engine():
    """기존 호출과 호환되도록 공통 설정 기반 엔진을 반환한다."""
    return configured_engine()


def session_factory(engine):
    """커밋 후에도 조회한 값을 유지하는 세션 팩터리를 만든다."""
    return sessionmaker(engine, expire_on_commit=False)
