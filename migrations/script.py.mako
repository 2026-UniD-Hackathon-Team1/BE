"""${message}"""
from alembic import op
import sqlalchemy as sa
${imports if imports else ""}
revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}

def upgrade():
    """이번 버전의 스키마 변경을 적용한다."""
    ${upgrades if upgrades else "pass"}

def downgrade():
    """이번 버전의 스키마 변경을 되돌린다."""
    ${downgrades if downgrades else "pass"}
