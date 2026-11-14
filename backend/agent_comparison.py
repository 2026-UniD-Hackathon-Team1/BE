"""Validate PC comparison metadata and reconcile bounded results into runs."""
import re

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from backend.comparison_contract import ComparedInput
from backend.agent_jobs import expire_jobs
from backend import agent
from backend.models import AgentJob, Run, utc_now


def input_manifest(session, profile_id, device_id, input_id):
    """Read only the completed manifest; input file bytes stay on the PC."""
    if not re.fullmatch(r"input_[a-f0-9]{32}", input_id):
        raise HTTPException(422, detail={"code": "invalid_input_id", "message": "비교 입력 ID를 확인해 주세요."})
    job = session.scalar(select(AgentJob).where(AgentJob.profile_id == profile_id,
        AgentJob.device_id == device_id, AgentJob.kind == "snapshot_inputs",
        AgentJob.request_key == "input:" + input_id))
    if job is None or job.status != "done":
        raise HTTPException(422, detail={"code": "invalid_input_id", "message": "이 기기에 등록된 비교 입력을 확인해 주세요."})
    value = job.result_json
    if (not isinstance(value, dict) or set(value) != {"input_id", "task_type", "relative_paths", "input_sha256"}
        or value["input_id"] != input_id or value["task_type"] != job.payload.get("task_type")
        or value["relative_paths"] != job.payload.get("relative_paths")
        or not isinstance(value["input_sha256"], str)
        or not re.fullmatch(r"[a-f0-9]{64}", value["input_sha256"])):
        raise HTTPException(409, detail={"code": "invalid_input_snapshot", "message": "PC 비교 입력 결과를 확인해 주세요."})
    return value


def sync_agent_comparison(session, comparison):
    """Map one authenticated job report once, including partial failures."""
    if "agent_device_id" not in comparison.plan or comparison.status in {"done", "failed", "cancelled"}:
        return
    job = session.scalar(select(AgentJob).where(AgentJob.profile_id == comparison.profile_id,
        AgentJob.device_id == comparison.plan["agent_device_id"],
        AgentJob.kind == "run_comparison", AgentJob.request_key == "comparison:" + comparison.id))
    if job is None:
        return
    expire_jobs(session, job.device_id, agent.current_time())
    rows = {row.arm: row for row in session.scalars(select(Run).where(Run.comparison_id == comparison.id))}
    if job.status in {"queued", "running"}:
        comparison.status = job.status
        for row in rows.values():
            row.status = job.status
        return
    if job.status != "done":
        comparison.status = "failed"
        comparison.completed_at = utc_now()
        for row in rows.values():
            row.status = "failed"
            row.failure_code = "agent_job_failed"
            row.ended_at = utc_now()
        return
    value = job.result_json
    try:
        if (not isinstance(value, dict) or set(value) != {"comparison_id", "input_sha256", "model_id", "runner_version", "runs"}
            or value["comparison_id"] != comparison.id or value["model_id"] != comparison.plan["model_id"]
            or value["input_sha256"] != comparison.input_sha256
            or value["runner_version"] != comparison.plan["runner_version"]
            or not isinstance(value["runs"], dict) or set(value["runs"]) != {"baseline", "candidate"}):
            raise ValueError
        parsed = {}
        expected_ids = [item["input_id"] for item in comparison.input_manifest]
        for arm in ("baseline", "candidate"):
            entries = value["runs"][arm]["inputs"]
            if not isinstance(entries, list) or [entry.get("input_id") for entry in entries] != expected_ids:
                raise ValueError
            parsed[arm] = [ComparedInput.model_validate(entry).model_dump() for entry in entries]
            if any(entry["skill_usage_status"] != ("not_expected" if arm == "baseline" else entry["skill_usage_status"])
                or entry["status"] == "completed" and entry["result"] is None
                for entry in parsed[arm]):
                raise ValueError
    except (KeyError, TypeError, ValueError, ValidationError, AttributeError):
        comparison.status = "failed"
        comparison.completed_at = utc_now()
        for row in rows.values():
            row.status = "failed"
            row.failure_code = "invalid_agent_result"
            row.ended_at = utc_now()
        return
    for arm, row in rows.items():
        entries = parsed[arm]
        complete = all(entry["status"] == "completed" for entry in entries)
        row.result_json = {"inputs": entries}
        row.metrics_json = {"inputs": [{"input_id": entry["input_id"], "seconds": entry["seconds"]}
            for entry in entries], "seconds": sum(entry["seconds"] or 0 for entry in entries)}
        row.skill_usage_status = ("not_expected" if arm == "baseline" else
            "confirmed" if complete and all(entry["skill_usage_status"] == "confirmed" for entry in entries)
            else "unconfirmed")
        row.status = "completed" if complete and (arm == "baseline" or row.skill_usage_status == "confirmed") else "failed"
        row.failure_code = None if row.status == "completed" else "input_run_failed"
        row.ended_at = utc_now()
    comparison.status = "done" if all(row.status == "completed" for row in rows.values()) else "failed"
    comparison.completed_at = utc_now()
