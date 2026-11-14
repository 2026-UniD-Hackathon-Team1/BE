"""Idempotent synthetic catalog for offline API development, never executable."""
import hashlib

from sqlalchemy import select
from backend.db import local_engine, session_factory
from backend.models import Asset, AssetVersion, ChangeEvent, Profile, Source


def seed(session):
    """기존 데이터를 유지하면서 누락된 데모 프로필과 후보 10건을 추가한다."""
    if session.get(Profile, "local") is None:
        session.add(Profile(id="local", role="backend developer", primary_language="python",
                            task_types=["code_review"], goals=["input_validation"]))
    if session.get(Source, "demo") is None:
        session.add(Source(id="demo", kind="demo", url="https://example.invalid/demo", enabled=False))
    session.flush()
    for index in range(1, 11):
        asset_id, version_id = f"demo_asset_{index:02}", f"demo_ver_{index:02}"
        if session.get(Asset, asset_id) is not None:
            continue
        task = "code_review" if index <= 5 else "test_generation"
        session.add(Asset(id=asset_id, kind="skill", canonical_key=f"demo:skill-{index}",
                          display_name=f"Demo {task} {index}", source_id="demo"))
        session.flush()
        session.add(AssetVersion(id=version_id, asset_id=asset_id, revision="demo-v1",
                    bundle_sha256=hashlib.sha256(f"demo-{index}".encode()).hexdigest(),
                    metadata_json={"demo": True, "source_verified": False},
                    tags={"task_types": [task], "capabilities": ["input_validation"],
                          "languages": ["python"], "supported_tools": ["codex"]}))
        session.flush()
        session.add(ChangeEvent(id=f"demo_change_{index:02}", asset_id=asset_id,
                    to_version_id=version_id, change_type="new", source_url="https://example.invalid/demo"))
    session.flush()


if __name__ == "__main__":
    engine = local_engine()
    with session_factory(engine).begin() as session:
        seed(session)
    engine.dispose()
