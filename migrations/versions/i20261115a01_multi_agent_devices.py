"""Allow multiple paired devices with one primary job runner."""
from alembic import op
import sqlalchemy as sa

revision = "k20261115a01"
down_revision = "j20261115a01"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_index("uq_active_agent_profile", table_name="agent_device")
    op.add_column("agent_device", sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.execute("""
        UPDATE agent_device AS device SET is_primary = TRUE
        WHERE device.id = (
            SELECT newest.id FROM agent_device AS newest
            WHERE newest.profile_id = device.profile_id AND newest.revoked_at IS NULL
            ORDER BY newest.last_seen_at DESC, newest.id DESC LIMIT 1
        )
    """)
    op.create_index("uq_primary_agent_profile", "agent_device", ["profile_id"], unique=True,
        postgresql_where=sa.text("revoked_at IS NULL AND is_primary"))
    op.create_index("uq_running_job_profile", "agent_job", ["profile_id"], unique=True,
        postgresql_where=sa.text("status = 'running'"))


def downgrade():
    op.drop_index("uq_running_job_profile", table_name="agent_job")
    op.drop_index("uq_primary_agent_profile", table_name="agent_device")
    op.drop_column("agent_device", "is_primary")
    op.create_index("uq_active_agent_profile", "agent_device", ["profile_id"], unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"))
