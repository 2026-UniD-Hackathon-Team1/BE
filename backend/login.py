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

from fastapi import APIRouter, Depends, HTTPException
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
SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}


def iso(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, **SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt), **SCRYPT)
    except (ValueError, TypeError):
        return False
    return secrets.compare_digest(actual, expected)


#: 없는 아이디로 로그인해도 같은 시간이 걸리게 비교할 가짜 해시 (아이디 존재 여부를 숨긴다)
DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    username: str = Field(min_length=3, max_length=30, pattern=r"^[a-z0-9_-]+$", examples=["junior_dev"])
    password: str = Field(min_length=8, max_length=128)


class SessionIssued(BaseModel):
    token: str
    user_id: str
    username: str


class Me(BaseModel):
    user_id: str
    username: str
    via: str  # account | local


def login_router(sessions, web_auth):
    """회원가입·로그인·내 정보·로그아웃."""
    router = APIRouter(prefix="/api/auth", tags=["auth"],
                       responses={401: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 429: {"model": ErrorResponse}})

    def issue(user_id: str, username: str) -> SessionIssued:
        token = secrets.token_urlsafe(32)
        with sessions.begin() as session:
            session.add(UserSession(token_hash=token_hash(token), user_id=user_id, expires_at=iso(datetime.now(timezone.utc) + SESSION_TTL)))
        return SessionIssued(token=token, user_id=user_id, username=username)

    def limit_login(username: str):
        """아이디마다 로그인 시도를 제한해 비밀번호 대입을 막는다."""
        now = int(datetime.now(timezone.utc).timestamp())
        key = "login:" + token_hash(username)
        with sessions.begin() as session:
            session.execute(delete(AgentRateLimit).where(AgentRateLimit.reset_at < now - 3600))
            expired = AgentRateLimit.reset_at <= now
            stmt = pg_insert(AgentRateLimit).values(key=key, attempts=1, reset_at=now + LOGIN_WINDOW)
            stmt = stmt.on_conflict_do_update(index_elements=[AgentRateLimit.key], set_={
                "attempts": case((expired, 1), else_=AgentRateLimit.attempts + 1),
                "reset_at": case((expired, now + LOGIN_WINDOW), else_=AgentRateLimit.reset_at)})
            attempts = session.scalar(stmt.returning(AgentRateLimit.attempts))
        if attempts > LOGIN_LIMIT:
            raise HTTPException(429, detail={"code": "login_rate_limited", "message": "로그인 시도가 많아요. 10분 뒤 다시 시도해 주세요."},
                                headers={"Retry-After": str(LOGIN_WINDOW)})

    @router.post("/signup", response_model=SessionIssued, status_code=201, summary="회원가입 (가입 후 바로 로그인)")
    def signup(body: Credentials):
        user_id = "user_" + uuid4().hex
        try:
            with sessions.begin() as session:
                session.add(UserAccount(id=user_id, username=body.username, password_hash=hash_password(body.password)))
        except IntegrityError:
            raise HTTPException(409, detail={"code": "username_taken", "message": "이미 쓰고 있는 아이디예요."}) from None
        return issue(user_id, body.username)

    @router.post("/login", response_model=SessionIssued, summary="로그인")
    def login(body: Credentials):
        limit_login(body.username)
        with sessions() as session:
            account = session.scalar(select(UserAccount).where(UserAccount.username == body.username))
            found = (account.id, account.username, account.password_hash) if account else None
        ok = verify_password(body.password, found[2] if found else DUMMY_HASH)
        if not found or not ok:
            raise HTTPException(401, detail={"code": "invalid_credentials", "message": "아이디나 비밀번호가 맞지 않아요."})
        return issue(found[0], found[1])

    @router.get("/me", response_model=Me, summary="로그인한 내 정보")
    def me(user_id: str = Depends(web_auth)):
        if user_id == LOCAL_PROFILE_ID:
            return Me(user_id=user_id, username="local", via="local")
        with sessions() as session:
            account = session.get(UserAccount, user_id)
            if account is None:
                raise HTTPException(401, detail={"code": "invalid_session", "message": "다시 로그인해 주세요."})
            return Me(user_id=account.id, username=account.username, via="account")

    @router.post("/logout", status_code=204, summary="로그아웃 (지금 세션만 끊음)")
    def logout(credentials: HTTPAuthorizationCredentials | None = Depends(session_bearer)):
        if credentials is not None:
            with sessions.begin() as session:
                session.execute(update(UserSession).where(UserSession.token_hash == token_hash(credentials.credentials),
                                                          UserSession.revoked_at.is_(None)).values(revoked_at=utc_now()))

    return router
