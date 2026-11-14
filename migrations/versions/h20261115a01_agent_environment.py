"""환경 스캔 결과를 기기와 작업에 연결하고 정제한 메타데이터를 저장한다."""
from alembic import op
import sqlalchemy as sa

revision = "h20261115a01"
down_revision = "g20261115a01"
branch_labels = None
depends_on = None


def upgrade():
    """기존 로컬 환경 기록을 보존하며 기기 스캔 결과 필드를 추가한다."""
    op.add_column("environment", sa.Column("device_id", sa.String(), nullable=True))
    op.add_column("environment", sa.Column("scan_job_id", sa.String(), nullable=True))
    op.add_column("environment", sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")))
    op.alter_column("environment", "metadata_json", server_default=None)
    op.create_foreign_key("fk_environment_device_owner", "environment", "agent_device",
        ["device_id", "profile_id"], ["id", "profile_id"])
    op.create_foreign_key("fk_environment_scan_job", "environment", "agent_job", ["scan_job_id"], ["id"])
    op.create_unique_constraint("uq_environment_scan_job", "environment", ["scan_job_id"])


def downgrade():
    """기존 환경 필드는 유지하고 기기 스캔 연결과 메타데이터 필드만 제거한다."""
    op.drop_constraint("uq_environment_scan_job", "environment", type_="unique")
    op.drop_constraint("fk_environment_scan_job", "environment", type_="foreignkey")
    op.drop_constraint("fk_environment_device_owner", "environment", type_="foreignkey")
    op.drop_column("environment", "metadata_json")
    op.drop_column("environment", "scan_job_id")
    op.drop_column("environment", "device_id")
