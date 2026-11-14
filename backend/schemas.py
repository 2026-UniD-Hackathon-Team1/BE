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
    frameworks: list[str] = Field(default_factory=list, max_length=15)
    tools: list[str] = Field(default_factory=list, max_length=15)
    additional_languages: list[str] = Field(default_factory=list, max_length=10)
    focus_areas: list[Goal] = Field(default_factory=list, max_length=12)
    excluded_capabilities: list[Goal] = Field(default_factory=list, max_length=12)
    excluded_tools: list[str] = Field(default_factory=list, max_length=15)
    prefer_recent: bool = True

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
    status: Literal["covered", "gap"]
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
    score: int
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
    assessment: dict = Field(default_factory=dict, description="작업 단계와 맥락 및 원문 근거에 따른 관련성 평가이며 성능 점수가 아님")


class RecommendationList(BaseModel):
    items: list[RecommendationCard]
    context: dict = Field(default_factory=dict)
    excluded: list[dict] = Field(default_factory=list, description="include_excluded 요청 시 개인 맥락에서 제외한 후보와 이유")
