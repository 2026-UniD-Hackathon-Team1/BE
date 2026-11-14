import pytest
from alembic import command
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import IntegrityError
from backend.models import Asset, AssetVersion, ChangeEvent, Profile, WorkMethod
from backend.seed import seed


def test_migration_and_idempotent_seed(database):
    """seed 재실행의 중복 방지와 마이그레이션 왕복 적용을 검증한다."""
    engine, sessions, config = database
    for _ in range(2):
        with sessions.begin() as session:
            seed(session)
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(Profile)) == 1
        assert session.scalar(select(func.count()).select_from(ChangeEvent)) == 10
        assert session.scalar(select(func.count()).select_from(AssetVersion)) == 10
    command.downgrade(config, "base")
    assert inspect(engine).get_table_names() == ["alembic_version"]
    command.upgrade(config, "head")
    assert "asset_version" in inspect(engine).get_table_names()


def test_foreign_keys_and_duplicate_versions_are_rejected(database):
    """잘못된 외래키와 자산 내 동일 콘텐츠 버전이 거부되는지 검증한다."""
    _, sessions, _ = database
    with pytest.raises(IntegrityError), sessions.begin() as session:
        session.add(Asset(id="orphan", kind="skill", canonical_key="orphan", display_name="x", source_id="missing"))
    with sessions.begin() as session:
        seed(session)
    with pytest.raises(IntegrityError), sessions.begin() as session:
        original = session.get(AssetVersion, "demo_ver_01")
        session.add(AssetVersion(id="duplicate", asset_id=original.asset_id, revision="other",
                                bundle_sha256=original.bundle_sha256, metadata_json={}, tags={}))


def test_only_one_active_method_per_task(database):
    """동일 프로필과 작업 유형에는 활성 방식이 하나만 허용되는지 검증한다."""
    _, sessions, _ = database
    with sessions.begin() as session:
        seed(session)
    with pytest.raises(IntegrityError), sessions.begin() as session:
        for index in range(2):
            session.add(WorkMethod(id=f"wm_{index}", version=1, profile_id="local",
                                  task_type="code_review", prompt_text="review", is_active=True))
