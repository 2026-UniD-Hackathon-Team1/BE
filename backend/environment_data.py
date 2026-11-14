"""PC 스캔 결과를 엄격하게 검증하고 서버 파일 경로와 분리해 저장한다."""
import hashlib
import json
from pathlib import PurePosixPath, PureWindowsPath
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from backend.models import Environment, AgentDevice
from sqlalchemy import select

Scope = Literal["config", "skills", "code"]


def current_environment(session, profile_id):
    """기본 기기의 환경만 선택하고 다른 기기나 과거 로컬 환경으로 대체하지 않는다."""
    primary = session.scalar(select(AgentDevice.id).where(AgentDevice.profile_id == profile_id,
        AgentDevice.is_primary.is_(True), AgentDevice.revoked_at.is_(None)))
    query = select(Environment).where(Environment.profile_id == profile_id)
    if primary is not None:
        query = query.where(Environment.device_id == primary)
    else:
        paired = session.scalar(select(AgentDevice.id).where(AgentDevice.profile_id == profile_id).limit(1))
        if paired is not None:
            return None
        query = query.where(Environment.device_id.is_(None))
    return session.scalar(query.order_by(Environment.scanned_at.desc()).limit(1))


FileName = Literal["pyproject.toml", "requirements.txt", "package.json", "Cargo.toml", "go.mod", ".codex/config.toml"]
Summary = Literal["Python 프로젝트 설정", "Python 패키지 목록", "JavaScript 프로젝트 설정", "Rust 프로젝트 설정", "Go 프로젝트 설정", "Codex 프로젝트 설정"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScanInput(StrictModel):
    root: str = Field(min_length=1, max_length=1024)
    scopes: list[Scope] = Field(min_length=1, max_length=3)

    @field_validator("root")
    @classmethod
    def validate_root(cls, value):
        """서버에서 경로를 열지 않으며 제어문자와 URI 입력을 차단한다."""
        if "://" in value or any(ord(char) < 32 for char in value):
            raise ValueError("invalid project root")
        return value

    @field_validator("scopes")
    @classmethod
    def validate_scopes(cls, value):
        """설정 읽기 승인을 필수로 하고 중복 범위를 정리한다."""
        if "config" not in value:
            raise ValueError("config scope is required")
        return sorted(set(value))


class InstalledSkill(StrictModel):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    version: Literal["unknown"]
    source: str = Field(pattern=r"^\.agents/skills/[A-Za-z0-9][A-Za-z0-9_.-]{0,63}/SKILL\.md$")
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    excluded: Literal[False]


class SettingsFile(StrictModel):
    path: FileName
    summary: Summary
    readable: bool
    excluded: bool


class ExcludedPath(StrictModel):
    path: str = Field(min_length=1, max_length=256)
    reason: Literal["링크 경로는 읽지 않습니다.", "파일 크기 제한을 초과했습니다.", "파일을 읽을 수 없습니다.",
        "스캔 중 파일이 바뀌었습니다.", "스캔 범위 제한을 초과했습니다.", "허용하지 않는 이름입니다."]

    @field_validator("path")
    @classmethod
    def validate_path(cls, value):
        """제외 목록에도 절대 경로나 상위 이동 및 제어문자를 허용하지 않는다."""
        if (PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()
            or "\\" in value or ".." in value.split("/") or any(ord(char) < 32 for char in value)):
            raise ValueError("invalid relative path")
        return value


class ScanResult(StrictModel):
    schema_version: Literal[1]
    scopes: list[Scope] = Field(min_length=1, max_length=3)
    scan_root: str = Field(min_length=1, max_length=1024)
    runner: Literal["codex_cli", "unknown"]
    model_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.:/-]{0,79}$")
    installed_skills: list[InstalledSkill] = Field(max_length=50)
    settings_files: list[SettingsFile] = Field(max_length=6)
    languages: list[Literal["python", "javascript", "typescript", "rust", "go"]] = Field(max_length=5)
    frameworks: list[Literal["fastapi", "django", "flask", "react", "nextjs", "vue", "express"]] = Field(max_length=7)
    excluded_paths: list[ExcludedPath] = Field(max_length=100)
    warnings: list[Literal["skill_limit"]] = Field(max_length=1)

    @field_validator("scopes")
    @classmethod
    def validate_scopes(cls, value):
        """요청과 같은 읽기 승인 규칙을 결과에도 적용한다."""
        return ScanInput.validate_scopes(value)

    @field_validator("scan_root")
    @classmethod
    def validate_scan_root(cls, value):
        """기기가 확인한 절대 경로만 메타데이터로 수락한다."""
        if (not (PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute())
            or any(ord(char) < 32 for char in value)):
            raise ValueError("invalid scan root")
        return value


class ScanFailure(StrictModel):
    error_code: Literal["ROOT_NOT_APPROVED", "ROOT_NOT_READABLE", "INVALID_SCAN_REQUEST", "SCAN_CANCELLED"]


class EnvironmentView(StrictModel):
    environment_id: str
    scan_job_id: str
    scan_root: str
    scopes: list[Scope]
    runner: Literal["codex_cli", "unknown"]
    runner_version: str
    model_id: str
    installed_skills: list[InstalledSkill]
    settings_files: list[SettingsFile]
    languages: list[str]
    frameworks: list[str]
    excluded_paths: list[ExcludedPath]
    warnings: list[str]
    settings_hash: str
    scanned_at: str
    verification: Literal["configuration_only"] = "configuration_only"


def store_environment(session, device, job, value, now):
    """완료 보고와 같은 트랜잭션에서 검증된 환경을 한 번만 저장한다."""
    try:
        result = ScanResult.model_validate(value)
        if result.scopes != job.payload["scopes"]:
            raise ValueError
        if "skills" not in result.scopes and result.installed_skills:
            raise ValueError
    except (ValidationError, ValueError, KeyError):
        raise HTTPException(422, detail={"code": "INVALID_SCAN_RESULT", "message": "환경 스캔 결과 형식을 확인해 주세요."}) from None
    data = result.model_dump()
    digest = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    environment_id = "env_" + hashlib.sha256(job.id.encode()).hexdigest()[:32]
    session.add(Environment(id=environment_id, profile_id=device.profile_id, device_id=device.id,
        scan_job_id=job.id, runner=result.runner, runner_version=device.codex_version or "unknown",
        model_id=result.model_id, installed_skills=data["installed_skills"], settings_hash=digest,
        scan_root=f"agent://{device.id}/{job.id}", scanned_at=now, metadata_json=data))


def environment_view(environment):
    """FE에는 승인 경로와 정제된 목록을 제공하며 서버 내부 경로와 구분한다."""
    data = environment.metadata_json
    return EnvironmentView(environment_id=environment.id, scan_job_id=environment.scan_job_id,
        scan_root=data["scan_root"], scopes=data["scopes"], runner=environment.runner,
        runner_version=environment.runner_version, model_id=environment.model_id,
        installed_skills=data["installed_skills"], settings_files=data["settings_files"],
        languages=data["languages"], frameworks=data["frameworks"], excluded_paths=data["excluded_paths"],
        warnings=data["warnings"], settings_hash=environment.settings_hash, scanned_at=environment.scanned_at)
