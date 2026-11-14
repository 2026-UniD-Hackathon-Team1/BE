"""프로필 맥락과 작업 단계 및 발견 당시 맥락을 추가한다."""
from alembic import op
import sqlalchemy as sa

revision = "m20261115a01"
down_revision = "l20261115a01"
branch_labels = None
depends_on = None


def upgrade():
    """기존 프로필과 불변 작업 방식에 빈 맥락을 채워 호환성을 유지한다."""
    op.add_column("profile", sa.Column("context_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))
    op.add_column("work_method", sa.Column("workflow_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))
    op.add_column("catalog_sync_job", sa.Column("context_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))


def downgrade():
    """이번 변경에서 추가한 맥락만 제거한다."""
    op.drop_column("catalog_sync_job", "context_snapshot")
    op.drop_column("work_method", "workflow_json")
    op.drop_column("profile", "context_json")
