"""배포 설정을 확인한 뒤 A/B 전체 스키마를 같은 DB에 적용한다."""
from alembic import command
from alembic.config import Config

from backend.settings import PROJECT_ROOT, database_url, validate_deployment


def main():
    """공통 URL로 마이그레이션을 실행하며 실패하면 배포를 중단한다."""
    validate_deployment()
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.attributes["database_url"] = database_url()
    command.upgrade(config, "head")


if __name__ == "__main__":
    main()
