import hashlib
import json
import sys
from pathlib import Path

import pytest

from backend.runtime.comparison import create_input_snapshot, execute_comparison
from backend.runtime.codex_runner import run_codex_once


@pytest.fixture
def link_file(monkeypatch):
    """실제 링크를 만들고 Windows 권한 제한 시 링크 감지 결과만 대체한다."""
    def create_link(path, target):
        """링크 생성 권한 오류만 대체하고 다른 파일 시스템 오류는 드러낸다."""
        try:
            path.symlink_to(target)
        except OSError as error:
            if getattr(error, "winerror", None) != 1314:
                raise
            path.touch()
            original = Path.is_symlink
            monkeypatch.setattr(Path, "is_symlink", lambda item: item == path or original(item))
    return create_link


def test_snapshot_hash_is_same_for_same_selected_files(tmp_path):
    """운영체제와 입력 순서에 관계없이 동일 바이트의 해시를 확인한다."""
    root = tmp_path / "project"
    root.mkdir()
    (root / "b.py").write_bytes(b"b=2\n")
    (root / "a.py").write_bytes(b"a=1\n")

    snapshot = create_input_snapshot(root, [Path("b.py"), Path("a.py")])

    expected = hashlib.sha256(b"a.py\0a=1\nb.py\0b=2\n").hexdigest()
    assert snapshot.sha256 == expected
    assert sorted(path.name for path in snapshot.directory.iterdir()) == ["a.py", "b.py"]


@pytest.mark.parametrize("selected", [Path("../outside.py"), Path("link.py")])
def test_snapshot_rejects_paths_outside_root_and_symlinks(tmp_path, selected, link_file):
    """루트 이탈과 링크 입력을 각각 거부하는지 확인한다."""
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("secret", encoding="utf-8")
    if selected == Path("link.py"):
        link_file(root / "link.py", outside)

    with pytest.raises(ValueError):
        create_input_snapshot(root, [selected])


def test_runner_records_output_and_timeout(tmp_path):
    """현재 Python으로 실행한 모의 프로세스의 출력과 종료 코드를 확인한다."""
    script = tmp_path / "fake-codex"
    script.write_text(
        "#!/usr/bin/env python3\nimport sys,time\n"
        "print('result')\nprint('event', file=sys.stderr)\n",
        encoding="utf-8",
    )
    stdout = tmp_path / "stdout.jsonl"
    result = run_codex_once(
        [sys.executable, str(script)], cwd=tmp_path, prompt="review", stdout_path=stdout, timeout=3
    )
    assert result.status == "completed"
    assert result.exit_code == 0
    assert stdout.read_text(encoding="utf-8") == "result\n"
    assert result.stderr == "event\n"


def test_execute_comparison_keeps_arms_separate_and_rejects_changed_candidate(tmp_path):
    """비교 실행 공간 분리와 후보 및 입력 해시 변경 거부를 확인한다."""
    root = tmp_path / "project"
    root.mkdir()
    (root / "app.py").write_text("print('safe')\n", encoding="utf-8")
    bundle = tmp_path / "candidate"
    bundle.mkdir()
    (bundle / "SKILL.md").write_bytes(b"candidate skill\n")
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

    result = execute_comparison(
        root=root,
        selected_paths=[Path("app.py")],
        candidate_bundle=bundle,
        approved_bundle_sha256=digest,
        model_id="fixed-model",
        runner_version="codex 0.test",
        output_root=tmp_path / "runs",
        codex_command=[sys.executable, str(executable)],
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
            codex_command=[sys.executable, str(executable)],
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
            codex_command=[sys.executable, str(executable)],
            timeout=3,
            method_prompt="Check input validation",
        )


def test_candidate_symlink_is_rejected_before_copy(tmp_path, link_file):
    """후보 번들의 링크를 복사나 실행 전에 거부하는지 확인한다."""
    root = tmp_path / "project"
    root.mkdir()
    (root / "app.py").write_text("pass\n")
    bundle = tmp_path / "skill"
    bundle.mkdir()
    (bundle / "SKILL.md").write_text("skill")
    link_file(bundle / "secret", root / "app.py")
    with pytest.raises(ValueError, match="symlink"):
        execute_comparison(root=root, selected_paths=[Path("app.py")], candidate_bundle=bundle,
            approved_bundle_sha256="0" * 64, model_id="fixed", runner_version="test",
            output_root=tmp_path / "runs", codex_command=["codex"], method_prompt="Review")


def test_runner_marks_timeout_without_reusing_prior_output(tmp_path):
    """시간 초과 후 이전 출력이 결과로 재사용되지 않는지 확인한다."""
    script = tmp_path / "slow"
    script.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(2)\n")
    output = tmp_path / "events.jsonl"
    output.write_text("old", encoding="utf-8")
    result = run_codex_once([sys.executable, str(script)], cwd=tmp_path, prompt="x", stdout_path=output, timeout=1)
    assert result.status == "timeout"
    assert not output.exists()
