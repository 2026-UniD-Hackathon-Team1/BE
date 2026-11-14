import hashlib
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from backend.runtime.comparison import create_input_snapshot, execute_comparison
from backend.runtime.codex_runner import run_codex_once


def test_snapshot_hash_is_same_for_same_selected_files(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "b.py").write_text("b=2\n", encoding="utf-8")
    (root / "a.py").write_text("a=1\n", encoding="utf-8")

    snapshot = create_input_snapshot(root, [Path("b.py"), Path("a.py")])

    expected = hashlib.sha256(b"a.py\0a=1\nb.py\0b=2\n").hexdigest()
    assert snapshot.sha256 == expected
    assert sorted(path.name for path in snapshot.directory.iterdir()) == ["a.py", "b.py"]


@pytest.mark.parametrize("selected", [Path("../outside.py"), Path("link.py")])
def test_snapshot_rejects_paths_outside_root_and_symlinks(tmp_path, selected):
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("secret", encoding="utf-8")
    (root / "link.py").symlink_to(outside)

    with pytest.raises(ValueError):
        create_input_snapshot(root, [selected])


def test_runner_records_output_and_timeout(tmp_path):
    script = tmp_path / "fake-codex"
    script.write_text(
        "#!/usr/bin/env python3\nimport sys,time\n"
        "print('result')\nprint('event', file=sys.stderr)\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    stdout = tmp_path / "stdout.jsonl"
    result = run_codex_once(
        [str(script)], cwd=tmp_path, prompt="review", stdout_path=stdout, timeout=3
    )
    assert result.status == "completed"
    assert result.exit_code == 0
    assert stdout.read_text(encoding="utf-8") == "result\n"
    assert result.stderr == "event\n"


def test_execute_comparison_keeps_arms_separate_and_rejects_changed_candidate(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "app.py").write_text("print('safe')\n", encoding="utf-8")
    bundle = tmp_path / "candidate"
    bundle.mkdir()
    (bundle / "SKILL.md").write_text("candidate skill\n", encoding="utf-8")
    encoded = json.dumps({"files": {"SKILL.md": "Y2FuZGlkYXRlIHNraWxsCg=="}}, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    executable = tmp_path / "fake-codex"
    executable.write_text(
        "#!/usr/bin/env python3\nimport os,sys,json\n"
        "assert '--model' in sys.argv and sys.argv[sys.argv.index('--model')+1] == 'fixed-model'\n"
        "arm = 'candidate' if os.path.exists('.agents/skills') else 'baseline'\n"
        "print(json.dumps({'type':'item.completed','item':{'command':'read .agents/skills/candidate/SKILL.md' if arm == 'candidate' else 'read app.py'}}))\n"
        "open(sys.argv[sys.argv.index('-o')+1], 'w').write(json.dumps({'summary':arm,'findings':[]}))\n",
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)

    result = execute_comparison(
        root=root,
        selected_paths=[Path("app.py")],
        candidate_bundle=bundle,
        approved_bundle_sha256=digest,
        model_id="fixed-model",
        runner_version="codex 0.test",
        output_root=tmp_path / "runs",
        codex_command=[str(executable)],
        timeout=3,
        method_prompt="Check input validation",
    )
    assert result.baseline.final_result["summary"] == "baseline"
    assert result.candidate.final_result["summary"] == "candidate"
    assert result.candidate.skill_usage_status == "confirmed"
    assert result.baseline.input_sha256 == result.candidate.input_sha256
    assert result.baseline.output_path != result.candidate.output_path

    with pytest.raises(ValueError, match="comparison input changed"):
        execute_comparison(
            root=root,
            selected_paths=[Path("app.py")],
            candidate_bundle=bundle,
            approved_bundle_sha256=digest,
            model_id="fixed-model",
            runner_version="codex 0.test",
            output_root=tmp_path / "changed-input",
            codex_command=[str(executable)],
            timeout=3,
            method_prompt="Check input validation",
            expected_input_sha256="0" * 64,
        )

    with pytest.raises(ValueError, match="approved candidate hash"):
        execute_comparison(
            root=root,
            selected_paths=[Path("app.py")],
            candidate_bundle=bundle,
            approved_bundle_sha256="0" * 64,
            model_id="fixed-model",
            runner_version="codex 0.test",
            output_root=tmp_path / "rejected",
            codex_command=[str(executable)],
            timeout=3,
            method_prompt="Check input validation",
        )


def test_candidate_symlink_is_rejected_before_copy(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "app.py").write_text("pass\n")
    bundle = tmp_path / "skill"
    bundle.mkdir()
    (bundle / "SKILL.md").write_text("skill")
    (bundle / "secret").symlink_to(root / "app.py")
    with pytest.raises(ValueError, match="symlink"):
        execute_comparison(root=root, selected_paths=[Path("app.py")], candidate_bundle=bundle,
            approved_bundle_sha256="0" * 64, model_id="fixed", runner_version="test",
            output_root=tmp_path / "runs", codex_command=["codex"], method_prompt="Review")


def test_runner_marks_timeout_without_reusing_prior_output(tmp_path):
    script = tmp_path / "slow"
    script.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(2)\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    output = tmp_path / "events.jsonl"
    output.write_text("old", encoding="utf-8")
    result = run_codex_once([str(script)], cwd=tmp_path, prompt="x", stdout_path=output, timeout=1)
    assert result.status == "timeout"
    assert not output.exists()
