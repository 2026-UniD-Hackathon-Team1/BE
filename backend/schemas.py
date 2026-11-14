"""프론트와 공유하는 요청 및 응답 계약."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetail


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    database: Literal["ready"] = "ready"


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
Goal = Literal["input_validation", "exception_handling", "regression_test", "test_coverage", "documentation"]


class ProfileInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    role: str = Field(min_length=1, max_length=100, examples=["백엔드 개발"])
    primary_language: str = Field(min_length=1, max_length=40, examples=["python"])
    task_types: list[TaskType] = Field(min_length=1, max_length=2, examples=[["code_review"]])
    goals: list[Goal] = Field(default_factory=list, max_length=5)
    preferences: dict[str, bool | str | int | float] = Field(default_factory=dict, max_length=20)

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
    source_url: str
    compatibility: Literal["unknown", "confirmed"]
    support_level: Literal["information_only", "comparable", "applicable"]
    performance_status: Literal["not_compared"] = "not_compared"
    status: Literal["new", "snoozed", "dismissed", "kept"]
    reason: str | None
    latest_comparison: LatestComparison | None
    demo: bool


class RecommendationList(BaseModel):
    items: list[RecommendationCard]
