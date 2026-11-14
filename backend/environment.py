"""웹의 환경 스캔 요청을 PC 작업 큐에 연결하고 결과를 조회한다."""
import asyncio
import time
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import select

from backend.agent_jobs import JobSpec, enqueue_job, read_job
from backend.environment_data import EnvironmentView, ScanInput, environment_view
from backend.models import AgentDevice, Environment
from backend.schemas import ErrorResponse


class PendingScan(BaseModel):
    job_id: str
    status: str


def scan_outcome(sessions, profile_id, job_id):
    """작업 상태를 조회하되 대기 중에는 DB 연결과 잠금을 유지하지 않는다."""
    job = read_job(sessions, profile_id, job_id)
    if job.kind != "scan":
        raise HTTPException(409, detail={"code": "INVALID_SCAN_JOB", "message": "환경 스캔 작업을 선택해 주세요."})
    if job.status in {"queued", "running"}:
        return PendingScan(job_id=job_id, status=job.status)
    if job.status == "cancelled":
        raise HTTPException(409, detail={"code": "AGENT_OFFLINE", "message": "연결이 해제되어 환경 스캔이 취소됐습니다."})
    if job.status == "failed":
        code = (job.result or {}).get("error_code")
        if code in {"ROOT_NOT_APPROVED", "ROOT_NOT_READABLE"}:
            raise HTTPException(403, detail={"code": code, "message": "connect에서 승인한 프로젝트 경로와 읽기 권한을 확인해 주세요."})
        raise HTTPException(502, detail={"code": "AGENT_SCAN_FAILED", "message": "환경 스캔에 실패했습니다. 연결 프로그램을 확인해 주세요."})
    with sessions() as session:
        value = session.scalar(select(Environment).where(Environment.profile_id == profile_id, Environment.scan_job_id == job_id))
        if value is None:
            raise HTTPException(503, detail={"code": "environment_not_ready", "message": "환경 스캔 결과를 확인할 수 없습니다."})
        return environment_view(value)


def environment_router(sessions, auth, *, wait_timeout=30.0):
    """실제 PC 파일은 열지 않고 승인 기기에 환경 스캔을 요청한다."""
    if not 0 <= wait_timeout <= 30:
        raise ValueError("Environment wait timeout must be between 0 and 30 seconds")
    router = APIRouter(prefix="/api/environment", tags=["environment"],
        responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse},
                   409: {"model": ErrorResponse}, 504: {"model": ErrorResponse}})

    @router.post("/scan", response_model=EnvironmentView, summary="연결된 PC의 승인 프로젝트 환경 스캔")
    async def scan(body: ScanInput, response: Response, profile_id: str = Depends(auth),
                   idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", min_length=1, max_length=100)):
        """동일 진행 작업을 재사용하고 최대 30초 후 작업 ID로 재조회하게 한다."""
        response.headers["Cache-Control"] = "no-store"
        spec = JobSpec(kind="scan", request_key="scan:" + (idempotency_key or uuid4().hex),
            payload={"schema_version": 1, **body.model_dump()})
        job = await asyncio.to_thread(enqueue_job, sessions, profile_id, spec, reuse_pending=idempotency_key is None)
        deadline = time.monotonic() + wait_timeout
        while True:
            value = await asyncio.to_thread(scan_outcome, sessions, profile_id, job.job_id)
            if isinstance(value, EnvironmentView):
                return value
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HTTPException(504, detail={"code": "AGENT_TIMEOUT", "message": "환경 스캔을 기다리는 시간이 지났습니다. 작업 상태를 다시 확인해 주세요.",
                    "job_id": job.job_id}, headers={"Retry-After": "2", "Cache-Control": "no-store"})
            await asyncio.sleep(min(0.25, remaining))

    @router.get("", response_model=EnvironmentView | PendingScan, responses={202: {"model": PendingScan}},
                summary="마지막 환경 또는 스캔 작업 결과 조회")
    async def get(response: Response, job_id: str | None = None, profile_id: str = Depends(auth)):
        """작업 ID가 있으면 대기 후 재조회하고 없으면 현재 기기의 마지막 환경을 반환한다."""
        response.headers["Cache-Control"] = "no-store"
        if job_id is not None:
            value = await asyncio.to_thread(scan_outcome, sessions, profile_id, job_id)
            if isinstance(value, PendingScan):
                response.status_code = 202
            return value
        def latest():
            """현재 연결 기기의 환경만 짧은 DB 조회로 읽는다."""
            with sessions() as session:
                value = session.scalar(select(Environment).join(AgentDevice, AgentDevice.id == Environment.device_id)
                    .where(Environment.profile_id == profile_id, AgentDevice.revoked_at.is_(None))
                    .order_by(Environment.scanned_at.desc()).limit(1))
                return environment_view(value) if value is not None else None
        value = await asyncio.to_thread(latest)
        if value is None:
            raise HTTPException(404, detail={"code": "environment_not_found", "message": "아직 확인한 프로젝트 환경이 없습니다."})
        return value

    return router
