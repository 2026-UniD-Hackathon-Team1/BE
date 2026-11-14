"""회원가입한 사용자(아이디·비밀번호 해시)와 웹 세션을 저장한다."""
from alembic import op
import sqlalchemy as sa

revision = "k20261115a01"
down_revision = "j20261115a01"
branch_labels = None
depends_on = None


def upgrade():
    """로그인 관련 테이블만 추가하고 기존 로컬 사용자 데이터는 그대로 둔다."""
    op.create_table("user_account",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("username", sa.String(), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False))
    op.create_table("user_session",
        sa.Column("token_hash", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("user_account.id"), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("expires_at", sa.String(), nullable=False),
        sa.Column("revoked_at", sa.String(), nullable=True))
    op.create_index("ix_user_session_user_id", "user_session", ["user_id"])


def downgrade():
    """로그인 테이블만 제거한다."""
    op.drop_index("ix_user_session_user_id", table_name="user_session")
    op.drop_table("user_session")
    op.drop_table("user_account")
