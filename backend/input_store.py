"""Approved local comparison inputs, stored outside the database."""
import json
import shutil
from pathlib import Path
from uuid import uuid4

from backend.runtime.comparison import create_input_snapshot


def register_input(root: Path, relative_paths: list[str], data_dir: Path, task_type: str) -> dict:
    if not relative_paths or len(relative_paths) > 3:
        raise ValueError("select between one and three files")
    snapshot = create_input_snapshot(root, [Path(path) for path in relative_paths])
    input_id = "input_" + uuid4().hex
    destination = data_dir / input_id
    try:
        destination.mkdir(parents=True, exist_ok=False)
        shutil.move(str(snapshot.directory), destination / "files")
        manifest = {"input_id": input_id, "relative_paths": relative_paths, "task_type": task_type,
                    "input_sha256": snapshot.sha256, "scan_root": str(root.resolve())}
        (destination / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return manifest
    except Exception:
        shutil.rmtree(snapshot.directory, ignore_errors=True)
        shutil.rmtree(destination, ignore_errors=True)
        raise


def load_input(input_id: str, data_dir: Path) -> dict:
    if not input_id.startswith("input_") or not input_id[6:].isalnum():
        raise ValueError("invalid input ID")
    return json.loads((data_dir / input_id / "manifest.json").read_text(encoding="utf-8"))
