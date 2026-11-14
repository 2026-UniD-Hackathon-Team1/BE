"""모든 수집 경로에 공통으로 적용하는 영속 요청 예산과 재시도 대기."""
import math
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from backend.catalog.github_client import CollectionError
from backend.models import CatalogRuntime, utc_now

HOURLY_LIMIT = 60


def iso_time(epoch):
    """운영 응답에 표시할 UTC 시각을 만든다."""
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def lock_runtime(session):
    """여러 프로세스에서 상태 초기화와 갱신을 한 행 잠금으로 직렬화한다."""
    session.execute(insert(CatalogRuntime).values(id="github", state_json={}, updated_at=utc_now())
                    .on_conflict_do_nothing(index_elements=["id"]))
    return session.scalar(select(CatalogRuntime).where(CatalogRuntime.id == "github").with_for_update())


def blocked_until(state, now):
    """외부 제한과 자체 시간당 예산 중 더 늦은 해제 시각을 선택한다."""
    until = state.get("blocked_until", 0)
    window = state.get("window", {})
    if window.get("start", 0) + 3600 > now and window.get("count", 0) >= HOURLY_LIMIT:
        until = max(until, window["start"] + 3600)
    return until if until > now else None


def retry_epoch(headers, now):
    """초와 HTTP 날짜 Retry-After 및 epoch reset 중 유효한 대기 안내를 읽는다."""
    candidates = [now + 60]
    retry = headers.get("Retry-After", "")
    if retry.isdigit() and len(retry) <= 10:
        candidates.append(now + int(retry))
    elif retry:
        try:
            candidates.append(parsedate_to_datetime(retry).timestamp())
        except (ValueError, TypeError, OverflowError):
            pass
    reset = headers.get("X-RateLimit-Reset", "")
    if reset.isdigit() and len(reset) <= 10:
        candidates.append(int(reset) + 1)
    # 잘못된 원격 시각으로 영구 대기하지 않도록 datetime이 지원하는 범위를 유지한다.
    return min(max(candidates), 253402300799)


class RequestPolicy:
    def __init__(self, sessions, *, clock=time.time):
        """DB와 시계를 주입받아 실제 대기 없이 제한 정책을 검증할 수 있게 한다."""
        self.sessions = sessions
        self.clock = clock

    def reserve(self):
        """HTTP 시도 직전에 요청 하나를 예약하고 제한 중에는 외부 호출을 거부한다."""
        now = self.clock()
        with self.sessions.begin() as session:
            row = lock_runtime(session)
            state = dict(row.state_json)
            until = blocked_until(state, now)
            if until:
                raise CollectionError("collection_cooldown", retry_after=str(math.ceil(until - now)),
                                      reset_at=str(math.ceil(until)))
            window = dict(state.get("window", {}))
            if not window or window.get("start", 0) + 3600 <= now:
                window = {"start": now, "count": 0}
            window["count"] = window.get("count", 0) + 1
            state.update(window=window, total_requests=state.get("total_requests", 0) + 1)
            row.state_json = state
            row.updated_at = utc_now()

    def observe(self, status, headers):
        """응답 본문 없이 요청 제한과 재시도 시각만 영속화한다."""
        exhausted = headers.get("X-RateLimit-Remaining") == "0"
        if status not in {401, 403, 429} and not exhausted:
            return
        now = self.clock()
        until = retry_epoch(headers, now)
        with self.sessions.begin() as session:
            row = lock_runtime(session)
            row.state_json = {**row.state_json,
                "blocked_until": max(row.state_json.get("blocked_until", 0), until),
                "blocked_reason": "authentication_failed" if status == 401 else "rate_limit_or_forbidden"}
            row.updated_at = utc_now()


def bind_policy(sessions, client, *, clock=time.time):
    """PC 인증을 읽지 않고 기존 HTTP 클라이언트에 DB 제한 검사를 연결한다."""
    policy = RequestPolicy(sessions, clock=clock)
    client.before_request = policy.reserve
    client.observe_response = policy.observe
    return client
