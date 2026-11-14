import pytest
from alembic import command
from alembic.config import Config
from backend.db import make_engine, session_factory


@pytest.fixture
def database(tmp_path):
    """임시 DB에 실제 마이그레이션을 적용하고 테스트용 연결을 제공한다."""
    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    engine = make_engine(url)
    yield engine, session_factory(engine), config
    engine.dispose()
