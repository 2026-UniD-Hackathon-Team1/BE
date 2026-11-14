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


class RequirementEvidence(BaseModel):
    path: str
    line: int = Field(ge=1)
    excerpt: str


class DeclaredRequirement(BaseModel):
    kind: Literal["cli", "external_api", "host_model", "package_files"]
    label: str
    verification: Literal["declared"] = "declared"
    evidence: RequirementEvidence


class CandidateRequirements(BaseModel):
    items: list[DeclaredRequirement] = Field(default_factory=list)
    external_api: Literal["declared_required", "declared_not_required", "unknown"] = Field(default="unknown",
        description="원문 선언이며 별도 API가 없다는 표기가 전체 작업의 무료 사용을 뜻하지 않습니다.")
    cost_status: Literal["possible", "unknown"] = "unknown"
    setup_verified: Literal[False] = Field(default=False, description="사용자의 설치나 결제 설정은 검사하거나 실행하지 않았습니다.")


class DeferredUnit(BaseModel):
    folder: str
    required_requests: int = Field(ge=0)
    reason: Literal["request_budget_insufficient", "bundle_request_budget_exceeded"]


class RepositoryProgress(BaseModel):
    repository: str
    status: Literal["done", "partial", "failed", "disabled"]
    new_versions: int = Field(ge=0)
    commit_sha: str | None = None
    completed_units: int | None = Field(default=None, ge=0, description="저장 완료한 스킬 번들과 저장소 README의 누적 개수")
    total_units: int | None = Field(default=None, ge=1, description="이번 고정 커밋 회차의 전체 번들 개수")
    resume_required: bool | None = Field(default=None, description="true면 새 요청 키와 resume_pending으로 이어서 수집")
    pause_reason: str | None = None
    deferred_units: list[DeferredUnit] = Field(default_factory=list, max_length=20,
        description="예산 때문에 이번 회차에서 보류한 번들. 원문이나 완료 상태를 부분 저장하지 않음")
    error: str | None = None
    retry_after: str | None = None
    reset_at: str | None = None


class DiscoveryFailure(BaseModel):
    query: str
    error: str
    retry_after: str | None = None
    reset_at: str | None = None


class DiscoveryProgress(BaseModel):
    query: str
    status: Literal["done", "failed", "not_attempted"] = Field(
        description="done은 빈 결과도 포함한 검색 성공이며, 실패와 아직 실행하지 않은 검색을 구분")
    repositories: list[str] = Field(default_factory=list,
        description="검색에서 발견한 저장소. 원문 수집 완료 여부는 items에서 별도로 확인")
    error: str | None = None


class CollectionResult(BaseModel):
    items: list[RepositoryProgress] = Field(default_factory=list)
    discovery_errors: list[DiscoveryFailure] = Field(default_factory=list)
    discovery: list[DiscoveryProgress] = Field(default_factory=list)
