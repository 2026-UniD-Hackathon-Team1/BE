import pytest
from alembic import command
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from backend.models import (
    ApplyRecord,
    AssetVersion,
    CandidateAdapter,
    Comparison,
    EventLog,
    Finding,
    Profile,
    Recommendation,
    Run,
    Snapshot,
    WorkMethod,
)
from backend.seed import seed


B_TABLES = {
    "comparison",
    "run",
    "finding",
    "snapshot",
    "apply_record",
    "event_log",
}


def add_comparison_dependencies(session):
    seed(session)
    session.add(
        WorkMethod(
            id="wm_review",
            version=1,
            profile_id="local",
            task_type="code_review",
            prompt_text="review",
            is_active=True,
        )
    )
    session.add(
        CandidateAdapter(
            id="adapter_01",
            asset_version_id="demo_ver_01",
            runner="codex_cli",
            supported_tasks=["code_review"],
            approved_bundle_sha256=session.get(
                AssetVersion, "demo_ver_01"
            ).bundle_sha256,
            support_level="applicable",
            reviewed=True,
        )
    )
    session.flush()
    session.add(
        Recommendation(
            id="rec_01",
            profile_id="local",
            change_id="demo_change_01",
            adapter_id="adapter_01",
            score=100,
            reasons=["task match"],
        )
    )
    session.flush()


def new_comparison(**overrides):
    values = {
        "id": "cmp_01",
        "profile_id": "local",
        "recommendation_id": "rec_01",
        "method_id": "wm_review",
        "method_version": 1,
        "candidate_version_id": "demo_ver_01",
        "input_manifest": [{"path": "fixture/app.py"}],
        "input_sha256": "a" * 64,
        "plan": {"model_id": "fixed-model"},
        "status": "queued",
    }
    values.update(overrides)
    return Comparison(**values)


def test_b_migration_upgrades_and_downgrades(database):
    engine, _, config = database
    assert B_TABLES <= set(inspect(engine).get_table_names())

    command.downgrade(config, "59cebd4dfa30")
    tables = set(inspect(engine).get_table_names())
    assert not (B_TABLES & tables)
    assert "asset_version" in tables

    command.upgrade(config, "head")
    assert B_TABLES <= set(inspect(engine).get_table_names())


def test_comparison_enforces_foreign_keys_and_decision_values(database):
    _, sessions, _ = database
    with sessions.begin() as session:
        add_comparison_dependencies(session)
        session.add(new_comparison())

    with pytest.raises(IntegrityError), sessions.begin() as session:
        session.add(new_comparison(id="orphan", profile_id="missing"))

    with pytest.raises(IntegrityError), sessions.begin() as session:
        session.add(new_comparison(id="invalid", user_decision="tie"))


def test_run_allows_only_one_arm_and_valid_skill_usage(database):
    _, sessions, _ = database
    with sessions.begin() as session:
        add_comparison_dependencies(session)
        session.add(new_comparison())
        session.flush()
        session.add(
            Run(
                id="run_baseline",
                comparison_id="cmp_01",
                arm="baseline",
                status="completed",
                skill_usage_status="not_expected",
            )
        )

    with pytest.raises(IntegrityError), sessions.begin() as session:
        session.add(
            Run(
                id="run_duplicate",
                comparison_id="cmp_01",
                arm="baseline",
                status="completed",
                skill_usage_status="not_expected",
            )
        )

    with pytest.raises(IntegrityError), sessions.begin() as session:
        session.add(
            Run(
                id="run_invalid",
                comparison_id="cmp_01",
                arm="candidate",
                status="completed",
                skill_usage_status="maybe",
            )
        )

    with sessions() as session:
        assert session.scalar(select(Run.id)) == "run_baseline"
