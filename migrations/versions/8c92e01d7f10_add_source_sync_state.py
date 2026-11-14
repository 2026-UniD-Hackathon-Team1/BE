"""수집 성공 시점의 현재 버전과 커밋을 소스별로 보관한다."""
from alembic import op
import sqlalchemy as sa

revision = "8c92e01d7f10"
down_revision = "59cebd4dfa30"
branch_labels = None
depends_on = None


def upgrade():
    """기존 소스에도 빈 상태를 채워 수집 커서를 추가한다."""
    op.add_column("source", sa.Column("sync_state", sa.JSON(), nullable=False, server_default="{}"))


def downgrade():
    """수집 상태 컬럼을 제거하고 이전 스키마로 되돌린다."""
    op.drop_column("source", "sync_state")
