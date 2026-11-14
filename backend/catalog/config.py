"""실제 수집 대상은 운영자가 지정한 설정에서만 읽는다."""
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RepositoryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    repository: str
    skill_folders: list[str] = Field(default_factory=list, max_length=20)


    # 빈 폴더 목록은 저장소 트리에서 SKILL.md를 자동 탐색한다.

    @field_validator("repository")
    @classmethod
    def validate_repository(cls, value):
        """URL 대신 owner/repo 형식의 공개 저장소 식별자만 허용한다."""
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", value):
            raise ValueError("repository must be owner/repo")
        if value.split("/")[1] in {".", ".."}:
            raise ValueError("invalid repository")
        return value.lower()

    @field_validator("skill_folders")
    @classmethod
    def validate_folders(cls, values):
        """상위 경로 이동과 중복 폴더 없이 명시된 상대 경로만 허용한다."""
        for value in values:
            validate_repo_path(value, allow_root=True)
        if len(set(values)) != len(values):
            raise ValueError("duplicate skill folder")
        return values


class CollectionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repositories: list[RepositoryConfig] = Field(default_factory=list, max_length=20)
    discovery_queries: list[str] = Field(default_factory=list, max_length=5)
    discovery_limit: int = Field(default=5, ge=1, le=20)
    batch_size: int = Field(default=5, ge=1, le=20, description="저장소당 이번 요청에서 처리할 최대 번들 수")
    resume_pending: bool = Field(default=False, description="이미 발견한 미완료 후보를 DB 커서부터 수집 대상에 포함")

    @field_validator("discovery_queries")
    @classmethod
    def validate_queries(cls, values):
        """검색량을 제한하고 비어 있거나 지나치게 긴 검색어를 거부한다."""
        if any(not value.strip() or len(value) > 200 for value in values):
            raise ValueError("query must contain 1 to 200 characters")
        return list(dict.fromkeys(values))

    @field_validator("repositories")
    @classmethod
    def validate_unique(cls, values):
        """동일 저장소가 서로 다른 설정으로 중복 실행되지 않도록 검사한다."""
        names = [item.repository for item in values]
        if len(names) != len(set(names)):
            raise ValueError("duplicate repository")
        return values


def validate_repo_path(value: str, *, allow_root=False):
    """Git 경로에 절대 경로나 Windows 경로 구분자가 섞이지 않도록 검사한다."""
    if allow_root and value == ".":
        return
    if not value or any(c in value for c in ("\\", ":", "\x00")):
        raise ValueError("invalid repository path")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("invalid repository path")
    if any(ord(c) < 32 for c in value):
        raise ValueError("invalid repository path")


def load_config(path: Path) -> CollectionConfig:
    """로컬 JSON 설정을 검증하며 토큰 등 비밀값은 설정에 받지 않는다."""
    return CollectionConfig.model_validate_json(path.read_text(encoding="utf-8"))
