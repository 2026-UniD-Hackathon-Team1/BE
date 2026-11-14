"""기존 추천 및 B 비교 참조를 보존하며 사용자 선택을 별도 테이블에 저장한다."""
from alembic import op
import sqlalchemy as sa

revision = "e20261114a01"
down_revision = "d20261114a01"
branch_labels = None
depends_on = None


def upgrade():
    """기존 추천 상태 제약을 변경하지 않고 사용자 선택을 저장할 테이블을 만든다."""
    op.create_table("recommendation_decision",
        sa.Column("recommendation_id", sa.String(), primary_key=True),
        sa.Column("profile_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["recommendation_id"], ["recommendation.id"]),
        sa.ForeignKeyConstraint(["profile_id"], ["profile.id"]),
        sa.CheckConstraint("status IN ('new', 'snoozed', 'dismissed', 'kept')"))


def downgrade():
    """기존 추천과 비교 기록은 유지하고 사용자 선택 테이블만 제거한다."""
    op.drop_table("recommendation_decision")
