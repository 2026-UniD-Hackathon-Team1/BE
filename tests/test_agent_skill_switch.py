"""PC 연결 프로그램이 승인된 스킬 하나를 백업·교체·복원하는 계약."""
import asyncio
import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from galtalka_agent.cli import main
from galtalka_agent.config import AgentConfig, AgentError, save_config
from galtalka_agent.skill_switch import apply_job, rollback_job
import galtalka_agent.skill_switch as skill_switch


def candidate():
    files = {"SKILL.md": base64.b64encode(b"new skill\n").decode()}
    digest = hashlib.sha256(json.dumps({"files": files}, sort_keys=True,
        ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
    return files, digest


def test_agent_previews_installs_and_restores_skill_bytes(tmp_path):
    target = tmp_path / ".agents" / "skills" / "review-skill"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_bytes(b"old skill\n")
    files, digest = candidate()
    common = {"skill_name": "review-skill", "files": files, "candidate_sha256": digest}
    preview = asyncio.run(apply_job({**common, "phase": "preview"}, tmp_path))
    assert preview["changes"] == [{"path": "SKILL.md", "action": "replace"}]
    assert (target / "SKILL.md").read_bytes() == b"old skill\n"

    apply_id = "apply_" + "a" * 32
    applied = asyncio.run(apply_job({**common, "phase": "install", "apply_id": apply_id,
        "expected_current_sha256": preview["current_state_sha256"]}, tmp_path))
    assert applied["status"] == "applied"
    assert applied["applied_sha256"] == digest
    assert (target / "SKILL.md").read_bytes() == b"new skill\n"
    backup = tmp_path / applied["backup_relative_path"] / "SKILL.md"
    assert backup.read_bytes() == b"old skill\n"

    restored = asyncio.run(rollback_job({"apply_id": apply_id, "skill_name": "review-skill",
        "applied_sha256": digest, "before_sha256": preview["current_state_sha256"]}, tmp_path))
    assert restored["status"] == "rolled_back"
    assert (target / "SKILL.md").read_bytes() == b"old skill\n"


def test_agent_rejects_changed_skill_before_install_and_before_rollback(tmp_path):
    target = tmp_path / ".agents" / "skills" / "review-skill"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_bytes(b"old skill\n")
    files, digest = candidate()
    common = {"skill_name": "review-skill", "files": files, "candidate_sha256": digest}
    preview = asyncio.run(apply_job({**common, "phase": "preview"}, tmp_path))
    (target / "SKILL.md").write_bytes(b"user edit\n")
    with pytest.raises(AgentError):
        asyncio.run(apply_job({**common, "phase": "install", "apply_id": "apply_" + "b" * 32,
            "expected_current_sha256": preview["current_state_sha256"]}, tmp_path))
    assert (target / "SKILL.md").read_bytes() == b"user edit\n"

    current = asyncio.run(apply_job({**common, "phase": "preview"}, tmp_path))
    apply_id = "apply_" + "c" * 32
    asyncio.run(apply_job({**common, "phase": "install", "apply_id": apply_id,
        "expected_current_sha256": current["current_state_sha256"]}, tmp_path))
    (target / "SKILL.md").write_bytes(b"later user edit\n")
    with pytest.raises(AgentError):
        asyncio.run(rollback_job({"apply_id": apply_id, "skill_name": "review-skill",
            "applied_sha256": digest, "before_sha256": current["current_state_sha256"]}, tmp_path))
    assert (target / "SKILL.md").read_bytes() == b"later user edit\n"


def test_agent_rollback_removes_skill_that_was_absent(tmp_path):
    files, digest = candidate()
    common = {"skill_name": "review-skill", "files": files, "candidate_sha256": digest}
    preview = asyncio.run(apply_job({**common, "phase": "preview"}, tmp_path))
    assert preview["current_state_sha256"] is None
    apply_id = "apply_" + "d" * 32
    asyncio.run(apply_job({**common, "phase": "install", "apply_id": apply_id,
        "expected_current_sha256": None}, tmp_path))
    assert (tmp_path / ".agents" / "skills" / "review-skill").exists()
    asyncio.run(rollback_job({"apply_id": apply_id, "skill_name": "review-skill",
        "applied_sha256": digest, "before_sha256": None}, tmp_path))
    assert not (tmp_path / ".agents" / "skills" / "review-skill").exists()


def test_agent_rejects_symlink_target_and_unapproved_bundle(tmp_path):
    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)
    (outside / "SKILL.md").write_bytes(b"outside\n")
    target = tmp_path / ".agents" / "skills" / "review-skill"
    target.parent.mkdir(parents=True)
    target.symlink_to(outside, target_is_directory=True)
    files, digest = candidate()
    with pytest.raises(AgentError):
        asyncio.run(apply_job({"phase": "preview", "skill_name": "review-skill",
            "files": files, "candidate_sha256": digest}, tmp_path))
    assert (outside / "SKILL.md").read_bytes() == b"outside\n"
    target.unlink()
    with pytest.raises(AgentError):
        asyncio.run(apply_job({"phase": "preview", "skill_name": "review-skill",
            "files": files, "candidate_sha256": "0" * 64}, tmp_path))


def test_cli_runs_registered_apply_job_in_approved_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    config_path = tmp_path / "config.json"
    save_config(config_path, AgentConfig("https://example.test", project, "t" * 43))
    files, digest = candidate()
    reports = []

    def server(request):
        if request.url.path.endswith("/heartbeat") or request.url.path.endswith("/progress"):
            return httpx.Response(204)
        if request.method == "GET":
            return httpx.Response(200, json={"job_id": "job_" + "a" * 32, "kind": "apply",
                "attempt": 1, "claim_token": "c" * 43,
                "lease_expires_at": (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat(),
                "payload": {"phase": "install", "skill_name": "review-skill", "files": files,
                    "candidate_sha256": digest, "apply_id": "apply_" + "e" * 32,
                    "expected_current_sha256": None}})
        reports.append(json.loads(request.content))
        return httpx.Response(204)

    assert main(["run", "--once", "--config", str(config_path)],
        transport=httpx.MockTransport(server)) == 0
    assert reports[0]["status"] == "done"
    assert (project / ".agents" / "skills" / "review-skill" / "SKILL.md").read_bytes() == b"new skill\n"


def test_failed_directory_swap_restores_previous_skill(tmp_path, monkeypatch):
    target = tmp_path / ".agents" / "skills" / "review-skill"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_bytes(b"old skill\n")
    files, digest = candidate()
    preview = asyncio.run(apply_job({"phase": "preview", "skill_name": "review-skill",
        "files": files, "candidate_sha256": digest}, tmp_path))
    original = skill_switch.os.replace

    def fail_stage(source, destination):
        if ".stage-" in str(source) and destination == target:
            raise OSError("simulated swap failure")
        return original(source, destination)

    monkeypatch.setattr(skill_switch.os, "replace", fail_stage)
    with pytest.raises(OSError):
        asyncio.run(apply_job({"phase": "install", "skill_name": "review-skill",
            "files": files, "candidate_sha256": digest,
            "apply_id": "apply_" + "f" * 32,
            "expected_current_sha256": preview["current_state_sha256"]}, tmp_path))
    assert (target / "SKILL.md").read_bytes() == b"old skill\n"


def test_rollback_rejects_backup_directory_symlink(tmp_path):
    files, digest = candidate()
    apply_id = "apply_" + "1" * 32
    asyncio.run(apply_job({"phase": "install", "skill_name": "review-skill",
        "files": files, "candidate_sha256": digest, "apply_id": apply_id,
        "expected_current_sha256": None}, tmp_path))
    backup = tmp_path / ".galtalka" / "backups" / apply_id
    moved = tmp_path / "moved-backup"
    backup.rename(moved)
    backup.symlink_to(moved, target_is_directory=True)
    with pytest.raises(AgentError):
        asyncio.run(rollback_job({"apply_id": apply_id, "skill_name": "review-skill",
            "applied_sha256": digest, "before_sha256": None}, tmp_path))
    assert (tmp_path / ".agents" / "skills" / "review-skill" / "SKILL.md").read_bytes() == b"new skill\n"


def test_agent_rejects_windows_drive_path_in_candidate(tmp_path):
    files, _ = candidate()
    files["C:/outside.txt"] = base64.b64encode(b"escape").decode()
    digest = hashlib.sha256(json.dumps({"files": files}, sort_keys=True,
        ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
    with pytest.raises(AgentError):
        asyncio.run(apply_job({"phase": "preview", "skill_name": "review-skill",
            "files": files, "candidate_sha256": digest}, tmp_path))


def test_install_preserves_edit_made_at_directory_swap(tmp_path, monkeypatch):
    target = tmp_path / ".agents" / "skills" / "review-skill"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_bytes(b"old skill\n")
    files, digest = candidate()
    preview = asyncio.run(apply_job({"phase": "preview", "skill_name": "review-skill",
        "files": files, "candidate_sha256": digest}, tmp_path))
    original = skill_switch.os.replace

    def edit_displaced(source, destination):
        result = original(source, destination)
        if source == target and ".previous-" in str(destination):
            (destination / "SKILL.md").write_bytes(b"concurrent edit\n")
        return result

    monkeypatch.setattr(skill_switch.os, "replace", edit_displaced)
    with pytest.raises(AgentError):
        asyncio.run(apply_job({"phase": "install", "skill_name": "review-skill",
            "files": files, "candidate_sha256": digest, "apply_id": "apply_" + "2" * 32,
            "expected_current_sha256": preview["current_state_sha256"]}, tmp_path))
    assert (target / "SKILL.md").read_bytes() == b"concurrent edit\n"


def test_rollback_preserves_edit_made_at_directory_swap(tmp_path, monkeypatch):
    target = tmp_path / ".agents" / "skills" / "review-skill"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_bytes(b"old skill\n")
    files, digest = candidate()
    preview = asyncio.run(apply_job({"phase": "preview", "skill_name": "review-skill",
        "files": files, "candidate_sha256": digest}, tmp_path))
    apply_id = "apply_" + "3" * 32
    asyncio.run(apply_job({"phase": "install", "skill_name": "review-skill",
        "files": files, "candidate_sha256": digest, "apply_id": apply_id,
        "expected_current_sha256": preview["current_state_sha256"]}, tmp_path))
    original = skill_switch.os.replace

    def edit_displaced(source, destination):
        result = original(source, destination)
        if source == target and ".rollback-" in str(destination):
            (destination / "SKILL.md").write_bytes(b"concurrent edit\n")
        return result

    monkeypatch.setattr(skill_switch.os, "replace", edit_displaced)
    with pytest.raises(AgentError):
        asyncio.run(rollback_job({"apply_id": apply_id, "skill_name": "review-skill",
            "applied_sha256": digest, "before_sha256": preview["current_state_sha256"]}, tmp_path))
    assert (target / "SKILL.md").read_bytes() == b"concurrent edit\n"


def test_agent_start_recovers_interrupted_directory_swap(tmp_path, monkeypatch):
    project = tmp_path / "project"
    target = project / ".agents" / "skills" / "review-skill"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_bytes(b"old skill\n")
    files, digest = candidate()
    preview = asyncio.run(apply_job({"phase": "preview", "skill_name": "review-skill",
        "files": files, "candidate_sha256": digest}, project))
    original = skill_switch.os.replace

    def interrupt_after_displace(source, destination):
        result = original(source, destination)
        if source == target and ".previous-" in str(destination):
            raise SystemExit("simulated process interruption")
        return result

    monkeypatch.setattr(skill_switch.os, "replace", interrupt_after_displace)
    with pytest.raises(SystemExit):
        asyncio.run(apply_job({"phase": "install", "skill_name": "review-skill",
            "files": files, "candidate_sha256": digest, "apply_id": "apply_" + "4" * 32,
            "expected_current_sha256": preview["current_state_sha256"]}, project))
    assert not target.exists()
    monkeypatch.setattr(skill_switch.os, "replace", original)
    config_path = tmp_path / "config.json"
    save_config(config_path, AgentConfig("https://example.test", project, "t" * 43))

    def server(request):
        if request.method == "GET":
            return httpx.Response(200, content="null", headers={"Content-Type": "application/json"})
        return httpx.Response(204)

    assert main(["run", "--once", "--config", str(config_path)],
        transport=httpx.MockTransport(server)) == 0
    assert (target / "SKILL.md").read_bytes() == b"old skill\n"


def test_agent_start_recovers_interrupted_rollback(tmp_path, monkeypatch):
    project = tmp_path / "project"
    target = project / ".agents" / "skills" / "review-skill"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_bytes(b"old skill\n")
    files, digest = candidate()
    preview = asyncio.run(apply_job({"phase": "preview", "skill_name": "review-skill",
        "files": files, "candidate_sha256": digest}, project))
    apply_id = "apply_" + "5" * 32
    asyncio.run(apply_job({"phase": "install", "skill_name": "review-skill", "files": files,
        "candidate_sha256": digest, "apply_id": apply_id,
        "expected_current_sha256": preview["current_state_sha256"]}, project))
    original = skill_switch.os.replace

    def interrupt_after_displace(source, destination):
        result = original(source, destination)
        if source == target and ".rollback-" in str(destination):
            raise SystemExit("simulated rollback interruption")
        return result

    monkeypatch.setattr(skill_switch.os, "replace", interrupt_after_displace)
    with pytest.raises(SystemExit):
        asyncio.run(rollback_job({"apply_id": apply_id, "skill_name": "review-skill",
            "applied_sha256": digest, "before_sha256": preview["current_state_sha256"]}, project))
    assert not target.exists()
    monkeypatch.setattr(skill_switch.os, "replace", original)
    skill_switch.recover_interrupted(project)
    assert (target / "SKILL.md").read_bytes() == b"new skill\n"
