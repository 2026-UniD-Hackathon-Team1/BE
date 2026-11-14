import os
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema

from backend.db import make_engine, session_factory


def pytest_configure(config):
    """명시한 테스트 PostgreSQL만 사용하고 미설정 시 실행 방법을 안내한다."""
    url = os.environ.get("POSTGRES_TEST_URL")
    if not url:
        raise pytest.UsageError("POSTGRES_TEST_URL에 별도 PostgreSQL 테스트 DB를 지정해 주세요.")
    if make_url(url).drivername not in {"postgres", "postgresql", "postgresql+psycopg"}:
        raise pytest.UsageError("POSTGRES_TEST_URL은 PostgreSQL이어야 합니다.")
    os.environ["DATABASE_URL"] = url


@pytest.fixture
def empty_database():
    """테스트마다 고유 PostgreSQL 스키마를 생성하고 그 스키마만 제거한다."""
    parsed = make_url(os.environ["POSTGRES_TEST_URL"])
    schema = "galatalkka_test_" + uuid4().hex
    admin = make_engine(parsed.render_as_string(hide_password=False))
    with admin.begin() as connection:
        connection.execute(CreateSchema(schema))
    options = parsed.query.get("options", "") + f" -csearch_path={schema}"
    url = parsed.update_query_dict({"options": options.strip()}).render_as_string(hide_password=False)
    engine = make_engine(url)
    config = Config("alembic.ini")
    config.attributes["database_url"] = url
    try:
        yield engine, session_factory(engine), config
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        admin.dispose()


@pytest.fixture
def database(empty_database):
    """실제 PostgreSQL의 테스트 전용 스키마에 전체 마이그레이션을 적용한다."""
    command.upgrade(empty_database[2], "head")
    return empty_database
