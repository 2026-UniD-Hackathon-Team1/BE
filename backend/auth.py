"""로컬 단일 사용자 API의 토큰 검사. 공유 배포 인증과는 구분한다."""
import secrets

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader

local_token_header = APIKeyHeader(name="X-Local-Token", auto_error=False)


def local_auth(expected_token: str | None):
    """앱에 명시적으로 설정한 로컬 토큰으로 검사 의존성을 만든다."""
    def require_local_token(provided: str | None = Security(local_token_header)):
        """토큰 설정이 없으면 개인 API를 닫고 값이 다르면 인증 오류를 반환한다."""
        if not expected_token:
            raise HTTPException(503, detail={"code": "local_token_not_configured", "message": "서버의 APP_LOCAL_TOKEN을 설정해 주세요."})
        if not provided or not secrets.compare_digest(provided.encode("utf-8"), expected_token.encode("utf-8")):
            raise HTTPException(401, detail={"code": "invalid_local_token", "message": "로컬 연결 토큰을 확인해 주세요."})
        return "local"
    return require_local_token
