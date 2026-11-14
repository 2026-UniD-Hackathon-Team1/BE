"""연결 코드와 기기 토큰 해시 및 영속 요청 제한을 저장한다."""
from alembic import op
import sqlalchemy as sa

revision = "f20261115a01"
down_revision = "e20261114a01"
branch_labels = None
depends_on = None


def upgrade():
    """사용자별 연결 코드와 기기 및 요청 제한 테이블을 추가한다."""
    op.create_table("agent_pairing_code",
        sa.Column("code_hash", sa.String(), primary_key=True),
        sa.Column("profile_id", sa.String(), sa.ForeignKey("profile.id"), nullable=False),
        sa.Column("expires_at", sa.String(), nullable=False),
        sa.Column("consumed_at", sa.String(), nullable=True))
    op.create_index("ix_agent_pairing_code_profile_id", "agent_pairing_code", ["profile_id"])
    op.create_table("agent_device",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("profile_id", sa.String(), sa.ForeignKey("profile.id"), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False, unique=True),
        sa.Column("device_name", sa.String(), nullable=False),
        sa.Column("agent_version", sa.String(), nullable=False),
        sa.Column("codex_version", sa.String(), nullable=True),
        sa.Column("codex_ready", sa.Boolean(), nullable=False),
        sa.Column("last_seen_at", sa.String(), nullable=False),
        sa.Column("revoked_at", sa.String(), nullable=True))
    op.create_index("uq_active_agent_profile", "agent_device", ["profile_id"], unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"))
    op.create_table("agent_rate_limit",
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("reset_at", sa.Integer(), nullable=False))


def downgrade():
    """기존 서비스 기록을 유지하고 연결 관련 테이블만 제거한다."""
    op.drop_table("agent_rate_limit")
    op.drop_table("agent_device")
    op.drop_table("agent_pairing_code")
