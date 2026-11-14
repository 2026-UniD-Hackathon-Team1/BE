"""사용자와 기기에 귀속된 영속 작업 큐를 추가한다."""
from alembic import op
import sqlalchemy as sa

revision = "g20261115a01"
down_revision = "f20261115a01"
branch_labels = None
depends_on = None


def upgrade():
    """기기 소유권과 요청 멱등성 및 동시 실행 제약을 갖는 작업 테이블을 만든다."""
    op.create_unique_constraint("uq_agent_device_owner", "agent_device", ["id", "profile_id"])
    op.create_table("agent_job",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("profile_id", sa.String(), nullable=False),
        sa.Column("device_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("request_key", sa.String(), nullable=False),
        sa.Column("request_hash", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("claim_token_hash", sa.String(), nullable=True),
        sa.Column("lease_expires_at", sa.String(), nullable=True),
        sa.Column("progress_step", sa.String(), nullable=True),
        sa.Column("progress_detail", sa.Text(), nullable=True),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("result_hash", sa.String(), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.Column("started_at", sa.String(), nullable=True),
        sa.Column("finished_at", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["device_id", "profile_id"], ["agent_device.id", "agent_device.profile_id"]),
        sa.UniqueConstraint("profile_id", "request_key", name="uq_agent_job_request"),
        sa.CheckConstraint("kind IN ('scan', 'suggest_inputs', 'snapshot_inputs', 'run_comparison', 'apply', 'rollback')", name="ck_agent_job_kind"),
        sa.CheckConstraint("status IN ('queued', 'running', 'done', 'failed', 'cancelled')", name="ck_agent_job_status"),
        sa.CheckConstraint("attempts >= 0", name="ck_agent_job_attempts"))
    op.create_index("ix_agent_job_device_status", "agent_job", ["device_id", "status", "created_at"])
    op.create_index("uq_running_job_device", "agent_job", ["device_id"], unique=True,
                    postgresql_where=sa.text("status = 'running'"))


def downgrade():
    """기존 연결 정보는 보존하고 작업 큐와 소유권 보조 제약을 제거한다."""
    op.drop_table("agent_job")
    op.drop_constraint("uq_agent_device_owner", "agent_device", type_="unique")
