"""후보 상세의 출처와 선언 및 실행 지원을 구분하는 프론트 계약."""
from typing import Literal

from pydantic import BaseModel, Field

from backend.schemas import TagEvidence
from backend.catalog.schemas import SourceMetadata


class PermissionEvidence(BaseModel):
    path: str
    line: int
    source_url: str | None = None


class DeclaredPermission(BaseModel):
    kind: Literal["file_read", "file_write", "network", "command_exec"]
    description: str
    additional: bool | None = Field(default=None, description="현재 환경 대비 추가 권한인지 확인하지 못하면 null")
    evidence: list[PermissionEvidence]


class CandidatePermissions(BaseModel):
    declared: list[DeclaredPermission]
    allowed: dict[str, bool] = Field(default_factory=dict, description="검토된 정책의 명시적 권한 여부만 제공하며 경로와 명령은 제외")
    verification: Literal["unknown", "declared", "partial"]


class Maintainer(BaseModel):
    author: str | None = None
    license: str | None = None
    last_updated_at: str | None = None


class CandidateDetail(BaseModel):
    candidate_version_id: str
    asset_id: str
    kind: str
    title: str
    summary: str | None
    revision: str
    bundle_sha256: str
    source_url: str | None
    published_at: str | None
    first_seen_at: str | None
    verified_source_at: str | None
    provenance: Literal["official", "known_author", "community", "unverified"]
    source_verification: Literal["verified", "unverified", "demo"]
    integrity_status: Literal["verified", "unknown", "mismatch", "invalid"]
    maintainer: Maintainer
    source_metadata: SourceMetadata = Field(default_factory=SourceMetadata)
    compatibility: Literal["confirmed", "unknown", "incompatible"]
    compatibility_reasons: list[str]
    permissions: CandidatePermissions
    risk_signals: list[str]
    risk_signal_codes: list[str]
    replaces: str | None = None
    claimed_benefit: str | None = None
    support_level: Literal["information_only", "comparable", "applicable"]
    adapter_id: str | None
    adapter_review_status: Literal["missing", "unreviewed", "hash_mismatch", "reviewed", "ambiguous"]
    task_types: list[str]
    dimensions: dict = Field(default_factory=dict, description="원문에 명시된 언어와 프레임워크 및 도구와 기능의 근거")
    tag_evidence: list[TagEvidence]
    related_reasons: list[str]
    is_current: bool | None
    demo: bool
    security_audit_status: Literal["unverified"] = "unverified"
