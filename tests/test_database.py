import pytest
from alembic import command
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from backend.models import Asset, AssetVersion, ChangeEvent, Profile, WorkMethod
from backend.seed import seed


def test_migration_and_idempotent_seed(database):
    engine, sessions, config = database
    for _ in range(2):
        with sessions.begin() as session:
            seed(session)
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(Profile)) == 1
        assert session.scalar(select(func.count()).select_from(ChangeEvent)) == 10
        assert session.scalar(select(func.count()).select_from(AssetVersion)) == 10
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
        assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1
    command.downgrade(config, "base")
    assert inspect(engine).get_table_names() == ["alembic_version"]
    command.upgrade(config, "head")
    assert "asset_version" in inspect(engine).get_table_names()


def test_foreign_keys_and_duplicate_versions_are_rejected(database):
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
    _, sessions, _ = database
    with sessions.begin() as session:
        seed(session)
    with pytest.raises(IntegrityError), sessions.begin() as session:
        for index in range(2):
            session.add(WorkMethod(id=f"wm_{index}", version=1, profile_id="local",
                                  task_type="code_review", prompt_text="review", is_active=True))
