"""프론트가 수집 완료와 미완료 및 실패를 구분할 응답 계약."""
from typing import Literal

from pydantic import BaseModel, Field


class RepositoryProgress(BaseModel):
    repository: str
    status: Literal["done", "partial", "failed", "disabled"]
    new_versions: int = Field(ge=0)
    commit_sha: str | None = None
    completed_units: int | None = Field(default=None, ge=0, description="저장 완료한 스킬 번들과 저장소 README의 누적 개수")
    total_units: int | None = Field(default=None, ge=1, description="이번 고정 커밋 회차의 전체 번들 개수")
    resume_required: bool | None = Field(default=None, description="true면 새 요청 키와 resume_pending으로 이어서 수집")
    pause_reason: str | None = None
    error: str | None = None
    retry_after: str | None = None
    reset_at: str | None = None


class DiscoveryFailure(BaseModel):
    query: str
    error: str
    retry_after: str | None = None
    reset_at: str | None = None


class CollectionResult(BaseModel):
    items: list[RepositoryProgress] = Field(default_factory=list)
    discovery_errors: list[DiscoveryFailure] = Field(default_factory=list)
