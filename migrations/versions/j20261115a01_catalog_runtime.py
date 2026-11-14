"""수집 대기와 요청 총량 및 예약 상태를 프로세스와 분리해 보존한다."""
from alembic import op
import sqlalchemy as sa

revision = "j20261115a01"
down_revision = "i20261115a01"
branch_labels = None
depends_on = None


def upgrade():
    """기본 비활성 예약과 요청 제한을 담을 상태 테이블을 만든다."""
    op.create_table("catalog_runtime",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("state_json", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False))


def downgrade():
    """수집 원본과 작업 이력은 보존하고 운영 상태 테이블만 제거한다."""
    op.drop_table("catalog_runtime")
