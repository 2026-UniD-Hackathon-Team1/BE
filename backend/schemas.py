"""프론트와 공유하는 요청 및 응답 계약."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from backend.catalog.schemas import SourceMetadata


class ErrorDetail(BaseModel):
    code: str
    message: str
    job_id: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    database: Literal["ready"] = "ready"


class TagEvidence(BaseModel):
    category: Literal["task_types", "capabilities"]
    tag: str
    path: str
    line: int
    excerpt: str


class CandidateSummary(BaseModel):
    candidate_version_id: str = Field(examples=["demo_ver_01"])
    asset_id: str
    kind: str
    title: str
    revision: str
    bundle_sha256: str
    source_url: str
    first_seen_at: str
    support_level: Literal["information_only"] = "information_only"
    performance_status: Literal["not_compared"] = "not_compared"
    classification_status: str
    tag_evidence: list[TagEvidence] = Field(default_factory=list)
    demo: bool


class CandidateList(BaseModel):
    items: list[CandidateSummary]
    total: int
    limit: int
    offset: int


class SourceSummary(BaseModel):
    source_id: str
    kind: str
    url: str
    enabled: bool
    last_success_at: str | None
    last_error: str | None


class SourceList(BaseModel):
    items: list[SourceSummary]


TaskType = Literal["code_review", "test_generation"]
Goal = Literal["input_validation", "exception_handling", "regression_test", "test_coverage", "documentation",
               "api_security", "accessibility", "performance", "browser_testing", "test_design", "database_changes", "typing"]


class ProfileContext(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    frameworks: list[str] = Field(default_factory=list, max_length=15,
        description="사용자가 직접 등록한 프레임워크이며 프로젝트 스캔으로 확인한 관측값과 구분합니다.")
    tools: list[str] = Field(default_factory=list, max_length=15,
        description="사용자가 직접 등록한 AI 도구와 보조 도구이며 설치나 실행 가능성을 검증한 값이 아닙니다.")
    additional_languages: list[str] = Field(default_factory=list, max_length=10,
        description="사용자가 직접 등록한 추가 언어입니다. 현재 프로젝트의 관측 언어와 다를 수 있습니다.")
    focus_areas: list[Goal] = Field(default_factory=list, max_length=12,
        description="추천에서 우선 연결할 실제 작업 기능입니다.")
    excluded_capabilities: list[Goal] = Field(default_factory=list, max_length=12)
    excluded_tools: list[str] = Field(default_factory=list, max_length=15)
    prefer_recent: bool = Field(default=True,
        description="검증된 원본 변경 시각에 한해 약한 최신성 가점을 적용하며 성능이나 출시일을 뜻하지 않습니다.")

    @field_validator("frameworks", "tools", "additional_languages", "excluded_tools")
    @classmethod
    def normalized_labels(cls, values):
        """짧은 기술 이름만 저장하고 검색에 쓸 수 없는 자유 문장과 경로는 차단한다."""
        import re
        if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._+#-]{0,39}", value) for value in values):
            raise ValueError("short technology labels required")
        return list(dict.fromkeys(value.lower() for value in values))


class WorkflowStep(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,40}$")
    label: str = Field(min_length=1, max_length=120)
    capabilities: list[Goal] = Field(min_length=1, max_length=12)
    status: Literal["covered", "gap"] = Field(
        description="covered는 이미 수행한다는 사용자 선언이고 gap은 보완하려는 단계입니다. 실제 실행 검증 결과가 아닙니다.")
    weight: int = Field(default=3, ge=1, le=5)


class ProfileInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    role: str = Field(min_length=1, max_length=100, examples=["백엔드 개발"])
    primary_language: str = Field(min_length=1, max_length=40, examples=["python"])
    task_types: list[TaskType] = Field(min_length=1, max_length=2, examples=[["code_review"]])
    goals: list[Goal] = Field(default_factory=list, max_length=12)
    preferences: dict[str, bool | str | int | float] = Field(default_factory=dict, max_length=20)
    context: ProfileContext = Field(default_factory=ProfileContext)

    @field_validator("task_types", "goals")
    @classmethod
    def unique_tags(cls, values):
        """사용자가 중복 입력한 작업 및 목표 태그를 순서대로 정리한다."""
        return list(dict.fromkeys(values))


class ProfileResponse(ProfileInput):
    profile_id: str
    profile_version: int
    updated_at: str


class ProfileSaved(BaseModel):
    profile_id: str
    profile_version: int
    recommendations_stale: bool


class WorkMethodInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    task_type: TaskType
    prompt_text: str = Field(min_length=1, max_length=20000, examples=["변경된 함수의 입력 검증과 예외 처리를 확인해 줘."])
    required_checks: list[str] = Field(default_factory=list, max_length=30)
    scoring_preferences: dict[str, str] = Field(default_factory=dict, max_length=20)
    workflow: list[WorkflowStep] = Field(default_factory=list, max_length=12)

    @field_validator("workflow")
    @classmethod
    def distinct_steps(cls, values):
        """불변 작업 방식 안에서 단계 식별자가 서로 겹치지 않게 한다."""
        if len({step.id for step in values}) != len(values):
            raise ValueError("duplicate workflow step")
        return values


class WorkMethodResponse(WorkMethodInput):
    method_id: str
    version: int
    is_active: bool
    created_at: str


class RecommendationAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["new", "snoozed", "dismissed", "kept"]
    reason: str | None = Field(default=None, max_length=500)


class LatestComparison(BaseModel):
    comparison_id: str
    status: str
    verdict: str | None = None


class ContextEvidence(BaseModel):
    """추천에 사용한 공개 원문의 차원과 위치를 설명한다."""
    dimension: Literal["task_types", "capabilities", "languages", "frameworks", "tools"]
    value: str = Field(description="원문에서 명시적으로 확인한 작업 태그나 기술 이름입니다.")
    path: str = Field(min_length=1, description="후보 문서 안의 상대 파일 경로이며 사용자 프로젝트의 절대 경로가 아닙니다.")
    line: int = Field(ge=1, description="원문의 1부터 시작하는 줄 번호입니다.")
    excerpt: str = Field(description="짧은 원문 근거입니다. 제공자 설명을 확인한 것이며 성능이나 안전성을 검증한 것은 아닙니다.")


class WorkflowFit(BaseModel):
    """기존 작업 단계와 후보의 선언 기능이 연결되는 이유를 설명한다."""
    step_id: str
    label: str
    status: Literal["covered", "gap"] = Field(
        description="등록한 작업 방식의 사용자 선언 상태이며 실제 수행 결과를 측정한 값이 아닙니다.")
    weight: int = Field(ge=1, le=5)
    matched_capabilities: list[Goal] = Field(default_factory=list)
    relation: Literal["fills_gap", "overlaps_existing"] = Field(
        description="fills_gap은 보완 후보이고 overlaps_existing은 현재 검사와의 중복입니다. 실제 개선 효과는 비교가 필요합니다.")
    source: Literal["declared_workflow", "required_check", "explicit_prompt"] = Field(
        description="사용자 등록 단계, 필수 검사 또는 프롬프트의 명시적 검사에서 연결 근거를 얻은 출처입니다.")
    evidence: list[ContextEvidence] = Field(default_factory=list,
        description="연결한 후보 기능의 공개 원문 근거이며 없으면 추가 확인이 필요합니다.")


class FreshnessSignal(BaseModel):
    """확인된 저장소 변경 시각과 미확인 상태를 구분한다."""
    status: Literal["observed", "unknown"] = Field(default="unknown",
        description="observed는 원본 저장소 시각을 확인한 상태이고 unknown은 최신성 근거가 없는 상태입니다.")
    basis: Literal["revision_committed_at", "repository_pushed_at"] | None = Field(default=None,
        description="고정 리비전 커밋 또는 저장소 push 시각입니다. 서비스 최초 발견일은 사용하지 않습니다.")
    timestamp: str | None = Field(default=None, description="확인된 원본의 UTC 시각이며 미확인이면 null입니다.")
    age_days: int | None = Field(default=None, ge=0, description="평가 시점부터 원본 변경까지의 경과 일수이며 미확인이면 null입니다.")
    bonus: int = Field(default=0, ge=0, le=3, description="관련성 점수에 더하는 작은 가점이며 성능 점수가 아닙니다.")
    is_release_date: Literal[False] = Field(default=False,
        description="저장소 변경 시각을 공식 출시일로 해석하지 않습니다.")


class RecommendationMatches(BaseModel):
    """선택한 작업과 기능 및 기술 문맥의 일치 항목을 제공한다."""
    model_config = ConfigDict(extra="allow")
    task_type: TaskType | None = None
    task_types: list[TaskType] = Field(default_factory=list)
    method_id: str | None = None
    method_version: int | None = Field(default=None, ge=1)
    goals: list[Goal] = Field(default_factory=list)
    capabilities: list[Goal] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    requested_capabilities: list[Goal] = Field(default_factory=list,
        description="등록한 목표와 선택한 작업 방식의 공백 및 명시적 검사에서 얻은 기능입니다.")
    matched_requested_capabilities: list[Goal] = Field(default_factory=list)
    confidence: Literal["contextual", "abstained", "limited"] = Field(default="limited",
        description="개인 문맥 연결, 추천 보류 또는 제한된 근거 상태이며 실행 호환성이나 효과의 검증 수준이 아닙니다.")


class RecommendationAssessment(BaseModel):
    """추천 적합성과 근거를 성능 검증 결과와 구분해서 제공한다."""
    eligible: bool = Field(default=False, description="현재 개인 문맥에서 추천할 조건을 충족했는지이며 설치나 실행 승인이 아닙니다.")
    score: int = Field(default=0, ge=0, le=100, description="작업 관련성 점수이며 성능 개선율이나 실행 결과 점수가 아닙니다.")
    reasons: list[str] = Field(default_factory=list)
    excluded_reasons: list[str] = Field(default_factory=list)
    breakdown: dict[str, int] = Field(default_factory=dict,
        description="관련성 가점과 중복 감점의 항목별 점수입니다. 감점은 음수일 수 있습니다.")
    matches: RecommendationMatches = Field(default_factory=RecommendationMatches)
    workflow_fit: list[WorkflowFit] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list, description="호환성이나 원문 및 환경에서 아직 확인하지 못한 조건입니다.")
    freshness: FreshnessSignal = Field(default_factory=FreshnessSignal)
    evidence: list[ContextEvidence] = Field(default_factory=list)
    context_fingerprint: str = Field(default="", description="평가한 사용자 문맥의 지문이며 프롬프트 원문을 포함하지 않습니다.")


class RecommendationCard(BaseModel):
    source_metadata: SourceMetadata = Field(default_factory=SourceMetadata)
    recommendation_id: str
    candidate_version_id: str
    adapter_id: str | None
    work_method_id: str | None
    work_method_version: int | None
    title: str
    change_type: str
    task_types: list[str]
    score: int = Field(ge=0, le=100, description="작업 관련성 점수이며 성능 점수가 아닙니다.")
    related_reasons: list[str]
    tag_evidence: list[TagEvidence] = Field(default_factory=list)
    source_url: str
    compatibility: Literal["unknown", "confirmed"]
    support_level: Literal["information_only", "comparable", "applicable"]
    performance_status: Literal["not_compared"] = "not_compared"
    status: Literal["new", "snoozed", "dismissed", "kept"]
    reason: str | None
    latest_comparison: LatestComparison | None
    demo: bool
    assessment: RecommendationAssessment = Field(default_factory=RecommendationAssessment,
        description="작업 단계와 맥락 및 원문 근거에 따른 관련성 평가이며 성능 점수가 아닙니다.")


class RecommendationList(BaseModel):
    items: list[RecommendationCard]
    context: dict = Field(default_factory=dict)
    excluded: list[dict] = Field(default_factory=list, description="include_excluded 요청 시 개인 맥락에서 제외한 후보와 이유")
