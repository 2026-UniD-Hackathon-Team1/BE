"""Back up and replace one skill inside the user-approved project."""
import base64
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath

from galtalka_agent.config import AgentError, read_json, write_json


def digest(files):
    encoded = {name: base64.b64encode(content).decode("ascii") for name, content in files.items()}
    raw = json.dumps({"files": encoded}, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def folder_files(folder):
    if folder.is_symlink() or not folder.is_dir():
        raise AgentError("관리 스킬 폴더를 확인해 주세요.")
    files = {}
    for item in folder.rglob("*"):
        if item.is_symlink() or not (item.is_file() or item.is_dir()):
            raise AgentError("관리 스킬에 지원하지 않는 파일이 있습니다.")
        if item.is_file():
            files[item.relative_to(folder).as_posix()] = item.read_bytes()
    return files


def current_hash(target):
    if target.is_symlink():
        raise AgentError("관리 스킬 경로가 링크로 바뀌었습니다.")
    return digest(folder_files(target)) if target.exists() else None


def target_path(project_root, name):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        raise AgentError("스킬 이름을 확인해 주세요.")
    root = Path(project_root).resolve(strict=True)
    if not root.is_dir():
        raise AgentError("승인한 프로젝트 폴더를 확인해 주세요.")
    target = root / ".agents" / "skills" / name
    for path in (root / ".agents", target.parent, target):
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise AgentError("관리 스킬 경로가 변경됐습니다.")
    return root, target


def candidate_files(payload):
    encoded = payload.get("files")
    expected = payload.get("candidate_sha256")
    if not isinstance(encoded, dict) or "SKILL.md" not in encoded or not isinstance(expected, str):
        raise AgentError("승인된 스킬 파일을 확인해 주세요.")
    if not re.fullmatch(r"[a-f0-9]{64}", expected):
        raise AgentError("후보 스킬 해시를 확인해 주세요.")
    files = {}
    for name, value in encoded.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise AgentError("후보 스킬 파일 형식을 확인해 주세요.")
        path = PurePosixPath(name)
        if (path.is_absolute() or path.as_posix() != name or ".." in path.parts
            or "\\" in name or PureWindowsPath(name).drive or ":" in name):
            raise AgentError("후보 스킬 파일 경로를 확인해 주세요.")
        try:
            content = base64.b64decode(value, validate=True)
        except ValueError:
            raise AgentError("후보 스킬 파일 인코딩을 확인해 주세요.") from None
        if base64.b64encode(content).decode("ascii") != value:
            raise AgentError("후보 스킬 파일 인코딩을 확인해 주세요.")
        files[name] = content
    if digest(files) != expected:
        raise AgentError("승인된 후보 스킬 해시가 일치하지 않습니다.")
    return files, expected


def backup_path(root, apply_id):
    if not isinstance(apply_id, str) or not re.fullmatch(r"apply_[a-f0-9]{32}", apply_id):
        raise AgentError("적용 작업 ID를 확인해 주세요.")
    base = root / ".galtalka" / "backups"
    for path in (base.parent, base):
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise AgentError("복원 백업 경로를 확인해 주세요.")
    backup = base / apply_id
    if backup.is_symlink() or (backup.exists() and not backup.is_dir()):
        raise AgentError("복원 백업 경로를 확인해 주세요.")
    return backup


def changes(old, new):
    result = []
    for name in sorted(old.keys() | new.keys()):
        action = ("add" if name not in old else "delete" if name not in new
                  else "replace" if old[name] != new[name] else None)
        if action:
            result.append({"path": name, "action": action})
    return result


def write_files(folder, files):
    for name, content in files.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def install(payload, root, target, files, candidate_hash):
    apply_id = payload.get("apply_id")
    backup = backup_path(root, apply_id)
    expected = payload.get("expected_current_sha256")
    if expected is not None and (not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected)):
        raise AgentError("미리보기 해시를 확인해 주세요.")
    target.parent.mkdir(parents=True, exist_ok=True)
    target_path(root, target.name)
    if current_hash(target) != expected:
        raise AgentError("미리보기 이후 스킬 파일이 변경됐습니다.")
    if backup.exists() or backup.is_symlink():
        raise AgentError("이미 사용한 적용 작업 ID입니다.")
    backup.mkdir(parents=True, mode=0o700)
    before = backup / "before"
    stage = None
    displaced = target.parent / f".{target.name}.previous-{apply_id}"
    try:
        if expected is not None:
            shutil.copytree(target, before, symlinks=True)
            if digest(folder_files(before)) != expected:
                raise AgentError("백업 중 기존 스킬이 변경됐습니다.")
        stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.stage-", dir=target.parent))
        write_files(stage, files)
        if digest(folder_files(stage)) != candidate_hash or current_hash(target) != expected:
            raise AgentError("적용 직전 스킬 파일이 변경됐습니다.")
        write_json(backup / "state.json", {"skill_name": target.name, "before_sha256": expected,
            "applied_sha256": candidate_hash, "status": "prepared"})
        if displaced.exists() or displaced.is_symlink():
            raise AgentError("이전 교체 기록이 남아 있습니다.")
        if target.exists():
            os.replace(target, displaced)
        try:
            if expected is not None and current_hash(displaced) != expected:
                raise AgentError("교체 중 기존 스킬이 변경됐습니다.")
            os.replace(stage, target)
            stage = None
            if current_hash(target) != candidate_hash:
                raise AgentError("설치한 스킬 해시가 일치하지 않습니다.")
            write_json(backup / "state.json", {"skill_name": target.name, "before_sha256": expected,
                "applied_sha256": candidate_hash, "status": "applied"})
        except Exception:
            if target.exists():
                if current_hash(target) != candidate_hash:
                    raise AgentError("교체 중 변경된 스킬을 자동으로 덮어쓸 수 없습니다.") from None
                shutil.rmtree(target)
            if displaced.exists():
                os.replace(displaced, target)
            raise
        if displaced.exists():
            shutil.rmtree(displaced)
    finally:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)
    return {"status": "applied", "apply_id": apply_id, "before_sha256": expected,
        "applied_sha256": candidate_hash, "backup_relative_path": (backup / "before").relative_to(root).as_posix()}


def recover_interrupted(project_root):
    """중단된 교체로 대상 폴더가 사라졌을 때 검증된 원본만 되돌린다."""
    root = Path(project_root).resolve(strict=True)
    base = root / ".galtalka" / "backups"
    if not base.exists():
        return
    if base.is_symlink() or not base.is_dir():
        raise AgentError("복원 백업 경로를 확인해 주세요.")
    for backup in base.iterdir():
        if backup.is_symlink() or not backup.is_dir():
            raise AgentError("복원 백업 경로를 확인해 주세요.")
        state_file = backup / "state.json"
        if not state_file.exists():
            continue
        state = read_json(state_file)
        if not isinstance(state, dict) or state.get("status") not in {"prepared", "rollback_prepared"}:
            continue
        _, target = target_path(root, state.get("skill_name"))
        rolling_back = state["status"] == "rollback_prepared"
        displaced = target.parent / f".{target.name}.{'rollback' if rolling_back else 'previous'}-{backup.name}"
        before_hash = state.get("applied_sha256") if rolling_back else state.get("before_sha256")
        restored_status = "applied" if rolling_back else "recovered"
        if not displaced.exists() and not displaced.is_symlink() and current_hash(target) == before_hash:
            write_json(state_file, {**state, "status": restored_status})
            continue
        if (target.exists() or target.is_symlink() or before_hash is None
            or displaced.is_symlink() or not displaced.is_dir()
            or current_hash(displaced) != before_hash):
            raise AgentError("중단된 스킬 교체를 자동 복구할 수 없습니다. 변경 상태를 확인해 주세요.")
        os.replace(displaced, target)
        write_json(state_file, {**state, "status": restored_status})


async def apply_job(payload, project_root):
    if not isinstance(payload, dict) or payload.get("phase") not in {"preview", "install"}:
        raise AgentError("적용 작업 형식을 확인해 주세요.")
    root, target = target_path(project_root, payload.get("skill_name"))
    files, candidate_hash = candidate_files(payload)
    if payload["phase"] == "install":
        return install(payload, root, target, files, candidate_hash)
    old = folder_files(target) if target.exists() else {}
    return {"status": "preview", "target_relative_path": target.relative_to(root).as_posix(),
        "current_state_sha256": digest(old) if target.exists() else None,
        "candidate_sha256": candidate_hash, "changes": changes(old, files)}


async def rollback_job(payload, project_root):
    if not isinstance(payload, dict):
        raise AgentError("복원 작업 형식을 확인해 주세요.")
    root, target = target_path(project_root, payload.get("skill_name"))
    backup = backup_path(root, payload.get("apply_id"))
    state = read_json(backup / "state.json")
    if (state.get("status") != "applied" or state.get("skill_name") != target.name
        or state.get("before_sha256") != payload.get("before_sha256")
        or state.get("applied_sha256") != payload.get("applied_sha256")):
        raise AgentError("복원 백업과 적용 기록이 일치하지 않습니다.")
    before_hash, applied_hash = state["before_sha256"], state["applied_sha256"]
    if current_hash(target) != applied_hash:
        raise AgentError("적용 이후 스킬 파일이 변경돼 자동 복원할 수 없습니다.")
    before = backup / "before"
    if before_hash is not None and digest(folder_files(before)) != before_hash:
        raise AgentError("복원 백업이 변경됐습니다.")
    stage = None
    displaced = target.parent / f".{target.name}.rollback-{payload['apply_id']}"
    try:
        if before_hash is not None:
            stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.restore-", dir=target.parent))
            shutil.copytree(before, stage, dirs_exist_ok=True, symlinks=True)
            if digest(folder_files(stage)) != before_hash:
                raise AgentError("복원 준비 중 백업이 변경됐습니다.")
        if displaced.exists() or displaced.is_symlink():
            raise AgentError("이전 복원 기록이 남아 있습니다.")
        write_json(backup / "state.json", {**state, "status": "rollback_prepared"})
        os.replace(target, displaced)
        try:
            if current_hash(displaced) != applied_hash:
                raise AgentError("복원 중 적용 스킬이 변경됐습니다.")
            if stage is not None:
                os.replace(stage, target)
                stage = None
            if current_hash(target) != before_hash:
                raise AgentError("복원한 스킬 해시가 일치하지 않습니다.")
            write_json(backup / "state.json", {**state, "status": "rolled_back"})
        except Exception:
            if target.exists():
                if current_hash(target) != before_hash:
                    raise AgentError("복원 중 변경된 스킬을 자동으로 덮어쓸 수 없습니다.") from None
                shutil.rmtree(target)
            os.replace(displaced, target)
            write_json(backup / "state.json", state)
            raise
        shutil.rmtree(displaced)
    finally:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)
    return {"status": "rolled_back", "apply_id": payload["apply_id"]}
