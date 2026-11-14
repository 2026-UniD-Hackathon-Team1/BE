"""로컬과 배포 환경에서 공유하는 연결 및 실행 설정."""
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def database_url(override: str | None = None) -> str:
    """로컬과 배포 모두 명시한 PostgreSQL 연결만 허용한다."""
    value = override or os.environ.get("DATABASE_URL")
    if not value:
        raise ValueError("DATABASE_URL must be configured")
    if not value.startswith(("postgresql://", "postgres://", "postgresql+psycopg://")):
        raise ValueError("PostgreSQL DATABASE_URL is required")
    return value


def validate_deployment():
    """공개 개발 서버 시작 전에 PostgreSQL과 연결 토큰 및 CORS 설정을 검사한다."""
    value = database_url()
    if os.environ.get("APP_ENV") in {"staging", "production"} or os.environ.get("RAILWAY_ENVIRONMENT_ID"):
        if not value.startswith(("postgresql://", "postgres://", "postgresql+psycopg://")):
            raise ValueError("Deployment requires PostgreSQL")
        if len(os.environ.get("APP_LOCAL_TOKEN", "")) < 32:
            raise ValueError("APP_LOCAL_TOKEN must contain at least 32 characters for deployment")
        origins = [part.strip() for part in os.environ.get("CORS_ORIGINS", "").split(",") if part.strip()]
        if not origins or "*" in origins:
            raise ValueError("Explicit CORS_ORIGINS must be configured for deployment")
