"""A-owned catalog and user context. Version IDs remain stable for B."""
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, CheckConstraint, ForeignKey, ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> str:
    """저장용 현재 시각을 UTC ISO 8601 문자열로 반환한다."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class Base(DeclarativeBase):
    pass


class Profile(Base):
    __tablename__ = "profile"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    role: Mapped[str] = mapped_column(Text)
    primary_language: Mapped[str] = mapped_column(String, default="python")
    task_types: Mapped[list] = mapped_column("task_types_json", JSON)
    goals: Mapped[list] = mapped_column("goals_json", JSON)
    preferences: Mapped[dict] = mapped_column("preferences_json", JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[str] = mapped_column(String, default=utc_now)


class AgentPairingCode(Base):
    __tablename__ = "agent_pairing_code"
    code_hash: Mapped[str] = mapped_column(String, primary_key=True)
    profile_id: Mapped[str] = mapped_column(ForeignKey("profile.id"), index=True)
    expires_at: Mapped[str] = mapped_column(String)
    consumed_at: Mapped[str | None] = mapped_column(String)


class AgentDevice(Base):
    __tablename__ = "agent_device"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    profile_id: Mapped[str] = mapped_column(ForeignKey("profile.id"))
    token_hash: Mapped[str] = mapped_column(String, unique=True)
    device_name: Mapped[str] = mapped_column(String)
    agent_version: Mapped[str] = mapped_column(String)
    codex_version: Mapped[str | None] = mapped_column(String)
    codex_ready: Mapped[bool] = mapped_column(Boolean, default=False)
    last_seen_at: Mapped[str] = mapped_column(String)
    revoked_at: Mapped[str | None] = mapped_column(String)
    __table_args__ = (Index("uq_active_agent_profile", "profile_id", unique=True,
        sqlite_where=text("revoked_at IS NULL"), postgresql_where=text("revoked_at IS NULL")),)


class AgentRateLimit(Base):
    __tablename__ = "agent_rate_limit"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    attempts: Mapped[int] = mapped_column(Integer)
    reset_at: Mapped[int] = mapped_column(Integer)


class Environment(Base):
    __tablename__ = "environment"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    profile_id: Mapped[str] = mapped_column(ForeignKey("profile.id"))
    runner: Mapped[str] = mapped_column(String)
    runner_version: Mapped[str] = mapped_column(String)
    model_id: Mapped[str] = mapped_column(String)
    installed_skills: Mapped[list] = mapped_column("installed_skills_json", JSON)
    settings_hash: Mapped[str] = mapped_column(String)
    scan_root: Mapped[str] = mapped_column(Text)
    scanned_at: Mapped[str] = mapped_column(String, default=utc_now)


class WorkMethod(Base):
    __tablename__ = "work_method"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[str] = mapped_column(ForeignKey("profile.id"))
    task_type: Mapped[str] = mapped_column(String)
    prompt_text: Mapped[str] = mapped_column(Text)
    skill_version_id: Mapped[str | None] = mapped_column(ForeignKey("asset_version.id"))
    required_checks: Mapped[list] = mapped_column("required_checks_json", JSON, default=list)
    scoring_preferences: Mapped[dict] = mapped_column("scoring_preferences_json", JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[str] = mapped_column(String, default=utc_now)
    __table_args__ = (
        CheckConstraint("version > 0"),
        Index("uq_active_method_task", "profile_id", "task_type", unique=True,
              sqlite_where=text("is_active = 1"), postgresql_where=text("is_active IS TRUE")),
    )


class Source(Base):
    __tablename__ = "source"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String)
    url: Mapped[str] = mapped_column(Text, unique=True)
    etag: Mapped[str | None] = mapped_column(Text)
    last_success_at: Mapped[str | None] = mapped_column(String)
    last_error: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    sync_state: Mapped[dict] = mapped_column(JSON, default=dict)


class Asset(Base):
    __tablename__ = "asset"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String)
    canonical_key: Mapped[str] = mapped_column(Text, unique=True)
    display_name: Mapped[str] = mapped_column(Text)
    source_id: Mapped[str] = mapped_column(ForeignKey("source.id"))


class AssetVersion(Base):
    __tablename__ = "asset_version"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("asset.id"))
    revision: Mapped[str] = mapped_column(String)
    bundle_sha256: Mapped[str] = mapped_column(String)
    artifact_path: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict] = mapped_column(JSON)
    tags: Mapped[dict] = mapped_column("tags_json", JSON)
    verified_source_at: Mapped[str] = mapped_column(String, default=utc_now)
    __table_args__ = (UniqueConstraint("asset_id", "bundle_sha256"),)


class ChangeEvent(Base):
    __tablename__ = "change_event"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("asset.id"))
    from_version_id: Mapped[str | None] = mapped_column(ForeignKey("asset_version.id"))
    to_version_id: Mapped[str] = mapped_column(ForeignKey("asset_version.id"))
    change_type: Mapped[str] = mapped_column(String)
    published_at: Mapped[str | None] = mapped_column(String)
    first_seen_at: Mapped[str] = mapped_column(String, default=utc_now)
    source_url: Mapped[str] = mapped_column(Text)
    __table_args__ = (
        UniqueConstraint("asset_id", "to_version_id", "change_type"),
        CheckConstraint("change_type IN ('new', 'updated')"),
        Index("idx_change_asset", "asset_id", "first_seen_at"),
    )


class CandidateAdapter(Base):
    __tablename__ = "candidate_adapter"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    asset_version_id: Mapped[str] = mapped_column(ForeignKey("asset_version.id"))
    runner: Mapped[str] = mapped_column(String)
    supported_tasks: Mapped[list] = mapped_column("supported_tasks_json", JSON)
    approved_bundle_sha256: Mapped[str] = mapped_column(String)
    support_level: Mapped[str] = mapped_column(String, default="information_only")
    reviewed: Mapped[bool] = mapped_column(Boolean, default=False)
    permissions: Mapped[dict] = mapped_column("permissions_json", JSON, default=dict)
    runtime: Mapped[dict] = mapped_column("runtime_json", JSON, default=dict)
    __table_args__ = (CheckConstraint("support_level IN ('information_only', 'comparable', 'applicable')"),)


class Recommendation(Base):
    __tablename__ = "recommendation"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    profile_id: Mapped[str] = mapped_column(ForeignKey("profile.id"))
    change_id: Mapped[str] = mapped_column(ForeignKey("change_event.id"))
    adapter_id: Mapped[str | None] = mapped_column(ForeignKey("candidate_adapter.id"))
    score: Mapped[int] = mapped_column(Integer)
    reasons: Mapped[list] = mapped_column("reason_json", JSON)
    status: Mapped[str] = mapped_column(String, default="new")
    updated_at: Mapped[str] = mapped_column(String, default=utc_now)
    __table_args__ = (
        UniqueConstraint("profile_id", "change_id"),
        CheckConstraint("status IN ('new', 'snoozed', 'dismissed', 'compared', 'applied')"),
        Index("idx_recommendation_profile", "profile_id", "status", "score"),
    )


class RecommendationDecision(Base):
    __tablename__ = "recommendation_decision"
    recommendation_id: Mapped[str] = mapped_column(ForeignKey("recommendation.id"), primary_key=True)
    profile_id: Mapped[str] = mapped_column(ForeignKey("profile.id"))
    status: Mapped[str] = mapped_column(String)
    reason: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(String, default=utc_now)
    __table_args__ = (CheckConstraint("status IN ('new', 'snoozed', 'dismissed', 'kept')"),)


class Comparison(Base):
    __tablename__ = "comparison"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    profile_id: Mapped[str] = mapped_column(ForeignKey("profile.id"))
    recommendation_id: Mapped[str | None] = mapped_column(ForeignKey("recommendation.id"))
    method_id: Mapped[str] = mapped_column(String)
    method_version: Mapped[int] = mapped_column(Integer)
    candidate_version_id: Mapped[str] = mapped_column(ForeignKey("asset_version.id"))
    input_manifest: Mapped[list] = mapped_column("input_manifest_json", JSON)
    input_sha256: Mapped[str] = mapped_column(String)
    plan: Mapped[dict] = mapped_column("plan_json", JSON)
    status: Mapped[str] = mapped_column(String, default="queued")
    user_decision: Mapped[str | None] = mapped_column(String)
    decision_reason: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String, default=utc_now)
    completed_at: Mapped[str | None] = mapped_column(String)
    __table_args__ = (
        ForeignKeyConstraint(
            ["method_id", "method_version"],
            ["work_method.id", "work_method.version"],
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'evaluating', 'done', 'failed', 'cancelled')"
        ),
        CheckConstraint(
            "user_decision IS NULL OR user_decision IN ('baseline', 'candidate', 'abstain')"
        ),
        Index("idx_comparison_profile", "profile_id", "created_at"),
    )


class Run(Base):
    __tablename__ = "run"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    comparison_id: Mapped[str] = mapped_column(ForeignKey("comparison.id"))
    arm: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="queued")
    skill_usage_status: Mapped[str] = mapped_column(String, default="unconfirmed")
    result_json: Mapped[dict | None] = mapped_column(JSON)
    event_log_path: Mapped[str | None] = mapped_column(Text)
    metrics_json: Mapped[dict] = mapped_column(JSON, default=dict)
    failure_code: Mapped[str | None] = mapped_column(String)
    started_at: Mapped[str | None] = mapped_column(String)
    ended_at: Mapped[str | None] = mapped_column(String)
    __table_args__ = (
        UniqueConstraint("comparison_id", "arm"),
        CheckConstraint("arm IN ('baseline', 'candidate')"),
        CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', 'cancelled', 'timeout', 'interrupted')"
        ),
        CheckConstraint(
            "skill_usage_status IN ('not_expected', 'confirmed', 'unconfirmed')"
        ),
    )


class Finding(Base):
    __tablename__ = "finding"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    comparison_id: Mapped[str] = mapped_column(ForeignKey("comparison.id"))
    run_id: Mapped[str | None] = mapped_column(ForeignKey("run.id"))
    canonical_group: Mapped[str | None] = mapped_column(String)
    path: Mapped[str | None] = mapped_column(Text)
    line_number: Mapped[int | None] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(Text)
    verification: Mapped[str] = mapped_column(String, default="unverified")
    verification_evidence: Mapped[dict] = mapped_column(
        "verification_evidence_json", JSON, default=dict
    )
    reviewed_at: Mapped[str | None] = mapped_column(String)
    __table_args__ = (
        CheckConstraint(
            "verification IN ('unverified', 'confirmed', 'false_positive')"
        ),
    )


class Snapshot(Base):
    __tablename__ = "snapshot"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    target_path: Mapped[str] = mapped_column(Text)
    backup_path: Mapped[str] = mapped_column(Text)
    before_sha256: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String, default=utc_now)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)


class ApplyRecord(Base):
    __tablename__ = "apply_record"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    comparison_id: Mapped[str] = mapped_column(ForeignKey("comparison.id"))
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("snapshot.id"))
    candidate_version_id: Mapped[str] = mapped_column(ForeignKey("asset_version.id"))
    target_path: Mapped[str] = mapped_column(Text)
    applied_sha256: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="applying")
    applied_at: Mapped[str | None] = mapped_column(String)
    rolled_back_at: Mapped[str | None] = mapped_column(String)
    __table_args__ = (
        CheckConstraint(
            "status IN ('applying', 'applied', 'rolled_back', 'recovery_required')"
        ),
    )


class EventLog(Base):
    __tablename__ = "event_log"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    profile_id: Mapped[str] = mapped_column(ForeignKey("profile.id"))
    event_type: Mapped[str] = mapped_column(String)
    subject_id: Mapped[str] = mapped_column(String)
    payload: Mapped[dict] = mapped_column("payload_json", JSON)
    created_at: Mapped[str] = mapped_column(String, default=utc_now)
