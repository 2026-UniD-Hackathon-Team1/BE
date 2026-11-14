"""create comparison runtime"""

from alembic import op
import sqlalchemy as sa


revision = "b20261114c01"
down_revision = "59cebd4dfa30"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "comparison",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("profile_id", sa.String(), nullable=False),
        sa.Column("recommendation_id", sa.String(), nullable=True),
        sa.Column("method_id", sa.String(), nullable=False),
        sa.Column("method_version", sa.Integer(), nullable=False),
        sa.Column("candidate_version_id", sa.String(), nullable=False),
        sa.Column("input_manifest_json", sa.JSON(), nullable=False),
        sa.Column("input_sha256", sa.String(), nullable=False),
        sa.Column("plan_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("user_decision", sa.String(), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.String(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("completed_at", sa.String(), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'evaluating', 'done', 'failed', 'cancelled')"
        ),
        sa.CheckConstraint(
            "user_decision IS NULL OR user_decision IN ('baseline', 'candidate', 'abstain')"
        ),
        sa.ForeignKeyConstraint(["profile_id"], ["profile.id"]),
        sa.ForeignKeyConstraint(["recommendation_id"], ["recommendation.id"]),
        sa.ForeignKeyConstraint(
            ["method_id", "method_version"],
            ["work_method.id", "work_method.version"],
        ),
        sa.ForeignKeyConstraint(["candidate_version_id"], ["asset_version.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_comparison_profile", "comparison", ["profile_id", "created_at"]
    )
    op.create_table(
        "run",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("comparison_id", sa.String(), nullable=False),
        sa.Column("arm", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("skill_usage_status", sa.String(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("event_log_path", sa.Text(), nullable=True),
        sa.Column("metrics_json", sa.JSON(), nullable=False),
        sa.Column("failure_code", sa.String(), nullable=True),
        sa.Column("started_at", sa.String(), nullable=True),
        sa.Column("ended_at", sa.String(), nullable=True),
        sa.CheckConstraint("arm IN ('baseline', 'candidate')"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', 'cancelled', 'timeout', 'interrupted')"
        ),
        sa.CheckConstraint(
            "skill_usage_status IN ('not_expected', 'confirmed', 'unconfirmed')"
        ),
        sa.ForeignKeyConstraint(["comparison_id"], ["comparison.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("comparison_id", "arm"),
    )
    op.create_table(
        "finding",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("comparison_id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=True),
        sa.Column("canonical_group", sa.String(), nullable=True),
        sa.Column("path", sa.Text(), nullable=True),
        sa.Column("line_number", sa.Integer(), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("verification", sa.String(), nullable=False),
        sa.Column("verification_evidence_json", sa.JSON(), nullable=False),
        sa.Column("reviewed_at", sa.String(), nullable=True),
        sa.CheckConstraint(
            "verification IN ('unverified', 'confirmed', 'false_positive')"
        ),
        sa.ForeignKeyConstraint(["comparison_id"], ["comparison.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["run.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "snapshot",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("target_path", sa.Text(), nullable=False),
        sa.Column("backup_path", sa.Text(), nullable=False),
        sa.Column("before_sha256", sa.String(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "apply_record",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("comparison_id", sa.String(), nullable=False),
        sa.Column("snapshot_id", sa.String(), nullable=False),
        sa.Column("candidate_version_id", sa.String(), nullable=False),
        sa.Column("target_path", sa.Text(), nullable=False),
        sa.Column("applied_sha256", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("applied_at", sa.String(), nullable=True),
        sa.Column("rolled_back_at", sa.String(), nullable=True),
        sa.CheckConstraint(
            "status IN ('applying', 'applied', 'rolled_back', 'recovery_required')"
        ),
        sa.ForeignKeyConstraint(["comparison_id"], ["comparison.id"]),
        sa.ForeignKeyConstraint(["snapshot_id"], ["snapshot.id"]),
        sa.ForeignKeyConstraint(["candidate_version_id"], ["asset_version.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "event_log",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("profile_id", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("subject_id", sa.String(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["profile_id"], ["profile.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade():
    op.drop_table("event_log")
    op.drop_table("apply_record")
    op.drop_table("snapshot")
    op.drop_table("finding")
    op.drop_table("run")
    op.drop_index("idx_comparison_profile", table_name="comparison")
    op.drop_table("comparison")
