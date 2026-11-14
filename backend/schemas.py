"""프론트와 공유하는 요청 및 응답 계약."""
from typing import Literal

from pydantic import BaseModel, Field


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
