import asyncio
import json
import os
import threading
from types import SimpleNamespace

import pytest

from galtalka_agent.failures import JobFailure
from galtalka_agent.scan import ScanReader, collect_scan, scan


def request(scopes=None, root="."):
    """서버가 생성하는 승인 범위 스캔 요청을 준비한다."""
    return {"schema_version": 1, "root": root, "scopes": scopes or ["config", "skills"]}


def test_scan_returns_metadata_without_secrets_or_source(tmp_path):
    """고정 manifest와 설정 및 스킬만 읽고 비밀값과 원문 코드를 반환하지 않는다."""
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex/config.toml").write_text('model = "gpt-example"\napi_key = "SECRET_VALUE"\n', encoding="utf-8")
    (tmp_path / ".codex/auth.json").write_text('{"key":"AUTH_SECRET"}', encoding="utf-8")
    (tmp_path / "package.json").write_text(json.dumps({"name": "private-name", "scripts": {"test": "DO_NOT_EXECUTE"},
        "devDependencies": {"typescript": "secret-url", "react": "secret-url"}}), encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("fastapi==1\nhttps://private.example/SECRET\n", encoding="utf-8")
    skill = tmp_path / ".agents/skills/review"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("PRIVATE_SKILL_BODY", encoding="utf-8")
    (tmp_path / ".env").write_text("ENV_SECRET", encoding="utf-8")
    (tmp_path / "app.py").write_text("PRIVATE_SOURCE_CODE", encoding="utf-8")
    value = asyncio.run(scan(request(), tmp_path.resolve()))
    encoded = json.dumps(value)
    assert value["languages"] == ["python", "typescript"]
    assert value["frameworks"] == ["fastapi", "react"]
    assert value["model_id"] == "gpt-example"
    assert value["runner"] == "codex_cli"
    assert value["installed_skills"][0]["name"] == "review"
    assert len(value["installed_skills"][0]["manifest_sha256"]) == 64
    for secret in ("SECRET", "private-name", "DO_NOT_EXECUTE", "PRIVATE_SKILL_BODY", "PRIVATE_SOURCE_CODE"):
        assert secret not in encoded


def test_config_only_does_not_read_skills(tmp_path, monkeypatch):
    """skills 범위를 승인하지 않으면 스킬 파일을 열지 않는다."""
    paths = []
    original = os.open
    def track(path, flags, *args, **kwargs):
        """열린 경로만 기록하고 실제 임시 파일 읽기를 유지한다."""
        paths.append(str(path))
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", track)
    skill = tmp_path / ".agents/skills/review"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_bytes(b"private")
    result = collect_scan(request(["config"]), tmp_path.resolve(), threading.Event())
    assert result["installed_skills"] == []
    assert all("SKILL.md" not in path for path in paths)


def test_root_mismatch_is_rejected_before_reading(tmp_path, monkeypatch):
    """다른 경로를 요청하면 어떤 파일도 열기 전에 거부한다."""
    def forbidden(*args, **kwargs):
        """승인 경로 불일치 뒤 읽기 호출을 금지한다."""
        pytest.fail("unapproved root must not be opened")
    monkeypatch.setattr(os, "open", forbidden)
    with pytest.raises(JobFailure) as error:
        collect_scan(request(root=str(tmp_path.parent)), tmp_path.resolve(), threading.Event())
    assert error.value.code == "ROOT_NOT_APPROVED"


def test_reparse_point_is_not_followed(tmp_path, monkeypatch):
    """Windows junction에 해당하는 reparse 속성을 만나면 하위 파일을 읽지 않는다."""
    target = tmp_path / ".agents"
    target.mkdir()
    original = type(target).lstat
    def lstat(path):
        """임시 디렉터리를 Windows reparse point로 모의한다."""
        info = original(path)
        return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400) if path == target else info
    monkeypatch.setattr(type(target), "lstat", lstat)
    result = collect_scan(request(), tmp_path.resolve(), threading.Event())
    assert result["installed_skills"] == []
    assert any(item["reason"] == "링크 경로는 읽지 않습니다." for item in result["excluded_paths"])


def test_file_size_parse_errors_and_token_like_model_are_safe(tmp_path):
    """큰 파일과 잘못된 형식은 상태로 표시하고 토큰 형태의 모델 값은 숨긴다."""
    (tmp_path / "package.json").write_text("x" * (64 * 1024 + 1), encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[broken", encoding="utf-8")
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex/config.toml").write_text('model="sk-PRIVATE_TOKEN"', encoding="utf-8")
    value = collect_scan(request(), tmp_path.resolve(), threading.Event())
    assert value["model_id"] == "unknown"
    assert "PRIVATE_TOKEN" not in json.dumps(value)
    assert next(item for item in value["settings_files"] if item["path"] == "package.json")["excluded"]
    assert not next(item for item in value["settings_files"] if item["path"] == "pyproject.toml")["readable"]


def test_skill_count_is_bounded(tmp_path):
    """스킬 폴더가 많아도 제한된 개수만 검사한다."""
    for index in range(52):
        folder = tmp_path / f".agents/skills/skill-{index}"
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_bytes(b"example")
    value = collect_scan(request(), tmp_path.resolve(), threading.Event())
    assert len(value["installed_skills"]) == 50
    assert value["warnings"] == ["skill_limit"]


def test_changed_file_identity_is_rejected_before_read(tmp_path, monkeypatch):
    """열기 전후 파일 식별자가 다르면 내용을 읽거나 반환하지 않는다."""
    (tmp_path / "requirements.txt").write_text("fastapi", encoding="utf-8")
    target = (tmp_path / "requirements.txt").stat()
    original = os.fstat
    def changed(descriptor):
        """열린 파일의 inode가 바뀐 상황을 모의한다."""
        info = original(descriptor)
        if (info.st_dev, info.st_ino) == (target.st_dev, target.st_ino):
            return SimpleNamespace(st_dev=info.st_dev, st_ino=info.st_ino + 1, st_mode=info.st_mode)
        return info
    monkeypatch.setattr(os, "fstat", changed)
    reader = ScanReader(tmp_path.resolve(), threading.Event())
    assert reader.read("requirements.txt") is None
    assert reader.excluded[0]["reason"] == "스캔 중 파일이 바뀌었습니다."


def test_cancelled_scan_stops_before_next_file(tmp_path):
    """취소된 스캔은 다음 파일 검사부터 중단한다."""
    stop = threading.Event()
    stop.set()
    with pytest.raises(JobFailure) as error:
        collect_scan(request(), tmp_path.resolve(), stop)
    assert error.value.code == "SCAN_CANCELLED"
