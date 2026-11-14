"""개인 작업 맥락을 공개 검색어로 바꾸고 공통 수집 예산 안에서 발견한다."""
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from backend.catalog.config import FOLDER_HINT_KEYS
from backend.catalog.dimensions import RULES
from backend.catalog.github_client import GithubClient
from backend.catalog.jobs import SyncJobResponse, SyncRequest, enqueue_job, run_job, serialize_job
from backend.environment_data import current_environment
from backend.models import Profile, Source, WorkMethod
from backend.personalization import build_context


# 자유 입력을 원격 검색어에 연결하지 않고 공개 기술명과 검사 항목만 허용한다.
ANCHORS = {key: key for dimension in ("languages", "frameworks", "tools") for key in RULES[dimension]}
ANCHORS.update({"go": "golang", "codex_cli": "codex", "claude_code": "claude"})
ALIASES = {"py": "python", "ts": "typescript", "js": "javascript", "golang": "go",
    "next.js": "nextjs", "next": "nextjs", "c#": "csharp", "codex": "codex_cli",
    "codex cli": "codex_cli", "claude": "claude_code", "claude code": "claude_code"}
TASK_TERMS = {"code_review": '"code review"', "test_generation": "testing"}
TASK_DISCOVERY_TERMS = {"code_review": '"code review"', "test_generation": '"test-driven"'}
CAPABILITY_TERMS = {
    "input_validation": '"input validation"', "exception_handling": '"error handling"',
    "regression_test": "regression", "test_coverage": "coverage", "documentation": "documentation",
    "api_security": "security", "accessibility": "accessibility", "performance": "performance",
    "browser_testing": "playwright", "test_design": '"test design"',
    "database_changes": "migration", "typing": "typescript",
}
CHECK_PHRASES = {
    "input_validation": ("input validation", "입력 검증"),
    "exception_handling": ("exception handling", "error handling", "예외 처리", "오류 처리"),
    "regression_test": ("regression", "회귀"), "test_coverage": ("coverage", "커버리지"),
    "documentation": ("documentation", "문서화"), "api_security": ("security", "보안"),
    "accessibility": ("accessibility", "접근성"), "performance": ("performance", "성능"),
    "browser_testing": ("browser test", "e2e", "브라우저 테스트"),
    "test_design": ("test design", "테스트 설계"), "database_changes": ("migration", "마이그레이션"),
    "typing": ("type safety", "타입 안전"),
}
LIMITATIONS = [
    "공개 GitHub 저장소 검색과 고정 커밋 수집을 사용하며 전체 인터넷의 유행을 대표하지 않습니다.",
    "최근 검색은 저장소의 90일 내 활동 조건이며 개별 스킬의 출시나 효과를 뜻하지 않습니다.",
    "검색어에는 허용된 기술명과 검사 항목만 넣고 프롬프트, 역할 원문, 프로젝트 경로는 보내지 않습니다.",
    "검색 누락을 줄이기 위해 최근 활동 검색과 기간 제한 없는 검색을 함께 계획합니다.",
    "agent-skills 주제와 저장소 이름은 발견 범위를 좁히는 단서이며 실제 스킬 여부는 고정 원문에서 확인합니다.",
    "후보 발견과 원문 근거 확인은 실행 호환성 승인이나 성능 개선 검증과 구분합니다.",
]


class DiscoveryQuery(BaseModel):
    query: str
    reason: str
    scope: Literal["recent", "evergreen"]
    sort: Literal["updated", "best_match"] = "updated"


class DiscoveryPlan(BaseModel):
    profile_id: str
    profile_version: int
    context_fingerprint: str
    queries: list[DiscoveryQuery]
    folder_hints: list[str] = Field(default_factory=list, max_length=20,
        description="발견한 저장소 안에서 먼저 읽을 정규 기술명과 검사 항목. 기능 지원 근거와 구분")
    tracking: dict[str, int]
    limitations: list[str]
    network_executed: Literal[False] = False


class DiscoverySyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_ids: list[str] = Field(min_length=1, max_length=10)
    max_requests: int = Field(default=20, ge=1, le=50)
    discovery_limit: int = Field(default=2, ge=1, le=5)
    batch_size: int = Field(default=3, ge=1, le=20)

    @field_validator("profile_ids")
    @classmethod
    def validate_ids(cls, values):
        """중복 대상은 순서를 보존해 합치고 제어문자와 과도한 식별자를 거부한다."""
        if any(not value or len(value) > 128 or any(ord(c) < 32 for c in value) for value in values):
            raise ValueError("invalid profile id")
        return list(dict.fromkeys(values))


def canonical(values):
    """이미 알려진 공개 기술명만 정규화하고 자유 입력은 버린다."""
    result = []
    for value in values if isinstance(values, (list, tuple)) else []:
        if not isinstance(value, str):
            continue
        name = value.casefold().strip()
        name = ALIASES.get(name, name)
        if name in ANCHORS and name not in result:
            result.append(name)
    return result


def focus_capabilities(profile, methods):
    """부족하다고 등록한 절차와 필수 검사에서 허용된 기능 식별자만 추린다."""
    gaps = []
    checks = []
    for method in methods.values():
        for step in getattr(method, "workflow_json", None) or []:
            if isinstance(step, dict) and step.get("status") == "gap":
                for capability in step.get("capabilities", []):
                    if capability in CAPABILITY_TERMS:
                        gaps.append((step.get("weight", 1), capability))
        checks.extend(value for value in (method.required_checks or []) if isinstance(value, str))
    context = getattr(profile, "context_json", None) or {}
    values = [capability for _, capability in sorted(gaps, key=lambda row: -row[0])]
    values.extend(profile.goals or [])
    values.extend(context.get("focus_areas", []))
    for capability, phrases in CHECK_PHRASES.items():
        if any(any(phrase in text.casefold() for phrase in phrases) for text in checks):
            values.append(capability)
    excluded = set(context.get("excluded_capabilities", []))
    return list(dict.fromkeys(value for value in values if value in CAPABILITY_TERMS and value not in excluded))


def plan_discovery(profile, methods, environment, *, now=None, tracking=None):
    """현재 맥락에서 최근 검색과 넓은 보완 검색을 만들며 외부 요청은 하지 않는다."""
    context = build_context(profile, methods, environment)
    effective = context.get("effective", {})
    declared = getattr(profile, "context_json", None) or {}
    observed = getattr(environment, "metadata_json", None) or {}
    frameworks = canonical(effective.get("frameworks", observed.get("frameworks") or declared.get("frameworks", [])))
    languages = canonical(effective.get("languages", observed.get("languages") or [profile.primary_language]))
    tools = canonical(effective.get("tools", declared.get("tools", [])))
    anchors = frameworks or languages or tools
    anchor = ANCHORS[anchors[0]] if anchors else ""
    task = next((value for value in profile.task_types if value in TASK_TERMS), "code_review")
    task_term = TASK_TERMS[task]
    moment = now or datetime.now(timezone.utc)
    cutoff = (moment - timedelta(days=90)).date().isoformat()
    suffix = " topic:agent-skills in:readme archived:false fork:false"
    specific = anchor or task_term
    queries = [
        DiscoveryQuery(query=specific + suffix + " pushed:>=" + cutoff,
            scope="recent", reason="현재 기술 환경을 명시한 에이전트 스킬 주제의 최근 활동 저장소 발견"),
        DiscoveryQuery(query=TASK_DISCOVERY_TERMS[task] +
            " topic:agent-skills in:name,description,readme archived:false fork:false", scope="evergreen",
            sort="best_match", reason="등록한 핵심 작업을 직접 명시한 원문 후보 발견: " + task),
        DiscoveryQuery(query="skills in:name topic:agent-skills archived:false fork:false", scope="evergreen",
            sort="best_match",
            reason="기간 제한 없이 스킬 모음 저장소를 발견하고 내부의 관련 스킬부터 확인해 최근 검색 누락 보완"),
    ]
    capabilities = focus_capabilities(profile, methods)
    if capabilities:
        focus = CAPABILITY_TERMS[capabilities[0]]
        specialization = " ".join(value for value in (anchor, focus) if value) + suffix
        if specialization not in {item.query for item in queries}:
            queries.append(DiscoveryQuery(query=specialization, scope="evergreen",
                sort="best_match",
                reason="등록한 부족 검사 또는 우선 목표와 관련된 기능 발견: " + capabilities[0]))
    limitations = list(LIMITATIONS)
    if not anchors:
        limitations.append("알려진 기술 환경이 없어 작업 유형 검색만 생성했습니다. 기술 환경 등록 후 더 좁힐 수 있습니다.")
    excluded = set(declared.get("excluded_tools", [])) | set(declared.get("excluded_capabilities", []))
    hints = list(dict.fromkeys(value for value in frameworks + languages + tools + profile.task_types + capabilities
        if value in FOLDER_HINT_KEYS and value not in excluded))[:20]
    return DiscoveryPlan(profile_id=profile.id, profile_version=profile.version,
        context_fingerprint=context["fingerprint"], queries=queries, folder_hints=hints,
        tracking=tracking or {"enabled_sources": 0, "pending_sources": 0}, limitations=limitations)


def load_plan(session, profile_id, *, now=None):
    """인증 또는 운영자가 지정한 프로필과 기본 기기의 맥락을 읽어 계획을 만든다."""
    profile = session.get(Profile, profile_id)
    if profile is None:
        raise HTTPException(404, detail={"code": "profile_not_found", "message": "먼저 내 작업 환경을 등록해 주세요."})
    methods = {row.task_type: row for row in session.scalars(select(WorkMethod).where(
        WorkMethod.profile_id == profile_id, WorkMethod.is_active.is_(True)))}
    sources = list(session.scalars(select(Source).where(Source.enabled.is_(True))))
    pending = 0
    for source in sources:
        batch = source.sync_state.get("batch", {})
        if source.sync_state.get("queued_config") or batch and batch.get("next", 0) < len(batch.get("folders", [])) + 1:
            pending += 1
    return plan_discovery(profile, methods, current_environment(session, profile_id), now=now,
        tracking={"enabled_sources": len(sources), "pending_sources": pending})


def shared_queries(plans, *, limit=5):
    """프로필마다 하나씩 돌아가며 검색어를 배정하고 공통 쿼리는 한 번만 실행한다."""
    selected = []
    for index in range(max((len(plan.queries) for plan in plans), default=0)):
        for plan in plans:
            if index < len(plan.queries):
                query = plan.queries[index].query
                if query not in selected:
                    selected.append(query)
                if len(selected) == limit:
                    return selected
    return selected


def shared_folder_hints(values, *, limit=20):
    """여러 프로필의 정규 식별자를 순환해 한 사용자의 힌트가 예산을 독점하지 않게 한다."""
    selected = []
    for index in range(max((len(items) for items in values), default=0)):
        for items in values:
            if index < len(items) and items[index] in FOLDER_HINT_KEYS and items[index] not in selected:
                selected.append(items[index])
                if len(selected) == limit:
                    return selected
    return selected


def shared_query_sorts(plans, queries):
    """중복 검색은 한 번만 실행하며 정렬 충돌 시 관련성 정렬을 일관되게 선택한다."""
    sorts = {query: "updated" for query in queries}
    for plan in plans:
        for item in plan.queries:
            if item.query in sorts and item.sort == "best_match":
                sorts[item.query] = "best_match"
    return sorts


def discovery_router(sessions, web_auth, operator_auth, *, client_factory=GithubClient):
    """개인 계획 조회와 운영 예산을 쓰는 실제 수집의 권한을 분리한다."""
    router = APIRouter(prefix="/api/catalog", tags=["catalog"])

    @router.get("/discovery-plan", response_model=DiscoveryPlan, summary="내 작업의 공개 후보 발견 계획")
    def get_plan(profile_id=Depends(web_auth)):
        """현재 로그인 사용자에게만 개인 검색 계획을 보여주고 원격 호출은 하지 않는다."""
        with sessions() as session:
            return load_plan(session, profile_id)

    @router.post("/discovery-sync", status_code=202, response_model=SyncJobResponse,
        summary="프로필 맥락에 따른 공개 후보 수집", description="운영 권한이 필요합니다. 최대 5개 공통 검색어를 만들고 기존 수집 예산과 재개 규칙을 적용합니다.")
    def sync_profiles(request: DiscoverySyncRequest, background: BackgroundTasks,
        operator_id=Depends(operator_auth), request_key: str = Header(..., alias="Idempotency-Key",
            min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")):
        """서버에서 계획을 다시 만들고 같은 요청 키는 기존 수집 작업으로 재사용한다."""
        with sessions.begin() as session:
            plans = [load_plan(session, profile_id) for profile_id in request.profile_ids]
            queries = shared_queries(plans)
            sorts = shared_query_sorts(plans, queries)
            config = SyncRequest(discovery_queries=queries, max_requests=request.max_requests,
                discovery_sort=sorts,
                discovery_hints=shared_folder_hints([plan.folder_hints for plan in plans]),
                discovery_limit=request.discovery_limit, batch_size=request.batch_size, resume_pending=True)
            snapshot = {plan.profile_id: {"fingerprint": plan.context_fingerprint,
                "queries": [item.query for item in plan.queries if item.query in queries],
                "query_sorts": {item.query: sorts[item.query] for item in plan.queries if item.query in queries},
                "folder_hints": plan.folder_hints,
                "covered": any(item.query in queries for item in plan.queries)} for plan in plans}
            job, created = enqueue_job(session, config, operator_id, "discovery:" + request_key)
            if created:
                job.context_snapshot = snapshot
                session.flush()
            elif job.context_snapshot != snapshot:
                raise HTTPException(409, detail={"code": "idempotency_conflict", "message": "같은 요청 키의 프로필 맥락이 변경됐습니다. 새 요청 키로 수집해 주세요."})
            response = serialize_job(job)
        if created:
            background.add_task(run_job, sessions, job.id, client_factory)
        return response

    return router
