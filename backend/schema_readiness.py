"""현재 코드에 필요한 전체 스키마와 Alembic 리비전을 읽기 전용으로 확인한다."""
import json
from functools import lru_cache

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.exc import SQLAlchemyError

from backend.db import configured_engine
from backend.models import Base
from backend.settings import PROJECT_ROOT


class SchemaReadinessError(RuntimeError):
    """접속 정보나 DB 원문을 포함하지 않는 스키마 준비 오류다."""

    def __init__(self, missing_tables, missing_columns, revision_matches):
        """코드에 정의된 테이블과 열 이름만 배포 진단에 표시한다."""
        self.missing_tables = tuple(missing_tables)
        self.missing_columns = {table: tuple(columns) for table, columns in missing_columns.items()}
        self.revision_matches = revision_matches
        details = []
        if missing_tables:
            details.append("missing tables: " + ", ".join(missing_tables))
        if missing_columns:
            details.append("missing columns: " + ", ".join(
                table + "." + column for table, columns in missing_columns.items() for column in columns))
        if not revision_matches:
            details.append("Alembic revision is not at the required head")
        super().__init__("Database migration required (" + "; ".join(details) + ").")


@lru_cache(maxsize=1)
def expected_schema_heads():
    """DB 연결 없이 현재 배포 파일이 요구하는 Alembic head를 읽는다."""
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    return tuple(sorted(ScriptDirectory.from_config(config).get_heads()))


def assert_schema_ready(connection):
    """모든 모델의 테이블과 열 및 migration head가 준비되지 않으면 명확히 실패한다."""
    inspector = inspect(connection)
    existing = set(inspector.get_table_names())
    required = {table.name: {column.name for column in table.columns} for table in Base.metadata.sorted_tables}
    missing_tables = sorted(set(required) - existing)
    missing_columns = {}
    for name in sorted(set(required) & existing):
        absent = sorted(required[name] - {column["name"] for column in inspector.get_columns(name)})
        if absent:
            missing_columns[name] = absent
    current_heads = ()
    if "alembic_version" in existing:
        if "version_num" in {column["name"] for column in inspector.get_columns("alembic_version")}:
            current_heads = tuple(sorted(MigrationContext.configure(connection).get_current_heads()))
        else:
            missing_columns["alembic_version"] = ["version_num"]
    else:
        missing_tables.append("alembic_version")
    revision_matches = current_heads == expected_schema_heads()
    if missing_tables or missing_columns or not revision_matches:
        raise SchemaReadinessError(sorted(missing_tables), missing_columns, revision_matches)


def main():
    """접속 정보나 사용자 데이터 없이 읽기 전용 스키마 진단을 JSON으로 출력한다."""
    result = {"status": "ready", "missing_tables": [], "missing_columns": {},
        "revision_matches": True, "expected_heads": list(expected_schema_heads())}
    engine = None
    code = 0
    try:
        engine = configured_engine()
        with engine.connect() as connection:
            assert_schema_ready(connection)
    except SchemaReadinessError as error:
        result.update(status="migration_required", missing_tables=list(error.missing_tables),
            missing_columns=error.missing_columns, revision_matches=error.revision_matches)
        code = 1
    except (SQLAlchemyError, ValueError):
        result.update(status="database_unavailable", revision_matches=None)
        code = 2
    finally:
        if engine is not None:
            engine.dispose()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
