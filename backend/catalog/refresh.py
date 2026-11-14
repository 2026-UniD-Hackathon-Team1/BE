"""명시적으로 실행한 워커에서만 동작하는 예약 수집과 운영 조회."""
import argparse
import json
import time
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError

from backend.catalog.config import CollectionConfig, RepositoryConfig
from backend.catalog.discovery import load_plan, shared_folder_hints, shared_queries, shared_query_sorts
from backend.catalog.github_client import GithubClient
from backend.catalog.jobs import SUBMIT_LOCK, SyncRequest, enqueue_job, expire_jobs, run_job
from backend.catalog.policy import HOURLY_LIMIT, bind_policy, blocked_until, iso_time, lock_runtime
from backend.db import configured_engine, session_factory
from backend.models import CatalogSyncJob, Profile, Source, utc_now


class RefreshSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    contextual_discovery: bool = False
    discovery_queries: list[str] = Field(default_factory=list, max_length=5)
    discovery_limit: int = Field(default=1, ge=1, le=5)
    resume_interval_seconds: int = Field(default=300, ge=300, le=86400)
    track_interval_seconds: int = Field(default=21600, ge=3600, le=604800)
    discovery_interval_seconds: int = Field(default=86400, ge=3600, le=604800)
    max_requests: int = Field(default=10, ge=5, le=20)
    batch_size: int = Field(default=5, ge=1, le=20)

    @field_validator("discovery_queries")
    @classmethod
    def validate_queries(cls, values):
        """수동 수집과 동일한 검색어 길이 및 중복 규칙을 적용한다."""
        return CollectionConfig(discovery_queries=values).discovery_queries


class RefreshState(BaseModel):
    schedule: RefreshSchedule
    next_runs: dict[str, str]
    next_allowed_at: str | None
    blocked_reason: str | None
    hourly_limit: int
    window_requests: int
    total_requests: int
    job_count: int
    last_job_id: str | None
    worker_required: bool = True


def is_pending(source):
    """미완료 회차와 아직 시작하지 않은 발견 대상을 구분해 확인한다."""
    state = source.sync_state.get("batch")
    return bool(source.sync_state.get("queued_config") or
                (state and state["next"] < len(state["folders"]) + 1))


def contextual_queries(session, state, schedule, now):
    """최대 열 명의 전체 검색 계획을 여러 회차로 나눠 편중 없이 순환한다."""
    cursor = state.get("contextual_cursor", {})
    after = cursor.get("profile_after", "")
    batch_ids = cursor.get("batch_profile_ids", [])[:10]
    if batch_ids:
        batch_ids = list(session.scalars(select(Profile.id).where(Profile.id.in_(batch_ids)).order_by(Profile.id)))
    if not batch_ids:
        batch_ids = list(session.scalars(select(Profile.id).where(Profile.id > after).order_by(Profile.id).limit(10)))
        if not batch_ids:
            batch_ids = list(session.scalars(select(Profile.id).order_by(Profile.id).limit(10)))
    plans = [load_plan(session, profile_id, now=datetime.fromtimestamp(now, timezone.utc)) for profile_id in batch_ids]
    # 한 사용자의 최근 검색뿐 아니라 모든 사용자의 보완 검색도 다음 회차에 남긴다.
    pool = shared_queries(plans, limit=10 * max((len(plan.queries) for plan in plans), default=0))
    pool = list(dict.fromkeys(pool + schedule.discovery_queries))
    if not pool:
        return [], {}, cursor
    offset = cursor.get("query_offset", 0)
    if not isinstance(offset, int) or offset < 0 or offset >= len(pool):
        offset = 0
    queries = pool[offset:offset + 5]
    sorts = shared_query_sorts(plans, queries)
    end = offset + len(queries)
    next_cursor = {"profile_after": after, "batch_profile_ids": batch_ids, "query_offset": end}
    if end >= len(pool):
        next_cursor = {"profile_after": batch_ids[-1] if batch_ids else after,
            "batch_profile_ids": [], "query_offset": 0}
    snapshot = {plan.profile_id: {"fingerprint": plan.context_fingerprint,
        "queries": [item.query for item in plan.queries if item.query in queries],
        "query_sorts": {item.query: sorts[item.query] for item in plan.queries if item.query in queries},
        "folder_hints": plan.folder_hints,
        "covered": any(item.query in queries for item in plan.queries)} for plan in plans}
    return queries, snapshot, next_cursor


def run_refresh_tick(sessions, *, clock=time.time, client_factory=GithubClient):
    """대기와 실행 중 작업을 확인하고 기한이 된 수집 하나만 제출한다."""
    now = clock()
    with sessions.begin() as session:
        if not session.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": SUBMIT_LOCK}):
            return {"status": "busy"}
        row = lock_runtime(session)
        state = dict(row.state_json)
        schedule = RefreshSchedule.model_validate(state.get("schedule", {}))
        if not schedule.enabled:
            return {"status": "disabled"}
        until = blocked_until(state, now)
        if until:
            return {"status": "waiting", "next_allowed_at": iso_time(until)}
        expire_jobs(session)
        if session.scalar(select(CatalogSyncJob.id).where(CatalogSyncJob.active_slot == 1)):
            return {"status": "busy"}
        sources = list(session.scalars(select(Source).where(Source.enabled.is_(True),
            Source.kind == "github_repo").order_by(Source.last_success_at.asc().nullsfirst(), Source.id)))
        due = dict(state.get("due", {}))
        mode, request = None, None
        snapshot, cursor_update = {}, {}
        base = {"max_requests": schedule.max_requests, "batch_size": schedule.batch_size}
        if due.get("resume", 0) <= now and any(is_pending(source) for source in sources):
            mode, request = "resume", SyncRequest(**base, resume_pending=True)
        elif due.get("track", 0) <= now and any(not is_pending(source) for source in sources):
            tracked = [RepositoryConfig(repository=source.id.removeprefix("github:"),
                skill_folders=source.sync_state.get("batch", {}).get("scope", []),
                folder_hints=source.sync_state.get("batch", {}).get("folder_hints", []))
                for source in sources if not is_pending(source)][:20]
            mode, request = "track", SyncRequest(**base, repositories=tracked)
        elif due.get("discovery", 0) <= now and (schedule.discovery_queries or schedule.contextual_discovery):
            queries = schedule.discovery_queries
            if schedule.contextual_discovery:
                queries, snapshot, next_cursor = contextual_queries(session, state, schedule, now)
                cursor_update = {"contextual_cursor": next_cursor}
            if queries:
                mode, request = "discovery", SyncRequest(**base, discovery_queries=queries,
                    discovery_sort={query: "best_match" if any(
                        value.get("query_sorts", {}).get(query) == "best_match" for value in snapshot.values())
                        else "updated" for query in queries},
                    discovery_hints=shared_folder_hints([value.get("folder_hints", []) for value in snapshot.values()
                        if value.get("covered")]),
                    discovery_limit=schedule.discovery_limit)
        if request is None:
            return {"status": "idle"}
        # 작업과 기한을 같이 커밋하므로 중복 tick과 워커 재시작도 같은 회차를 만들지 않는다.
        request_key = "refresh:" + state.get("revision", "initial") + ":" + mode + ":" + str(int(now))
        job, _ = enqueue_job(session, request, state.get("profile_id", "local"), request_key)
        if mode == "discovery" and schedule.contextual_discovery:
            job.context_snapshot = snapshot
        interval = getattr(schedule, mode + "_interval_seconds")
        due[mode] = now + interval
        row.state_json = {**state, **cursor_update, "due": due, "last_job_id": job.id, "last_mode": mode}
        row.updated_at = utc_now()

    def factory(**kwargs):
        """워커의 HTTP 시도에도 같은 영속 제한과 주입한 시계를 적용한다."""
        return bind_policy(sessions, client_factory(**kwargs), clock=clock)

    run_job(sessions, job.id, factory)
    with sessions.begin() as session:
        finished = session.get(CatalogSyncJob, job.id)
        errors = {item.get("error") for key in ("items", "discovery_errors")
                  for item in finished.result_json.get(key, [])}
        transient = {"rate_limited", "collection_cooldown", "network_error", "storage_error",
                     "http_500", "http_502", "http_503", "http_504"}
        if errors & transient:
            row = lock_runtime(session)
            current = dict(row.state_json)
            if current.get("revision") == state.get("revision") and current.get("last_job_id") == job.id:
                retry_at = max(clock() + 300, blocked_until(current, clock()) or 0)
                retry_due = dict(current.get("due", {}))
                retry_due[mode] = min(retry_due.get(mode, retry_at), retry_at)
                retry_state = {**current, "due": retry_due}
                if mode == "discovery" and schedule.contextual_discovery:
                    # 일시 실패로 미시도 검색이 건너뛰어지지 않도록 같은 계획부터 재시도한다.
                    retry_state["contextual_cursor"] = state.get("contextual_cursor", {})
                row.state_json = retry_state
                row.updated_at = utc_now()
    return {"status": "submitted", "mode": mode, "job_id": job.id}


def refresh_router(sessions, auth, *, clock=time.time):
    """인증된 운영 조회와 기본 비활성 예약 설정 API를 구성한다."""
    router = APIRouter(prefix="/api/catalog/refresh", tags=["catalog"])

    @router.get("", response_model=RefreshState, summary="수집 요청량과 예약 상태 조회")
    def status(profile_id=Depends(auth)):
        """자체 요청량과 대기 시각을 조회하며 원격 API를 호출하지 않는다."""
        now = clock()
        with sessions.begin() as session:
            row = lock_runtime(session)
            state = row.state_json
            until = blocked_until(state, now)
            window = state.get("window", {})
            return RefreshState(schedule=RefreshSchedule.model_validate(state.get("schedule", {})),
                next_runs={key: iso_time(value) for key, value in state.get("due", {}).items()},
                next_allowed_at=iso_time(until) if until else None,
                blocked_reason=(state.get("blocked_reason") if state.get("blocked_until", 0) > now
                                else "hourly_budget") if until else None,
                hourly_limit=HOURLY_LIMIT,
                window_requests=window.get("count", 0) if window.get("start", 0) + 3600 > now else 0,
                total_requests=state.get("total_requests", 0),
                job_count=session.scalar(select(func.count()).select_from(CatalogSyncJob)),
                last_job_id=state.get("last_job_id"))

    @router.put("/schedule", response_model=RefreshSchedule, summary="수집 예약 설정",
                description="기본 비활성입니다. 설정만으로 워커를 시작하지 않으며 명시적 실행 워커가 필요합니다.")
    def configure(request: RefreshSchedule, profile_id=Depends(auth)):
        """기존 제한과 누적 통계를 보존하며 예약 설정과 회차를 갱신한다."""
        with sessions.begin() as session:
            row = lock_runtime(session)
            row.state_json = {**row.state_json, "schedule": request.model_dump(mode="json"),
                "due": {}, "revision": uuid4().hex, "profile_id": profile_id,
                "contextual_cursor": {"profile_after": "", "batch_profile_ids": [], "query_offset": 0}}
            row.updated_at = utc_now()
        return request

    return router


def main():
    """명시적 네트워크 허용 옵션이 있을 때만 예약 워커를 실행한다."""
    parser = argparse.ArgumentParser(description="명시적으로 켜는 서버 카탈로그 갱신 워커")
    parser.add_argument("--execute-network", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if not args.execute_network:
        print(json.dumps({"status": "disabled", "network_executed": False}))
        return
    engine = configured_engine()
    sessions = session_factory(engine)
    try:
        while True:
            try:
                result = run_refresh_tick(sessions)
            except SQLAlchemyError:
                result = {"status": "storage_unavailable"}
            print(json.dumps(result), flush=True)
            if args.once:
                break
            time.sleep(30)
    except KeyboardInterrupt:
        pass
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
