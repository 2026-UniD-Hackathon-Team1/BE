"""PostgreSQL에서도 작업 유형별 활성 방식만 유일하도록 부분 인덱스를 적용한다."""
from alembic import op
import sqlalchemy as sa

revision = "d20261114a01"
down_revision = "c20261114a01"
branch_labels = None
depends_on = None


def upgrade():
    """PostgreSQL의 전체 유일 인덱스를 활성 행 대상 부분 인덱스로 수정한다."""
    if op.get_context().dialect.name == "postgresql":
        op.drop_index("uq_active_method_task", table_name="work_method")
        op.create_index("uq_active_method_task", "work_method", ["profile_id", "task_type"],
                        unique=True, postgresql_where=sa.text("is_active IS TRUE"))


def downgrade():
    """과거 버전 데이터를 제한하지 않도록 활성 행 부분 인덱스를 유지한다."""
    pass
