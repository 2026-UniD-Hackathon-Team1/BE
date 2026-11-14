"""Local SQLite connections; schema changes are owned by Alembic."""
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker


def make_engine(url: str = "sqlite:///data/galatalkka.db"):
    """외래키 검사와 잠금 대기 설정을 적용한 SQLite 엔진을 만든다."""
    if not url.startswith("sqlite:"):
        raise ValueError("Only local SQLite is supported")
    engine = create_engine(url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def configure_sqlite(connection, _):
        """새 DB 연결마다 외래키 검사와 최대 잠금 대기 시간을 설정한다."""
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    return engine


def local_engine():
    """프로젝트 데이터 폴더를 준비하고 기본 로컬 DB 엔진을 만든다."""
    Path("data").mkdir(exist_ok=True)
    return make_engine()


def session_factory(engine):
    """커밋 후에도 조회한 값을 유지하는 세션 팩터리를 만든다."""
    return sessionmaker(engine, expire_on_commit=False)
