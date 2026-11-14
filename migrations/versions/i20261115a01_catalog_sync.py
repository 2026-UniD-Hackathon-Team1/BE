"""서버 수집 작업과 요청 키 및 진행 이력을 보존한다."""
from alembic import op
import sqlalchemy as sa

revision = "i20261115a01"
down_revision = "h20261115a01"
branch_labels = None
depends_on = None


def upgrade():
    """PC 작업 큐와 분리된 수집 이력과 전역 활성 슬롯을 만든다."""
    op.create_table("catalog_sync_job",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("profile_id", sa.String(), nullable=False),
        sa.Column("request_key", sa.String(), nullable=False),
        sa.Column("request_hash", sa.String(), nullable=False),
        sa.Column("config_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("active_slot", sa.Integer(), nullable=True, unique=True),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.Column("expires_at", sa.String(), nullable=False),
        sa.UniqueConstraint("profile_id", "request_key", name="uq_catalog_sync_request"),
        sa.CheckConstraint("status IN ('queued', 'running', 'done', 'partial', 'failed', 'interrupted')",
                           name="ck_catalog_sync_status"),
        sa.CheckConstraint("(status IN ('queued', 'running') AND active_slot = 1 AND active_slot IS NOT NULL) OR "
                           "(status NOT IN ('queued', 'running') AND active_slot IS NULL)", name="ck_catalog_sync_slot"))


def downgrade():
    """수집된 후보 원본은 보존하고 작업 이력 테이블만 제거한다."""
    op.drop_table("catalog_sync_job")
