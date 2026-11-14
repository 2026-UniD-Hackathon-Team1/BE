"""회원가입과 로그인.

- POST /api/auth/signup {username, password}: 계정을 만들고 바로 로그인 세션을 준다.
- POST /api/auth/login  {username, password}: 로그인 세션을 준다.
- 받은 토큰은 Authorization: Bearer <토큰> 으로 보낸다 (backend/auth.py).
비밀번호는 원문을 저장하지 않고 scrypt 해시만 저장한다. 로그인 시도는 아이디마다 10분에 10번으로 제한한다.
"""
import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from threading import BoundedSemaphore

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import case, delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from backend.auth import LOCAL_PROFILE_ID, session_bearer, token_hash
from backend.models import AgentRateLimit, UserAccount, UserSession, utc_now
from backend.schemas import ErrorResponse

SESSION_TTL = timedelta(days=30)
LOGIN_LIMIT, LOGIN_WINDOW = 10, 600
SCRYPT = {"n": 2**14, "r": 8, "p": 5, "dklen": 32}
HASH_SLOTS = BoundedSemaphore(2)


def iso(value: datetime) -> str:
    """세션 시각을 비교 가능한 UTC 문자열로 만든다."""
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def derive(password, salt, *, legacy=False):
    """해시 동시 실행을 제한해 공개 인증 요청의 메모리 사용을 제한한다."""
    if not HASH_SLOTS.acquire(blocking=False):
        raise HTTPException(503, detail={"code": "auth_busy", "message": "잠시 후 다시 로그인해 주세요."},
                            headers={"Retry-After": "2"})
    try:
        return hashlib.scrypt(password.encode("utf-8"), salt=salt, **{**SCRYPT, "p": 1 if legacy else 5})
    finally:
        HASH_SLOTS.release()


def hash_password(password: str) -> str:
    """서로 다른 salt와 명시적 비용 매개변수로 비밀번호를 해시한다."""
    salt = secrets.token_bytes(16)
    digest = derive(password, salt)
    return "scrypt$16384$8$5$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str) -> bool:
    """고정된 새 비용과 이전 해시 형식만 허용하고 원문을 그대로 비교한다."""
    try:
        parts = stored.split("$")
        legacy = len(parts) == 3 and parts[0] == "scrypt"
        if not legacy and (len(parts) != 6 or parts[:4] != ["scrypt", "16384", "8", "5"]):
            return False
        salt, digest = parts[-2:]
        expected = base64.b64decode(digest, validate=True)
        decoded_salt = base64.b64decode(salt, validate=True)
        if len(decoded_salt) != 16 or len(expected) != 32:
            return False
        actual = derive(password, decoded_salt, legacy=legacy)
    except (ValueError, TypeError):
        return False
    return secrets.compare_digest(actual, expected)


# 없는 아이디에도 비밀번호 해시 연산을 수행한다.
DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=3, max_length=30, pattern=r"^[a-z0-9_-]+$", examples=["junior_dev"])
    password: str = Field(min_length=8, max_length=128)


class SessionIssued(BaseModel):
    token: str
    user_id: str
    username: str
    expires_at: str


class Me(BaseModel):
    user_id: str
    username: str
    via: str  # account | local


def login_router(sessions, web_auth):
    """회원가입·로그인·내 정보·로그아웃."""
    router = APIRouter(prefix="/api/auth", tags=["auth"],
                       responses={401: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 429: {"model": ErrorResponse}})

    def issue(user_id: str, username: str, response: Response) -> SessionIssued:
        """원문 토큰을 한 번만 반환하고 세션은 해시로 저장하며 캐시를 막는다."""
        token = secrets.token_urlsafe(32)
        expires_at = iso(datetime.now(timezone.utc) + SESSION_TTL)
        with sessions.begin() as session:
            session.add(UserSession(token_hash=token_hash(token), user_id=user_id, expires_at=expires_at))
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return SessionIssued(token=token, user_id=user_id, username=username, expires_at=expires_at)

    def limit_attempt(key, limit, window=LOGIN_WINDOW):
        """비용이 드는 해시 연산 전에 계정과 접속 주소별 영속 제한을 확인한다."""
        now = int(datetime.now(timezone.utc).timestamp())
        key = "auth:" + token_hash(key)
        with sessions.begin() as session:
            session.execute(delete(AgentRateLimit).where(AgentRateLimit.reset_at < now - 3600))
            expired = AgentRateLimit.reset_at <= now
            stmt = pg_insert(AgentRateLimit).values(key=key, attempts=1, reset_at=now + window)
            stmt = stmt.on_conflict_do_update(index_elements=[AgentRateLimit.key], set_={
                "attempts": case((expired, 1), else_=AgentRateLimit.attempts + 1),
                "reset_at": case((expired, now + window), else_=AgentRateLimit.reset_at)})
            attempts = session.scalar(stmt.returning(AgentRateLimit.attempts))
        if attempts > limit:
            raise HTTPException(429, detail={"code": "login_rate_limited", "message": "로그인 시도가 많아요. 10분 뒤 다시 시도해 주세요."},
                                headers={"Retry-After": str(window)})

    @router.post("/signup", response_model=SessionIssued, status_code=201, summary="회원가입 (가입 후 바로 로그인)")
    def signup(body: Credentials, request: Request, response: Response):
        """가입 요청도 주소와 전체 빈도로 제한하고 검증한 계정만 생성한다."""
        address = request.client.host if request.client else "unknown"
        limit_attempt("signup:ip:" + address, 5)
        limit_attempt("signup:global", 30)
        user_id = "user_" + uuid4().hex
        try:
            with sessions.begin() as session:
                session.add(UserAccount(id=user_id, username=body.username, password_hash=hash_password(body.password)))
        except IntegrityError:
            raise HTTPException(409, detail={"code": "username_taken", "message": "이미 쓰고 있는 아이디예요."}) from None
        return issue(user_id, body.username, response)

    @router.post("/login", response_model=SessionIssued, summary="로그인")
    def login(body: Credentials, request: Request, response: Response):
        """계정과 접속 주소를 함께 제한하고 유효한 세션을 발급한다."""
        limit_attempt("login:user:" + body.username, LOGIN_LIMIT)
        limit_attempt("login:ip:" + (request.client.host if request.client else "unknown"), 30)
        with sessions() as session:
            account = session.scalar(select(UserAccount).where(UserAccount.username == body.username))
            found = (account.id, account.username, account.password_hash) if account else None
        ok = verify_password(body.password, found[2] if found else DUMMY_HASH)
        if not found or not ok:
            raise HTTPException(401, detail={"code": "invalid_credentials", "message": "아이디나 비밀번호가 맞지 않아요."})
        if len(found[2].split("$")) == 3:
            with sessions.begin() as session:
                session.execute(update(UserAccount).where(UserAccount.id == found[0],
                    UserAccount.password_hash == found[2]).values(password_hash=hash_password(body.password)))
        return issue(found[0], found[1], response)

    @router.get("/me", response_model=Me, summary="로그인한 내 정보")
    def me(user_id: str = Depends(web_auth)):
        """현재 인증의 계정 정보만 반환한다."""
        if user_id == LOCAL_PROFILE_ID:
            return Me(user_id=user_id, username="local", via="local")
        with sessions() as session:
            account = session.get(UserAccount, user_id)
            if account is None:
                raise HTTPException(401, detail={"code": "invalid_session", "message": "다시 로그인해 주세요."})
            return Me(user_id=account.id, username=account.username, via="account")

    @router.post("/logout", status_code=204, summary="로그아웃 (지금 세션만 끊음)")
    def logout(credentials: HTTPAuthorizationCredentials | None = Depends(session_bearer)):
        """현재 세션 하나만 폐기하며 여러 기기의 다른 세션은 보존한다."""
        if credentials is not None:
            with sessions.begin() as session:
                session.execute(update(UserSession).where(UserSession.token_hash == token_hash(credentials.credentials),
                                                          UserSession.revoked_at.is_(None)).values(revoked_at=utc_now()))

    return router
