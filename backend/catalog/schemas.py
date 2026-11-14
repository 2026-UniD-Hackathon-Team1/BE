"""프론트가 수집 완료와 미완료 및 실패를 구분할 응답 계약."""
from typing import Literal

from pydantic import BaseModel, Field


class SourceMetadata(BaseModel):
    publisher: str | None = Field(default=None, description="저장소 게시 계정이며 실제 스킬 작성자와 구분")
    publisher_url: str | None = None
    repository_license: str | None = Field(default=None, description="수집 당시 저장소 API의 선언이며 개별 스킬의 라이선스 확인과 구분")
    repository_created_at: str | None = None
    repository_pushed_at: str | None = None
    revision_committed_at: str | None = Field(default=None, description="고정 저장소 커밋 시각이며 스킬 파일의 최종 수정 시각과 구분")
    metadata_observed_at: str | None = None


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
