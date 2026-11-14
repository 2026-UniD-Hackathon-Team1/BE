"""배포 설정을 확인한 뒤 A/B 전체 스키마를 같은 DB에 적용한다."""
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from backend.db import configured_engine
from backend.schema_readiness import assert_schema_ready
from backend.settings import PROJECT_ROOT, database_url, validate_deployment


def migrate():
    """마이그레이션을 직렬 적용하고 전체 스키마가 준비되지 않으면 배포를 중단한다."""
    validate_deployment()
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.attributes["database_url"] = database_url()
    engine = configured_engine(config.attributes["database_url"])
    try:
        with engine.begin() as connection:
            # 같은 DB의 시작 프로세스가 동시에 DDL을 적용하지 않도록 트랜잭션 잠금을 잡는다.
            connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 7843206111501})
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            assert_schema_ready(connection)
    finally:
        engine.dispose()


def main():
    """배포 단계와 서버 시작이 같은 마이그레이션 함수를 사용한다."""
    migrate()


if __name__ == "__main__":
    main()
