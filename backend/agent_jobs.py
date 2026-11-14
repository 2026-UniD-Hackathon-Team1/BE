"""서버가 승인한 작업을 기기별로 인수하고 진행 및 결과를 영속 저장한다."""
import json
import secrets
from datetime import datetime, timedelta
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Response, Security
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

import backend.agent as agent
from backend.models import AgentDevice, AgentJob
from backend.environment_data import ScanFailure, store_environment
from pydantic import ValidationError
from backend.schemas import ErrorResponse

JobKind = Literal["scan", "suggest_inputs", "snapshot_inputs", "run_comparison", "apply", "rollback"]
LEASE_SECONDS = 120
MAX_RUNTIME_SECONDS = 1800
MAX_ATTEMPTS = 3
RETRYABLE_KINDS = {"scan", "suggest_inputs", "snapshot_inputs"}


def canonical_json(value):
    """동일한 요청 및 결과를 비교할 수 있도록 JSON 표현을 고정한다."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class JobSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kind: JobKind
    request_key: str = Field(min_length=1, max_length=128)
    payload: dict = Field(default_factory=dict)

    @field_validator("payload")
    @classmethod
    def bounded_payload(cls, value):
        """서버가 생성하는 작업 메타데이터의 크기를 제한한다."""
        if len(canonical_json(value).encode("utf-8")) > 128 * 1024:
            raise ValueError("job payload exceeds limit")
        return value


class JobView(BaseModel):
    job_id: str
    kind: JobKind
    status: Literal["queued", "running", "done", "failed", "cancelled"]
    attempts: int
    progress_step: str | None
    progress_detail: str | None
    result: dict | None
    message: str | None
    created_at: str
    updated_at: str
    started_at: str | None
    finished_at: str | None


class JobClaim(BaseModel):
    job_id: str
    kind: JobKind
    payload: dict
    attempt: int
    claim_token: str
    lease_expires_at: str


class ProgressInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    claim_token: str = Field(min_length=32, max_length=128)
    step: str = Field(min_length=1, max_length=80)
    detail: str | None = Field(default=None, max_length=1000)


class ResultInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    claim_token: str = Field(min_length=32, max_length=128)
    status: Literal["done", "failed"]
    result: dict | None = None
    message: str | None = Field(default=None, max_length=1000)

    @field_validator("result")
    @classmethod
    def bounded_result(cls, value):
        """결과 보고의 JSON 크기를 제한하고 유효하지 않은 숫자를 거부한다."""
        if len(canonical_json(value).encode("utf-8")) > 256 * 1024:
            raise ValueError("job result exceeds limit")
        return value


def job_view(job):
    """웹 조회에는 실행 소유권 토큰과 작업 내부 payload를 노출하지 않는다."""
    return JobView(job_id=job.id, kind=job.kind, status=job.status, attempts=job.attempts,
        progress_step=job.progress_step, progress_detail=job.progress_detail,
        result=job.result_json, message=job.message, created_at=job.created_at, updated_at=job.updated_at,
        started_at=job.started_at, finished_at=job.finished_at)


def enqueue_job(sessions, profile_id: str, spec: JobSpec, *, reuse_pending=False):
    """서버 내부 생산자가 같은 요청 키로 작업을 중복 생성하지 않도록 한다."""
    digest = agent.secret_hash(canonical_json({"kind": spec.kind, "payload": spec.payload}))
    with sessions.begin() as session:
        device = session.scalar(select(AgentDevice).where(AgentDevice.profile_id == profile_id,
            AgentDevice.revoked_at.is_(None), AgentDevice.is_primary.is_(True)).with_for_update())
        existing = session.scalar(select(AgentJob).where(AgentJob.profile_id == profile_id,
            AgentJob.request_key == spec.request_key))
        if existing is not None:
            if existing.request_hash != digest:
                raise HTTPException(409, detail={"code": "JOB_REQUEST_CONFLICT", "message": "같은 요청 키에 다른 작업이 등록돼 있습니다."})
            return job_view(existing)
        now = agent.current_time()
        if device is None or device.last_seen_at < agent.iso_time(now - timedelta(seconds=60)):
            raise HTTPException(409, detail={"code": "AGENT_OFFLINE", "message": "연결 프로그램이 꺼져 있어요."})
        if reuse_pending:
            pending = session.scalar(select(AgentJob).where(AgentJob.device_id == device.id,
                AgentJob.request_hash == digest, AgentJob.status.in_(["queued", "running"]))
                .order_by(AgentJob.created_at).limit(1))
            if pending is not None:
                return job_view(pending)
        if spec.kind == "run_comparison" and not device.codex_ready:
            raise HTTPException(409, detail={"code": "CODEX_NOT_READY", "message": "연결 프로그램의 Codex 로그인 상태를 확인해 주세요."})
        job = AgentJob(id="job_" + uuid4().hex, profile_id=profile_id, device_id=device.id,
            kind=spec.kind, request_key=spec.request_key, request_hash=digest, payload=spec.payload,
            status="queued", attempts=0, created_at=agent.iso_time(now), updated_at=agent.iso_time(now))
        session.add(job)
        session.flush()
        return job_view(job)


def expire_jobs(session, device_id, now):
    """기한이 지난 읽기 작업만 재시도하고 비용 발생 및 쓰기 작업은 실패로 닫는다."""
    for job in session.scalars(select(AgentJob).where(AgentJob.device_id == device_id,
        AgentJob.status == "running", AgentJob.lease_expires_at <= agent.iso_time(now)).with_for_update()):
        retry = job.kind in RETRYABLE_KINDS and job.attempts < MAX_ATTEMPTS
        job.status = "queued" if retry else "failed"
        job.claim_token_hash = None
        job.lease_expires_at = None
        job.progress_step = None
        job.progress_detail = None
        job.updated_at = agent.iso_time(now)
        job.started_at = None if retry else job.started_at
        job.finished_at = None if retry else agent.iso_time(now)
        job.message = None if retry else "작업 보고 기한이 지나 종료됐습니다. 상태를 확인한 뒤 다시 요청해 주세요."
    session.flush()


def owned_job(session, device, job_id, claim_token):
    """같은 기기의 현재 인수 토큰으로만 해당 작업에 보고하도록 제한한다."""
    job = session.scalar(select(AgentJob).where(AgentJob.id == job_id,
        AgentJob.device_id == device.id, AgentJob.profile_id == device.profile_id).with_for_update())
    if job is None:
        raise HTTPException(404, detail={"code": "JOB_NOT_FOUND", "message": "해당 작업을 찾을 수 없습니다."})
    if not job.claim_token_hash or not secrets.compare_digest(job.claim_token_hash, agent.secret_hash(claim_token)):
        raise HTTPException(409, detail={"code": "STALE_JOB_CLAIM", "message": "이 작업의 실행 권한이 만료됐습니다."})
    return job


def require_live_lease(job, now):
    """기한 만료나 이미 끝난 실행의 진행 및 결과 변경을 거부한다."""
    if job.status != "running" or job.lease_expires_at is None or job.lease_expires_at <= agent.iso_time(now):
        raise HTTPException(409, detail={"code": "JOB_LEASE_EXPIRED", "message": "작업 보고 기한이 지났습니다. 현재 작업 상태를 확인해 주세요."})


def read_job(sessions, profile_id, job_id):
    """짧은 트랜잭션으로 소유 작업과 만료 상태를 조회한다."""
    with sessions.begin() as session:
        device_id = session.scalar(select(AgentJob.device_id).where(AgentJob.id == job_id, AgentJob.profile_id == profile_id))
        if device_id is None:
            raise HTTPException(404, detail={"code": "JOB_NOT_FOUND", "message": "해당 작업을 찾을 수 없습니다."})
        session.scalar(select(AgentDevice).where(AgentDevice.id == device_id).with_for_update())
        expire_jobs(session, device_id, agent.current_time())
        return job_view(session.get(AgentJob, job_id))


def job_router(sessions, web_auth):
    """기기 작업 인수와 보고 API 및 웹의 소유 작업 상태 조회를 구성한다."""
    router = APIRouter(prefix="/api/agent/jobs", tags=["agent-jobs"],
        responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}})

    @router.get("", response_model=JobClaim | None, summary="실행할 작업 하나 인수")
    def claim(response: Response, credentials: HTTPAuthorizationCredentials | None = Security(agent.bearer)):
        """기기별 실행 한 개와 원자적 잠금으로 동시 폴링의 중복 인수를 막는다."""
        response.headers["Cache-Control"] = "no-store"
        with sessions.begin() as session:
            device = agent.authenticated_device(session, credentials)
            now = agent.current_time()
            expire_jobs(session, device.id, now)
            running = session.scalar(select(AgentJob.id).where(AgentJob.profile_id == device.profile_id,
                AgentJob.status == "running"))
            if running is not None or not device.is_primary:
                return None
            job = session.scalar(select(AgentJob).where(AgentJob.device_id == device.id, AgentJob.status == "queued")
                .order_by(AgentJob.created_at, AgentJob.id).with_for_update(skip_locked=True).limit(1))
            if job is None:
                return None
            token = secrets.token_urlsafe(32)
            job.status = "running"
            job.attempts += 1
            job.claim_token_hash = agent.secret_hash(token)
            job.started_at = agent.iso_time(now)
            job.updated_at = agent.iso_time(now)
            job.lease_expires_at = agent.iso_time(now + timedelta(seconds=LEASE_SECONDS))
            return JobClaim(job_id=job.id, kind=job.kind, payload=job.payload, attempt=job.attempts,
                claim_token=token, lease_expires_at=job.lease_expires_at)

    @router.get("/{job_id}", response_model=JobView, summary="내 작업 상태 조회")
    def status(job_id: str, response: Response, profile_id: str = Depends(web_auth)):
        """서버 대기 시간 초과 뒤에도 같은 작업을 조회하고 만료 상태를 갱신한다."""
        response.headers["Cache-Control"] = "no-store"
        return read_job(sessions, profile_id, job_id)

    @router.post("/{job_id}/progress", status_code=204, summary="작업 진행 보고와 기한 갱신")
    def progress(job_id: str, body: ProgressInput,
                 credentials: HTTPAuthorizationCredentials | None = Security(agent.bearer)):
        """실행 상한 안에서만 lease를 연장하고 웹에 표시할 진행 단계를 저장한다."""
        with sessions.begin() as session:
            device = agent.authenticated_device(session, credentials)
            job = owned_job(session, device, job_id, body.claim_token)
            now = agent.current_time()
            require_live_lease(job, now)
            upper = datetime.fromisoformat(job.started_at) + timedelta(seconds=MAX_RUNTIME_SECONDS)
            job.lease_expires_at = agent.iso_time(min(now + timedelta(seconds=LEASE_SECONDS), upper))
            job.updated_at = agent.iso_time(now)
            job.progress_step, job.progress_detail = body.step, body.detail
        return Response(status_code=204)

    @router.post("/{job_id}/result", status_code=204, summary="작업 완료 또는 실패 결과 보고")
    def result(job_id: str, body: ResultInput,
               credentials: HTTPAuthorizationCredentials | None = Security(agent.bearer)):
        """같은 결과 재전송은 허용하고 다른 결과나 이전 실행의 보고는 거부한다."""
        digest = agent.secret_hash(canonical_json(body.model_dump(exclude={"claim_token"})))
        with sessions.begin() as session:
            device = agent.authenticated_device(session, credentials)
            job = owned_job(session, device, job_id, body.claim_token)
            if job.status in {"done", "failed"} and job.result_hash == digest:
                return Response(status_code=204)
            now = agent.current_time()
            require_live_lease(job, now)
            if job.kind == "scan":
                if body.status == "done":
                    store_environment(session, device, job, body.result, agent.iso_time(now))
                elif body.result is not None:
                    try:
                        ScanFailure.model_validate(body.result)
                    except ValidationError:
                        raise HTTPException(422, detail={"code": "INVALID_SCAN_RESULT", "message": "환경 스캔 실패 결과 형식을 확인해 주세요."}) from None
            job.status, job.result_json, job.message = body.status, body.result, body.message
            job.result_hash = digest
            job.lease_expires_at = None
            job.finished_at = agent.iso_time(now)
            job.updated_at = agent.iso_time(now)
        return Response(status_code=204)

    return router
