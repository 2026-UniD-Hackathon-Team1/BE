"""명시적으로 요청한 서버 수집의 중복 실행과 영속 상태를 관리한다."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, text, update

from backend.catalog.config import CollectionConfig
from backend.catalog.github_client import GithubClient
from backend.catalog.service import CatalogCollector
from backend.catalog.schemas import CollectionResult
from backend.models import CatalogSyncJob, utc_now

# 제출과 실제 실행에 각각 잠금을 사용해 여러 서버 프로세스도 한 번만 수집한다.
SUBMIT_LOCK = 724619001
WORKER_LOCK = 724619002


def expires_later():
    """HTTP 제한 시간보다 여유 있게 5분 뒤 작업 만료 시각을 만든다."""
    return (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(timespec="microseconds").replace("+00:00", "Z")


class SyncRequest(CollectionConfig):
    max_requests: int = Field(default=20, ge=1, le=50)

    @model_validator(mode="after")
    def require_target(self):
        """대상이 없는 수집 요청은 실행하지 않는다."""
        if not self.repositories and not self.discovery_queries and not self.resume_pending:
            raise ValueError("collection target required")
        return self


class SyncJobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    status: Literal["queued", "running", "done", "partial", "failed", "interrupted"]
    request_count: int
    max_requests: int
    result: CollectionResult
    error: str | None
    created_at: str
    updated_at: str
    expires_at: str
    context_snapshot: dict = Field(default_factory=dict)


def serialize_job(job):
    """요청 키나 내부 인증 값 없이 프론트용 진행 정보를 반환한다."""
    return SyncJobResponse(id=job.id, status=job.status, request_count=job.request_count,
        max_requests=job.config_json["max_requests"], result=job.result_json, error=job.error,
        created_at=job.created_at, updated_at=job.updated_at, expires_at=job.expires_at,
        context_snapshot=job.context_snapshot)


def expire_jobs(session):
    """프로세스 종료 등으로 만료된 작업을 중단 처리하고 활성 슬롯을 반환한다."""
    session.execute(update(CatalogSyncJob).where(CatalogSyncJob.active_slot == 1,
        CatalogSyncJob.expires_at <= utc_now()).values(status="interrupted", active_slot=None,
        error="worker_interrupted", updated_at=utc_now()))


def enqueue_job(session, request, profile_id, request_key):
    """웹과 예약 워커에서 같은 멱등성과 활성 슬롯 규칙으로 작업을 만든다."""
    payload = request.model_dump(mode="json")
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": SUBMIT_LOCK})
    expire_jobs(session)
    existing = session.scalar(select(CatalogSyncJob).where(
        CatalogSyncJob.profile_id == profile_id, CatalogSyncJob.request_key == request_key))
    if existing:
        if existing.request_hash != digest and SyncRequest.model_validate(
                existing.config_json).model_dump(mode="json") != payload:
            raise HTTPException(409, detail={"code": "idempotency_conflict", "message": "같은 요청 키의 내용이 다릅니다."})
        return existing, False
    if session.scalar(select(CatalogSyncJob.id).where(CatalogSyncJob.active_slot == 1)):
        raise HTTPException(409, detail={"code": "catalog_sync_busy", "message": "진행 중인 수집이 있습니다."})
    job = CatalogSyncJob(id="sync_" + uuid4().hex, profile_id=profile_id,
        request_key=request_key, request_hash=digest, config_json=payload,
        active_slot=1, expires_at=expires_later())
    session.add(job)
    session.flush()
    return job, True


def run_job(sessions, job_id, client_factory=GithubClient):
    """한 서버 작업만 실행하며 저장소별 진행과 최종 결과를 DB에 남긴다."""
    client = None
    # 세션 잠금은 작업 내내 보유하고 연결 종료 전에 명시적으로 해제한다.
    with sessions() as guard:
        locked = guard.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": WORKER_LOCK})
        if not locked:
            with sessions.begin() as session:
                session.execute(update(CatalogSyncJob).where(CatalogSyncJob.id == job_id,
                    CatalogSyncJob.status == "queued").values(status="failed", active_slot=None,
                    error="collector_busy", updated_at=utc_now()))
            return
        try:
            with sessions.begin() as session:
                job = session.get(CatalogSyncJob, job_id, with_for_update=True)
                if job is None or job.status != "queued" or job.expires_at <= utc_now():
                    expire_jobs(session)
                    return
                job.status = "running"
                job.updated_at = utc_now()
                job.expires_at = expires_later()
                config = SyncRequest.model_validate(job.config_json)
            client = client_factory(max_requests=config.max_requests)

            def progress(result):
                """완료한 저장소와 실제 요청 수를 별도 트랜잭션에 보존한다."""
                with sessions.begin() as session:
                    session.execute(update(CatalogSyncJob).where(CatalogSyncJob.id == job_id,
                        CatalogSyncJob.status == "running").values(result_json=result,
                        request_count=client.request_count, updated_at=utc_now()))

            result = CatalogCollector(sessions, client).run(
                CollectionConfig.model_validate(config.model_dump(exclude={"max_requests"})), progress=progress)
            success = any(item["status"] in {"done", "partial"} for item in result["items"])
            failed = bool(result["discovery_errors"]) or any(item["status"] in {"failed", "partial"} for item in result["items"])
            status = ("partial" if success else "failed") if failed else "done"
            with sessions.begin() as session:
                session.execute(update(CatalogSyncJob).where(CatalogSyncJob.id == job_id,
                    CatalogSyncJob.status == "running").values(status=status, active_slot=None,
                    result_json=result, request_count=client.request_count, updated_at=utc_now()))
        except Exception:
            # 응답 원문과 DB 접속 정보는 실패 이력에 넣지 않는다.
            with sessions.begin() as session:
                session.execute(update(CatalogSyncJob).where(CatalogSyncJob.id == job_id,
                    CatalogSyncJob.status.in_(["queued", "running"])).values(status="failed", active_slot=None,
                    error="collection_failed", request_count=client.request_count if client else 0,
                    updated_at=utc_now()))
        finally:
            try:
                if client:
                    client.close()
            finally:
                guard.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": WORKER_LOCK})


def catalog_router(sessions, auth, *, client_factory=GithubClient):
    """인증한 사용자의 명시적 수집 요청과 작업 조회 API를 구성한다."""
    router = APIRouter(prefix="/api/catalog", tags=["catalog"])

    @router.post("/sync", status_code=202, response_model=SyncJobResponse,
                 summary="공개 GitHub 수집 요청", description="서버에서 무인증 공개 GET만 수행합니다. 최대 120초이며 중단된 작업은 자동 재시작하지 않습니다.")
    def sync(request: SyncRequest, background: BackgroundTasks, profile_id=Depends(auth),
             request_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128,
                                       pattern=r"^[A-Za-z0-9_.:-]+$")):
        """요청 키 중복을 재사용하고 다른 활성 수집은 충돌로 반환한다."""
        with sessions.begin() as session:
            job, created = enqueue_job(session, request, profile_id, request_key)
            response = serialize_job(job)
        if created:
            background.add_task(run_job, sessions, job.id, client_factory)
        return response

    @router.get("/jobs/{job_id}", response_model=SyncJobResponse, summary="서버 수집 작업 조회")
    def get_job(job_id: str, profile_id=Depends(auth)):
        """자신의 작업만 조회하며 만료된 작업은 중단 상태로 제공한다."""
        with sessions.begin() as session:
            expire_jobs(session)
            job = session.scalar(select(CatalogSyncJob).where(CatalogSyncJob.id == job_id,
                                                             CatalogSyncJob.profile_id == profile_id))
            if job is None:
                raise HTTPException(404, detail={"code": "catalog_job_not_found", "message": "수집 작업을 찾을 수 없습니다."})
            return serialize_job(job)

    return router
