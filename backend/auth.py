"""웹 API 인증. 로그인으로 받은 세션(Bearer)과 기존 로컬 토큰(X-Local-Token)을 함께 받는다."""
import hashlib
import secrets

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from backend.models import UserSession, utc_now

local_token_header = APIKeyHeader(name="X-Local-Token", auto_error=False)
session_bearer = HTTPBearer(auto_error=False, scheme_name="LoginSession")

LOCAL_PROFILE_ID = "local"


def token_hash(token: str) -> str:
    """원문 토큰 대신 저장·비교할 해시."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def local_auth(expected_token: str | None):
    """앱에 명시적으로 설정한 로컬 토큰으로 검사 의존성을 만든다."""
    def require_local_token(provided: str | None = Security(local_token_header)):
        """토큰 설정이 없으면 개인 API를 닫고 값이 다르면 인증 오류를 반환한다."""
        if not expected_token:
            raise HTTPException(503, detail={"code": "local_token_not_configured", "message": "서버의 APP_LOCAL_TOKEN을 설정해 주세요."})
        if not provided or not secrets.compare_digest(provided.encode("utf-8"), expected_token.encode("utf-8")):
            raise HTTPException(401, detail={"code": "invalid_local_token", "message": "로컬 연결 토큰을 확인해 주세요."})
        return LOCAL_PROFILE_ID
    return require_local_token


def session_user(sessions, token: str) -> str | None:
    """유효한 로그인 세션이면 사용자 id, 아니면 None."""
    with sessions() as session:
        row = session.get(UserSession, token_hash(token))
        if row is None or row.revoked_at is not None or row.expires_at <= utc_now():
            return None
        return row.user_id


def web_auth(sessions, expected_token: str | None):
    """로그인 세션이 있으면 그 사용자로, 없으면 기존 로컬 토큰으로 인증한다.

    로그인 사용자의 id가 곧 프로필 id라서, 기존 API는 고치지 않아도 사용자별로 데이터가 나뉜다.
    """
    local = local_auth(expected_token)

    def require_user(credentials: HTTPAuthorizationCredentials | None = Security(session_bearer),
                     provided: str | None = Security(local_token_header)):
        if credentials is not None:
            user_id = session_user(sessions, credentials.credentials)
            if user_id is None:
                raise HTTPException(401, detail={"code": "invalid_session", "message": "다시 로그인해 주세요."})
            return user_id
        return local(provided)
    return require_user
