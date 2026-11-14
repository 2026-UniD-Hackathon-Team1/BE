"""B-owned response shape for the side-by-side comparison screen."""
from typing import Any, Literal

from pydantic import BaseModel, Field


class ComparedInput(BaseModel):
    input_id: str
    status: Literal["queued", "completed", "failed", "timeout", "output_limit"]
    result: "ReviewResult | None" = None
    output_text: str | None = None
    seconds: float | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    skill_usage_status: Literal["not_expected", "confirmed", "unconfirmed"]
    failure_code: str | None = None


class RunResult(BaseModel):
    inputs: list[ComparedInput]


class ComparisonRun(BaseModel):
    arm: Literal["baseline", "candidate"]
    status: Literal["queued", "running", "completed", "failed", "cancelled", "timeout", "interrupted"]
    result: RunResult | None = None
    output_text: str | None = None
    seconds: float | None = None
    skill_usage_status: Literal["not_expected", "confirmed", "unconfirmed"]
    failure_code: str | None = None
    inputs: list[ComparedInput]


class ReviewFinding(BaseModel):
    path: str
    line: int
    severity: Literal["high", "medium", "low"]
    issue: str
    evidence: str
    suggested_fix: str


class ReviewResult(BaseModel):
    findings: list[ReviewFinding]
    summary: str


class InputManifest(BaseModel):
    input_id: str
    relative_paths: list[str] = Field(default_factory=list)
    task_type: str | None = None
    input_sha256: str | None = None


class ComparisonPlan(BaseModel):
    model_id: str
    runner_version: str | None = None
    timeout_seconds: int | None = None
    idempotency_key: str | None = None


class ComparisonDetail(BaseModel):
    comparison_id: str
    status: Literal["queued", "running", "evaluating", "done", "failed", "cancelled"]
    verdict: Literal["baseline", "candidate", "abstain"] | None = Field(
        description="사용자가 저장한 선택. 시스템의 자동 우열 판정이 아니다.")
    verdict_reason: str | None = None
    baseline_label: str
    candidate_label: str
    change_title: str
    rubric_version: str
    runs_per_arm: int
    method_id: str
    method_version: int
    candidate_version_id: str
    inputs: list[InputManifest]
    plan: ComparisonPlan
    runs: list[ComparisonRun]
    findings: list[dict[str, Any]] = Field(default_factory=list)
    llm_judgment: dict[str, Any] | None = None
    limits: list[str]
    is_prerecorded: bool
