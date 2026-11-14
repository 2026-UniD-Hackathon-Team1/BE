import asyncio
import hashlib

import pytest

from galtalka_agent.comparison_inputs import snapshot_inputs_job
from galtalka_agent.failures import JobFailure


def request(input_id="input_" + "a" * 32, paths=None):
    return {"schema_version": 1, "input_id": input_id, "task_type": "code_review",
            "relative_paths": paths or ["src/app.py"], "confirmed_upload_scope": True}


def test_snapshot_freezes_selected_files_and_is_idempotent(tmp_path):
    root = tmp_path / "approved"
    source = root / "src" / "app.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"old source\n")
    first = asyncio.run(snapshot_inputs_job(request(), root))
    expected = hashlib.sha256(b"src/app.py\0old source\n").hexdigest()
    assert first == {"input_id": "input_" + "a" * 32, "task_type": "code_review",
                     "relative_paths": ["src/app.py"], "input_sha256": expected}
    source.write_bytes(b"new source\n")
    assert asyncio.run(snapshot_inputs_job(request(), root)) == first
    stored = root / ".galtalka" / "inputs" / first["input_id"] / "files" / "src" / "app.py"
    assert stored.read_bytes() == b"old source\n"


@pytest.mark.parametrize("paths", [["../outside.py"], [".env"], [".git/config"], ["src/link/app.py"]])
def test_snapshot_rejects_escape_secrets_and_symlink_parent(tmp_path, paths):
    root = tmp_path / "approved"
    (root / "src").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "app.py").write_text("pass\n")
    (root / "src" / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(JobFailure):
        asyncio.run(snapshot_inputs_job(request(paths=paths), root))
    assert not (root / ".galtalka" / "inputs" / request()["input_id"]).exists()


def test_snapshot_rejects_large_file(tmp_path):
    root = tmp_path / "approved"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_bytes(b"x" * (64 * 1024 + 1))
    with pytest.raises(JobFailure):
        asyncio.run(snapshot_inputs_job(request(), root))


def test_snapshot_hash_is_stable_for_unsorted_file_selection(tmp_path):
    root = tmp_path / "approved"
    root.mkdir()
    (root / "z.py").write_bytes(b"z\n")
    (root / "a.py").write_bytes(b"a\n")
    value = request(paths=["z.py", "a.py"])
    first = asyncio.run(snapshot_inputs_job(value, root))
    assert first["relative_paths"] == ["z.py", "a.py"]
    assert asyncio.run(snapshot_inputs_job(value, root)) == first
