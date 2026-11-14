"""Approved local comparison inputs, stored outside the database."""
import json
import shutil
from pathlib import Path
from uuid import uuid4

from backend.runtime.comparison import create_input_snapshot


def register_input(root: Path, relative_paths: list[str], data_dir: Path, task_type: str, *, profile_id="local") -> dict:
    """사용자가 승인한 파일 묶음과 소유 프로필을 함께 저장한다."""
    if not relative_paths or len(relative_paths) > 3:
        raise ValueError("select between one and three files")
    snapshot = create_input_snapshot(root, [Path(path) for path in relative_paths])
    input_id = "input_" + uuid4().hex
    destination = data_dir / input_id
    try:
        destination.mkdir(parents=True, exist_ok=False)
        shutil.move(str(snapshot.directory), destination / "files")
        manifest = {"input_id": input_id, "profile_id": profile_id, "relative_paths": relative_paths, "task_type": task_type,
                    "input_sha256": snapshot.sha256, "scan_root": str(root.resolve())}
        (destination / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return manifest
    except Exception:
        shutil.rmtree(snapshot.directory, ignore_errors=True)
        shutil.rmtree(destination, ignore_errors=True)
        raise


def load_input(input_id: str, data_dir: Path, *, profile_id="local") -> dict:
    """식별자를 알아도 다른 프로필의 서버 입력을 읽거나 실행할 수 없게 한다."""
    if not input_id.startswith("input_") or not input_id[6:].isalnum():
        raise ValueError("invalid input ID")
    manifest = json.loads((data_dir / input_id / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("profile_id", "local") != profile_id:
        raise ValueError("input not owned by profile")
    return manifest
