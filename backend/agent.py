"""단일 사용자 웹 인증과 기기별 연결 인증을 구분하는 연결 API."""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import case, delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from backend.models import AgentDevice, AgentJob, AgentPairingCode, AgentRateLimit, Profile
from backend.schemas import ErrorResponse

bearer = HTTPBearer(auto_error=False, scheme_name="AgentToken")


def current_time():
    """연결 만료와 상태 계산에 사용할 UTC 시각을 반환한다."""
    return datetime.now(timezone.utc)


def iso_time(value):
    """UTC 시각을 정렬 가능한 공통 저장 형식으로 변환한다."""
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def secret_hash(value):
    """고엔트로피 연결 비밀값의 해시만 저장하거나 조회한다."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class PairInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    code: str = Field(pattern=r"^[A-Za-z2-9]{4}-?[A-Za-z2-9]{4}$")
    device_name: str = Field(min_length=1, max_length=100)
    agent_version: str = Field(min_length=1, max_length=40)

    @field_validator("code")
    @classmethod
    def normalize_code(cls, value):
        """표시용 구분자와 대소문자를 정리해 같은 코드로 조회한다."""
        return value.replace("-", "").upper()


class HeartbeatInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    codex_version: str | None = Field(default=None, min_length=1, max_length=80)
    codex_ready: bool = False


class PairingCodeResponse(BaseModel):
    code: str
    expires_at: str
    command: str | None = None
    install_command: str | None = None


class PairResponse(BaseModel):
    agent_token: str


class AgentStatus(BaseModel):
    status: Literal["connected", "disconnected", "never_connected"]
    device_name: str | None = None
    agent_version: str | None = None
    codex_version: str | None = None
    codex_ready: bool = False
    last_seen_at: str | None = None


class AgentDeviceView(AgentStatus):
    device_id: str
    is_primary: bool


class DeviceList(BaseModel):
    devices: list[AgentDeviceView]


def authenticated_device(session, credentials, *, lock_profile=False):
    """기기 행을 잠그고 해제된 토큰의 작업 접근과 상태 갱신을 거부한다."""
    if credentials is not None:
        if lock_profile:
            profile_id = session.scalar(select(AgentDevice.profile_id).where(
                AgentDevice.token_hash == secret_hash(credentials.credentials), AgentDevice.revoked_at.is_(None)))
            if profile_id is not None:
                session.scalar(select(Profile).where(Profile.id == profile_id).with_for_update())
        device = session.scalar(select(AgentDevice).where(
            AgentDevice.token_hash == secret_hash(credentials.credentials), AgentDevice.revoked_at.is_(None))
            .with_for_update())
        if device is not None:
            return device
    raise HTTPException(401, detail={"code": "INVALID_AGENT_TOKEN", "message": "연결 프로그램을 다시 연결해 주세요."})


def revoke_devices(session, profile_id, now):
    """기기 토큰과 미완료 작업을 같은 트랜잭션에서 무효화한다."""
    ids = list(session.scalars(select(AgentDevice.id).where(AgentDevice.profile_id == profile_id,
        AgentDevice.revoked_at.is_(None)).with_for_update()))
    session.execute(update(AgentJob).where(AgentJob.device_id.in_(ids),
        AgentJob.status.in_(["queued", "running"])).values(status="cancelled", lease_expires_at=None,
        updated_at=now, finished_at=now, message="연결이 해제되어 작업이 취소됐습니다."))
    session.execute(update(AgentDevice).where(AgentDevice.id.in_(ids)).values(revoked_at=now, codex_ready=False))


def agent_router(sessions, web_auth):
    """웹 연결 관리와 agent Bearer 인증 경로를 구성한다."""
    router = APIRouter(prefix="/api/agent", tags=["agent"],
        responses={401: {"model": ErrorResponse}, 429: {"model": ErrorResponse}})

    def limit_attempts(scope, identity, maximum):
        """DB의 원자적 카운터로 서버 재시작과 다중 워커에도 요청 제한을 적용한다."""
        now = int(current_time().timestamp())
        key = scope + ":" + secret_hash(identity)
        with sessions.begin() as session:
            session.execute(delete(AgentRateLimit).where(AgentRateLimit.reset_at < now - 3600))
            expired = AgentRateLimit.reset_at <= now
            stmt = pg_insert(AgentRateLimit).values(key=key, attempts=1, reset_at=now + 60)
            stmt = stmt.on_conflict_do_update(index_elements=[AgentRateLimit.key], set_={
                "attempts": case((expired, 1), else_=AgentRateLimit.attempts + 1),
                "reset_at": case((expired, now + 60), else_=AgentRateLimit.reset_at)})
            attempts = session.scalar(stmt.returning(AgentRateLimit.attempts))
        if attempts > maximum:
            raise HTTPException(429, detail={"code": "AGENT_RATE_LIMITED", "message": "잠시 후 다시 연결해 주세요."},
                                headers={"Retry-After": "60"})

    @router.post("/pairing-code", response_model=PairingCodeResponse, summary="기기 연결 코드 발급")
    def issue_code(response: Response, profile_id: str = Depends(web_auth)):
        """기존 미사용 코드를 무효화하고 10분 유효한 일회용 코드를 발급한다."""
        limit_attempts("issue", profile_id, 5)
        now = current_time()
        code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))
        expires_at = iso_time(now + timedelta(minutes=10))
        with sessions.begin() as session:
            if session.scalar(select(Profile).where(Profile.id == profile_id).with_for_update()) is None:
                raise HTTPException(404, detail={"code": "profile_not_found", "message": "먼저 내 작업 환경을 등록해 주세요."})
            session.execute(update(AgentPairingCode).where(AgentPairingCode.profile_id == profile_id,
                AgentPairingCode.consumed_at.is_(None)).values(consumed_at=iso_time(now)))
            session.add(AgentPairingCode(code_hash=secret_hash(code), profile_id=profile_id, expires_at=expires_at))
        response.headers["Cache-Control"] = "no-store"
        return PairingCodeResponse(code=code[:4] + "-" + code[4:], expires_at=expires_at)

    @router.post("/pair", response_model=PairResponse, summary="연결 코드를 기기 토큰으로 교환")
    def pair(body: PairInput, request: Request, response: Response):
        """사용자 소유 코드를 한 번만 소비하고 이전 기기 토큰을 교체한다."""
        limit_attempts("pair", request.client.host if request.client else "unknown", 10)
        now = iso_time(current_time())
        token = secrets.token_urlsafe(32)
        with sessions.begin() as session:
            profile_id = session.scalar(select(AgentPairingCode.profile_id).where(
                AgentPairingCode.code_hash == secret_hash(body.code)))
            if profile_id is not None:
                session.scalar(select(Profile).where(Profile.id == profile_id).with_for_update())
            consumed = session.execute(update(AgentPairingCode).where(
                AgentPairingCode.code_hash == secret_hash(body.code),
                AgentPairingCode.consumed_at.is_(None), AgentPairingCode.expires_at > now).values(consumed_at=now))
            if consumed.rowcount != 1:
                raise HTTPException(400, detail={"code": "INVALID_PAIRING_CODE", "message": "연결 코드가 잘못됐거나 만료됐습니다. 새 코드를 발급해 주세요."})
            session.execute(update(AgentDevice).where(AgentDevice.profile_id == profile_id,
                AgentDevice.revoked_at.is_(None)).values(is_primary=False))
            session.add(AgentDevice(id="agent_" + uuid4().hex, profile_id=profile_id,
                token_hash=secret_hash(token), device_name=body.device_name, agent_version=body.agent_version,
                is_primary=True, codex_ready=False, last_seen_at=now))
        response.headers["Cache-Control"] = "no-store"
        return PairResponse(agent_token=token)

    @router.get("", response_model=AgentStatus, summary="현재 기기 연결 상태")
    def status(response: Response, profile_id: str = Depends(web_auth)):
        """최근 60초 heartbeat와 연결 해제를 기준으로 기기 상태를 반환한다."""
        response.headers["Cache-Control"] = "no-store"
        with sessions() as session:
            device = session.scalar(select(AgentDevice).where(AgentDevice.profile_id == profile_id)
                .order_by(AgentDevice.revoked_at.is_(None).desc(), AgentDevice.is_primary.desc(),
                          AgentDevice.last_seen_at.desc(), AgentDevice.id.desc()))
            if device is None:
                return AgentStatus(status="never_connected")
            connected = device.revoked_at is None and device.last_seen_at >= iso_time(current_time() - timedelta(seconds=60))
            return AgentStatus(status="connected" if connected else "disconnected",
                device_name=device.device_name, agent_version=device.agent_version,
                codex_version=device.codex_version, codex_ready=device.codex_ready if connected else False,
                last_seen_at=device.last_seen_at)

    @router.get("/devices", response_model=DeviceList, summary="연결된 기기 목록")
    def devices(response: Response, profile_id: str = Depends(web_auth)):
        response.headers["Cache-Control"] = "no-store"
        with sessions() as session:
            now = iso_time(current_time() - timedelta(seconds=60))
            return DeviceList(devices=[AgentDeviceView(device_id=item.id, device_name=item.device_name,
                status="connected" if item.revoked_at is None and item.last_seen_at >= now else "disconnected",
                agent_version=item.agent_version, codex_version=item.codex_version,
                codex_ready=item.codex_ready and item.revoked_at is None and item.last_seen_at >= now,
                last_seen_at=item.last_seen_at, is_primary=item.is_primary)
                for item in session.scalars(select(AgentDevice).where(AgentDevice.profile_id == profile_id)
                    .order_by(AgentDevice.is_primary.desc(), AgentDevice.last_seen_at.desc()))])

    @router.put("/devices/{device_id}/primary", status_code=204, summary="작업 기본 기기 지정")
    def set_primary(device_id: str, profile_id: str = Depends(web_auth)):
        with sessions.begin() as session:
            session.scalar(select(Profile).where(Profile.id == profile_id).with_for_update())
            device = session.scalar(select(AgentDevice).where(AgentDevice.id == device_id,
                AgentDevice.profile_id == profile_id, AgentDevice.revoked_at.is_(None)).with_for_update())
            if device is None:
                raise HTTPException(404, detail={"code": "DEVICE_NOT_FOUND", "message": "연결된 기기를 찾을 수 없습니다."})
            session.execute(update(AgentDevice).where(AgentDevice.profile_id == profile_id,
                AgentDevice.revoked_at.is_(None)).values(is_primary=False))
            device.is_primary = True
        return Response(status_code=204)

    @router.post("/heartbeat", status_code=204, summary="기기 생존 및 Codex 준비 상태 갱신")
    def heartbeat(body: HeartbeatInput,
                  credentials: HTTPAuthorizationCredentials | None = Security(bearer)):
        """유효한 기기 토큰으로만 heartbeat를 갱신하며 해제된 기기는 거부한다."""
        if body.codex_ready and body.codex_version is None:
            raise HTTPException(422, detail={"code": "validation_error", "message": "Codex 준비 상태와 버전을 함께 보내 주세요."})
        with sessions.begin() as session:
            device = authenticated_device(session, credentials)
            device.last_seen_at = iso_time(current_time())
            device.codex_version = body.codex_version
            device.codex_ready = body.codex_ready
        return Response(status_code=204)

    @router.delete("", status_code=204, summary="기기 연결 해제")
    def disconnect(profile_id: str = Depends(web_auth)):
        """기기 토큰과 미사용 연결 코드를 함께 무효화한다."""
        now = iso_time(current_time())
        with sessions.begin() as session:
            session.scalar(select(Profile).where(Profile.id == profile_id).with_for_update())
            revoke_devices(session, profile_id, now)
            session.execute(update(AgentPairingCode).where(AgentPairingCode.profile_id == profile_id,
                AgentPairingCode.consumed_at.is_(None)).values(consumed_at=now))
        return Response(status_code=204)

    return router
