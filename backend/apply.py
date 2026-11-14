"""Queue reviewed skill preview, install, and rollback on the connected PC."""
import hashlib
import json
import re
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from backend.agent_jobs import JobSpec, enqueue_job
from backend.models import (AgentJob, ApplyRecord, Asset, AssetVersion, CandidateAdapter,
                            Comparison, EventLog, Run, Snapshot, utc_now)


class PreviewInput(BaseModel):
    comparison_id: str


class ApplyInput(BaseModel):
    preview_id: str
    change_set_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class RollbackInput(BaseModel):
    confirmed: bool


def fail(status, code, message):
    raise HTTPException(status, detail={"code": code, "message": message})


def eligible(session, comparison_id, profile_id):
    comparison = session.get(Comparison, comparison_id)
    if comparison is None or comparison.profile_id != profile_id:
        fail(404, "COMPARISON_NOT_FOUND", "비교 결과를 찾을 수 없습니다.")
    if comparison.status != "done" or comparison.user_decision != "candidate":
        fail(409, "CANDIDATE_NOT_SELECTED", "완료된 비교에서 후보를 선택해 주세요.")
    run = session.scalar(select(Run).where(Run.comparison_id == comparison_id, Run.arm == "candidate"))
    if run is None or run.status != "completed" or run.skill_usage_status != "confirmed":
        fail(409, "SKILL_USAGE_UNCONFIRMED", "후보 스킬 사용이 확인되지 않았습니다.")
    version = session.get(AssetVersion, comparison.candidate_version_id)
    adapter = session.scalar(select(CandidateAdapter).where(
        CandidateAdapter.asset_version_id == comparison.candidate_version_id,
        CandidateAdapter.support_level == "applicable", CandidateAdapter.reviewed.is_(True)))
    if version is None or adapter is None or version.bundle_sha256 != adapter.approved_bundle_sha256:
        fail(409, "CANDIDATE_NOT_APPLICABLE", "승인된 적용 후보를 확인해 주세요.")
    bundle = version.metadata_json.get("bundle")
    if not isinstance(bundle, dict) or not isinstance(bundle.get("files"), dict) or "SKILL.md" not in bundle["files"]:
        fail(409, "CANDIDATE_BUNDLE_MISSING", "후보 스킬 원본 파일을 확인해 주세요.")
    digest = hashlib.sha256(json.dumps(bundle, sort_keys=True, ensure_ascii=True,
        separators=(",", ":")).encode()).hexdigest()
    if digest != version.bundle_sha256:
        fail(409, "CANDIDATE_VERSION_CHANGED", "후보 스킬 파일이 승인된 버전과 다릅니다.")
    asset = session.get(Asset, version.asset_id)
    name = adapter.runtime.get("skill_name") or asset.canonical_key.rsplit("/", 1)[-1]
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        fail(409, "INVALID_SKILL_NAME", "적용할 스킬 이름을 확인해 주세요.")
    return comparison, version, name, bundle["files"]


def owned_job(session, job_id, profile_id, kind):
    job = session.get(AgentJob, job_id)
    if job is None or job.profile_id != profile_id or job.kind != kind:
        fail(404, "JOB_NOT_FOUND", "작업을 찾을 수 없습니다.")
    return job


def preview_view(job):
    if job.payload.get("phase") != "preview":
        fail(404, "PREVIEW_NOT_FOUND", "적용 미리보기를 찾을 수 없습니다.")
    result = job.result_json if job.status == "done" else None
    if result is None:
        return {"preview_id": job.id, "status": job.status, "message": job.message}
    if (not isinstance(result, dict) or result.get("status") != "preview"
        or result.get("candidate_sha256") != job.payload["candidate_sha256"]
        or not isinstance(result.get("changes"), list)
        or not isinstance(result.get("target_relative_path"), str)
        or (result.get("current_state_sha256") is not None and
            (not isinstance(result["current_state_sha256"], str) or
             not re.fullmatch(r"[a-f0-9]{64}", result["current_state_sha256"])))):
        fail(409, "INVALID_PREVIEW_RESULT", "연결 프로그램의 미리보기 결과를 확인해 주세요.")
    fingerprint = [job.payload["comparison_id"], result["target_relative_path"],
                   result.get("current_state_sha256"), result["candidate_sha256"], result["changes"]]
    change_hash = hashlib.sha256(json.dumps(fingerprint, sort_keys=True,
        ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
    return {"preview_id": job.id, "status": "done", "target_path": result["target_relative_path"],
        "current_state_sha256": result.get("current_state_sha256"),
        "candidate_version_id": job.payload["candidate_version_id"],
        "candidate_sha256": result["candidate_sha256"], "changes": result["changes"],
        "change_set_sha256": change_hash, "requires_confirmation": True}


def apply_router(sessions, auth):
    router = APIRouter(tags=["apply"])

    @router.post("/api/apply/preview", status_code=202)
    def preview(body: PreviewInput, profile_id: str = Depends(auth)):
        with sessions() as session:
            comparison, version, name, files = eligible(session, body.comparison_id, profile_id)
            existing = session.scalar(select(ApplyRecord).where(ApplyRecord.comparison_id == comparison.id,
                ApplyRecord.status.in_(["applying", "applied", "recovery_required"])))
            if existing:
                fail(409, "ALREADY_APPLIED", "적용 중이거나 이미 적용한 비교입니다.")
            payload = {"phase": "preview", "comparison_id": comparison.id,
                "candidate_version_id": version.id, "skill_name": name,
                "files": files, "candidate_sha256": version.bundle_sha256}
        job = enqueue_job(sessions, profile_id, JobSpec(kind="apply",
            request_key="preview:" + uuid4().hex, payload=payload))
        return {"preview_id": job.job_id, "status": job.status}

    @router.get("/api/apply/preview/{preview_id}")
    def preview_status(preview_id: str, profile_id: str = Depends(auth)):
        with sessions() as session:
            return preview_view(owned_job(session, preview_id, profile_id, "apply"))

    @router.post("/api/apply", status_code=202)
    def apply(body: ApplyInput, profile_id: str = Depends(auth)):
        with sessions.begin() as session:
            job = owned_job(session, body.preview_id, profile_id, "apply")
            if job.payload.get("phase") != "preview" or job.status != "done":
                fail(409, "PREVIEW_NOT_READY", "적용 미리보기가 완료될 때까지 기다려 주세요.")
            view = preview_view(job)
            if view["change_set_sha256"] != body.change_set_sha256:
                fail(409, "PREVIEW_CHANGED", "미리보기 내용이 달라져 다시 확인해야 합니다.")
            comparison, version, name, files = eligible(session, job.payload["comparison_id"], profile_id)
            if version.id != job.payload["candidate_version_id"] or version.bundle_sha256 != view["candidate_sha256"]:
                fail(409, "CANDIDATE_VERSION_CHANGED", "후보 버전이 변경됐습니다.")
            existing = session.scalar(select(ApplyRecord).where(ApplyRecord.comparison_id == comparison.id,
                ApplyRecord.status.in_(["applying", "applied", "recovery_required"])).with_for_update())
            if existing:
                if existing.status == "applying" and existing.id == job.payload.get("apply_id"):
                    return {"apply_id": existing.id, "status": existing.status}
                fail(409, "ALREADY_APPLIED", "적용 중이거나 이미 적용한 비교입니다.")
            apply_id = "apply_" + uuid4().hex
            snapshot_id = "snapshot_" + uuid4().hex
            session.add(Snapshot(id=snapshot_id, target_path=view["target_path"],
                backup_path=f".galtalka/backups/{apply_id}/before",
                before_sha256=view["current_state_sha256"], verified=False))
            session.flush()
            session.add(ApplyRecord(id=apply_id, comparison_id=comparison.id,
                snapshot_id=snapshot_id, candidate_version_id=version.id,
                target_path=view["target_path"], status="applying"))
            job.payload = {**job.payload, "apply_id": apply_id}
        payload = {"phase": "install", "apply_id": apply_id, "skill_name": name,
            "files": files, "candidate_sha256": version.bundle_sha256,
            "expected_current_sha256": view["current_state_sha256"]}
        try:
            queued = enqueue_job(sessions, profile_id, JobSpec(kind="apply",
                request_key="install:" + apply_id, payload=payload))
        except Exception:
            with sessions.begin() as session:
                session.get(ApplyRecord, apply_id).status = "recovery_required"
            raise
        return {"apply_id": apply_id, "status": "applying", "job_id": queued.job_id}

    def reconcile(session, record):
        install = session.scalar(select(AgentJob).where(AgentJob.request_key == "install:" + record.id,
            AgentJob.profile_id == session.get(Comparison, record.comparison_id).profile_id))
        if record.status == "applying":
            if install is None or install.status in {"failed", "cancelled"}:
                record.status = "recovery_required"
            elif install.status == "done":
                result = install.result_json or {}
                if (result.get("status") == "applied" and result.get("apply_id") == record.id
                    and result.get("applied_sha256") == install.payload["candidate_sha256"]
                    and result.get("before_sha256") == install.payload["expected_current_sha256"]):
                    record.status = "applied"
                    record.applied_sha256 = result["applied_sha256"]
                    record.applied_at = utc_now()
                    session.get(Snapshot, record.snapshot_id).verified = True
                    session.add(EventLog(id="event_" + uuid4().hex,
                        profile_id=install.profile_id, event_type="skill_applied", subject_id=record.id,
                        payload={"apply_id": record.id, "comparison_id": record.comparison_id,
                            "summary": "스킬 적용 완료", "change_title": record.target_path}))
                else:
                    record.status = "recovery_required"
        rollback = session.scalar(select(AgentJob).where(AgentJob.request_key == "rollback:" + record.id,
            AgentJob.profile_id == session.get(Comparison, record.comparison_id).profile_id))
        if rollback is not None and record.status == "applied":
            if rollback.status in {"failed", "cancelled"}:
                record.status = "recovery_required"
            elif rollback.status == "done":
                result = rollback.result_json or {}
                if result.get("status") == "rolled_back" and result.get("apply_id") == record.id:
                    record.status = "rolled_back"
                    record.rolled_back_at = utc_now()
                    session.add(EventLog(id="event_" + uuid4().hex,
                        profile_id=rollback.profile_id, event_type="skill_rolled_back", subject_id=record.id,
                        payload={"apply_id": record.id, "comparison_id": record.comparison_id,
                            "summary": "이전 스킬 상태 복원", "change_title": record.target_path}))
                else:
                    record.status = "recovery_required"
        return install, rollback

    @router.get("/api/apply/{apply_id}")
    def detail(apply_id: str, profile_id: str = Depends(auth)):
        with sessions.begin() as session:
            record = session.get(ApplyRecord, apply_id)
            comparison = session.get(Comparison, record.comparison_id) if record else None
            if comparison is None or comparison.profile_id != profile_id:
                fail(404, "APPLY_NOT_FOUND", "적용 이력을 찾을 수 없습니다.")
            install, rollback = reconcile(session, record)
            active = rollback or install
            return {"apply_id": record.id, "comparison_id": record.comparison_id,
                "candidate_version_id": record.candidate_version_id, "status": record.status,
                "job_status": active.status if active else None,
                "applied_at": record.applied_at, "rolled_back_at": record.rolled_back_at,
                "rollback_available": record.status == "applied" and rollback is None}

    @router.post("/api/apply/{apply_id}/rollback", status_code=202)
    def rollback(apply_id: str, body: RollbackInput, profile_id: str = Depends(auth)):
        if not body.confirmed:
            fail(422, "CONFIRMATION_REQUIRED", "복원 여부를 확인해 주세요.")
        with sessions.begin() as session:
            record = session.get(ApplyRecord, apply_id)
            comparison = session.get(Comparison, record.comparison_id) if record else None
            if comparison is None or comparison.profile_id != profile_id:
                fail(404, "APPLY_NOT_FOUND", "적용 이력을 찾을 수 없습니다.")
            _, previous = reconcile(session, record)
            if previous:
                return {"apply_id": apply_id, "status": record.status, "job_id": previous.id}
            if record.status != "applied":
                fail(409, "ROLLBACK_UNAVAILABLE", "적용이 끝난 스킬만 복원할 수 있습니다.")
            snapshot = session.get(Snapshot, record.snapshot_id)
            payload = {"apply_id": apply_id, "skill_name": record.target_path.rsplit("/", 1)[-1],
                "applied_sha256": record.applied_sha256, "before_sha256": snapshot.before_sha256}
        queued = enqueue_job(sessions, profile_id, JobSpec(kind="rollback",
            request_key="rollback:" + apply_id, payload=payload))
        return {"apply_id": apply_id, "status": "rollback_queued", "job_id": queued.job_id}

    return router
