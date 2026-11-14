"""Freeze explicitly selected project files for a later two-arm comparison."""
import asyncio
import hashlib
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath

from galtalka_agent.config import read_json, write_json
from galtalka_agent.failures import JobFailure

MAX_INPUT_BYTES = 64 * 1024
EXCLUDED = {".git", ".galtalka", ".env", ".ssh", ".aws", "node_modules", "__pycache__", "runtime"}


def input_path(root, input_id):
    if not isinstance(input_id, str) or not re.fullmatch(r"input_[a-f0-9]{32}", input_id):
        raise JobFailure("INVALID_INPUT_ID", "비교 입력 ID를 확인해 주세요.")
    base = root / ".galtalka" / "inputs"
    for folder in (root, base.parent, base):
        if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
            raise JobFailure("INVALID_INPUT_ROOT", "승인한 프로젝트 경로가 변경됐습니다.")
    target = base / input_id
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise JobFailure("INVALID_INPUT_ROOT", "비교 입력 보관 경로가 변경됐습니다.")
    return target


def checked_relative(value):
    if (not isinstance(value, str) or not value or len(value) > 256 or "\\" in value
        or ":" in value or any(ord(char) < 32 for char in value)):
        raise JobFailure("INVALID_INPUT_PATH", "선택한 파일 경로를 확인해 주세요.")
    path = PurePosixPath(value)
    if (path.is_absolute() or path.as_posix() != value or any(
        part in {"", ".", ".."} or part in EXCLUDED or part.startswith(".env")
        or part.lower().endswith((".pem", ".key")) or "secret" in part.lower()
        for part in path.parts) or PureWindowsPath(value).drive):
        raise JobFailure("INVALID_INPUT_PATH", "선택한 파일 경로를 확인해 주세요.")
    return path


def read_selected(root, relative, budget):
    path = root
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise JobFailure("INVALID_INPUT_PATH", "링크 파일은 비교 입력으로 사용할 수 없습니다.")
    try:
        before = path.stat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > budget:
            raise JobFailure("INPUT_TOO_LARGE", "비교 입력 파일이 크기 제한을 초과했습니다.")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        with os.fdopen(descriptor, "rb") as source:
            opened = os.fstat(source.fileno())
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise JobFailure("INPUT_CHANGED", "비교 입력 파일이 읽는 중 변경됐습니다.")
            content = source.read(budget + 1)
            after = os.fstat(source.fileno())
        if (len(content) > budget or (after.st_size, after.st_mtime_ns) !=
            (opened.st_size, opened.st_mtime_ns) or path.resolve(strict=True) != root / relative):
            raise JobFailure("INPUT_CHANGED", "비교 입력 파일이 읽는 중 변경됐습니다.")
        return content
    except (OSError, ValueError):
        raise JobFailure("INPUT_UNREADABLE", "선택한 파일을 읽을 수 없습니다.") from None


def read_snapshot(root, input_id):
    target = input_path(root, input_id)
    manifest = read_json(target / "manifest.json")
    if not isinstance(manifest, dict) or manifest.get("input_id") != input_id:
        raise JobFailure("INPUT_CHANGED", "저장된 비교 입력을 확인할 수 없습니다.")
    names = manifest.get("relative_paths")
    if (not isinstance(names, list) or not 1 <= len(names) <= 3
        or any(not isinstance(name, str) for name in names) or len(set(names)) != len(names)):
        raise JobFailure("INPUT_CHANGED", "저장된 비교 입력을 확인할 수 없습니다.")
    digest = hashlib.sha256()
    remaining = MAX_INPUT_BYTES
    for name in sorted(names):
        relative = checked_relative(name)
        content = read_selected(target / "files", relative, remaining)
        remaining -= len(content)
        digest.update(name.encode() + b"\0" + content)
    if digest.hexdigest() != manifest.get("input_sha256"):
        raise JobFailure("INPUT_CHANGED", "저장된 비교 입력이 변경됐습니다.")
    return manifest


def freeze(payload, project_root):
    if (not isinstance(payload, dict) or payload.get("schema_version") != 1
        or payload.get("task_type") != "code_review" or payload.get("confirmed_upload_scope") is not True
        or not isinstance(payload.get("relative_paths"), list)
        or not 1 <= len(payload["relative_paths"]) <= 3):
        raise JobFailure("INVALID_INPUT_REQUEST", "비교 입력 요청을 확인해 주세요.")
    root = Path(project_root).resolve(strict=True)
    target = input_path(root, payload.get("input_id"))
    names = [checked_relative(value).as_posix() for value in payload["relative_paths"]]
    if len(set(names)) != len(names):
        raise JobFailure("INVALID_INPUT_REQUEST", "중복된 비교 입력 파일이 있습니다.")
    if target.exists():
        manifest = read_snapshot(root, payload["input_id"])
        if manifest["relative_paths"] != names or manifest["task_type"] != payload["task_type"]:
            raise JobFailure("INPUT_ID_CONFLICT", "다른 작업에 사용한 입력 ID입니다.")
        return manifest
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".input-stage-", dir=target.parent))
    digest = hashlib.sha256()
    remaining = MAX_INPUT_BYTES
    try:
        for name in sorted(names):
            relative = PurePosixPath(name)
            content = read_selected(root, relative, remaining)
            remaining -= len(content)
            digest.update(name.encode() + b"\0" + content)
            destination = stage / "files" / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        manifest = {"input_id": payload["input_id"], "task_type": payload["task_type"],
            "relative_paths": names, "input_sha256": digest.hexdigest()}
        write_json(stage / "manifest.json", manifest)
        if target.exists():
            raise JobFailure("INPUT_ID_CONFLICT", "비교 입력 ID가 이미 존재합니다.")
        os.replace(stage, target)
        return manifest
    finally:
        if stage.exists():
            shutil.rmtree(stage)


async def snapshot_inputs_job(payload, project_root):
    return await asyncio.to_thread(freeze, payload, project_root)
