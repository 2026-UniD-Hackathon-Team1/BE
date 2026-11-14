"""PostgreSQL 전용 연결. 스키마 변경은 Alembic으로 관리한다."""
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from backend.settings import database_url


def make_engine(url: str):
    """PostgreSQL URL을 psycopg 드라이버로 통일하고 다른 DB는 거부한다."""
    parsed = make_url(url)
    if parsed.drivername not in {"postgres", "postgresql", "postgresql+psycopg"}:
        raise ValueError("Only PostgreSQL is supported")
    return create_engine(parsed.set(drivername="postgresql+psycopg"), pool_pre_ping=True,
                         connect_args={"connect_timeout": 10})


def configured_engine(url: str | None = None):
    """명시한 URL 또는 공통 DATABASE_URL로 PostgreSQL 엔진을 구성한다."""
    return make_engine(database_url(url))


def session_factory(engine):
    """커밋 후에도 조회한 값을 유지하는 세션 팩터리를 만든다."""
    return sessionmaker(engine, expire_on_commit=False)
