"""실제 수집 대상은 운영자가 지정한 설정에서만 읽는다."""
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.catalog.dimensions import RULES


FOLDER_HINT_KEYS = {"code_review", "test_generation"} | {
    key for values in RULES.values() for key in values
}
FIXTURE_COMPONENTS = {"tests", "test", "__tests__", "fixtures", "fixture", "testdata",
    "node_modules", ".venv", ".git"}


def is_fixture_folder(path):
    """정확한 경로 구성요소로 저장소 테스트 자료와 의존성 폴더를 구분한다."""
    return isinstance(path, str) and any(part.casefold() in FIXTURE_COMPONENTS for part in path.split("/"))


def validate_folder_hints(values):
    """자유 문장 대신 허용된 기술 식별자만 중복 없이 우선순위 힌트로 받는다."""
    if not isinstance(values, list) or any(
        not isinstance(value, str) or value not in FOLDER_HINT_KEYS for value in values
    ):
        raise ValueError("folder hints must be canonical technology or task keys")
    return list(dict.fromkeys(values))


class RepositoryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    repository: str
    skill_folders: list[str] = Field(default_factory=list, max_length=20)
    folder_hints: list[str] = Field(default_factory=list, max_length=20,
        description="자동 발견 폴더를 먼저 읽을 순서의 약한 힌트이며 기능 지원 근거와 구분")


    # 빈 폴더 목록은 저장소 트리에서 SKILL.md를 자동 탐색한다.

    @field_validator("folder_hints", mode="before")
    @classmethod
    def validate_hints(cls, values):
        """중복을 제거한 최대 20개 정규 식별자만 허용한다."""
        return validate_folder_hints(values)

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
    discovery_sort: dict[str, Literal["updated", "best_match"]] = Field(default_factory=dict, max_length=5,
        description="검색어별 최신 갱신 또는 GitHub 기본 관련성 정렬. 생략한 검색어는 updated 사용")
    discovery_hints: list[str] = Field(default_factory=list, max_length=20,
        description="검색으로 찾은 큰 저장소 안에서 관련 폴더를 먼저 읽을 정규 식별자")
    discovery_limit: int = Field(default=5, ge=1, le=20)
    batch_size: int = Field(default=5, ge=1, le=20, description="저장소당 이번 요청에서 처리할 최대 번들 수")
    resume_pending: bool = Field(default=False, description="이미 발견한 미완료 후보를 DB 커서부터 수집 대상에 포함")

    @model_validator(mode="after")
    def validate_query_sorts(self):
        """정렬 설정이 이번 작업의 검색어를 벗어나거나 별도 검색을 추가하지 못하게 한다."""
        if set(self.discovery_sort) - set(self.discovery_queries):
            raise ValueError("discovery sort keys must be planned queries")
        return self

    @field_validator("discovery_hints", mode="before")
    @classmethod
    def validate_hints(cls, values):
        """검색 문구나 개인정보를 폴더 우선순위 힌트로 저장하지 않는다."""
        return validate_folder_hints(values)

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
