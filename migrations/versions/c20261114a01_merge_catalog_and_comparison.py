"""수집과 비교 스키마의 병렬 변경을 하나의 마이그레이션 헤드로 합친다."""
revision = "c20261114a01"
down_revision = ("8c92e01d7f10", "b20261114c01")
branch_labels = None
depends_on = None


def upgrade():
    """두 스키마가 모두 적용된 상태를 단일 기준점으로 기록한다."""
    pass


def downgrade():
    """테이블 변경 없이 두 이전 스키마 분기로 돌아간다."""
    pass
