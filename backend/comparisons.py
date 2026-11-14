"""Local comparison requests and result viewing."""
import os
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from backend.models import Asset, AssetVersion, CandidateAdapter, Comparison, Environment, EventLog, Run, WorkMethod, utc_now
from backend.runtime.comparison import execute_comparison
from backend.input_store import register_input, load_input

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "experiments" / "skill_eval" / "fixture"


class ComparisonInput(BaseModel):
    method_id: str
    method_version: int = Field(ge=1)
    candidate_version_id: str
    task_type: Literal["code_review", "test_generation"]
    input_ids: list[str] = Field(min_length=1, max_length=3)
    confirmed_upload_scope: bool
    idempotency_key: str = Field(min_length=1)


class DecisionInput(BaseModel):
    choice: str
    reason: str = Field(min_length=1)


class InputRegistration(BaseModel):
    relative_paths: list[str] = Field(min_length=1, max_length=3)
    task_type: Literal["code_review", "test_generation"]


def comparison_router(sessions, auth):
    router = APIRouter(tags=["comparisons"])

    @router.post("/api/inputs", status_code=201)
    def register(body: InputRegistration, profile_id: str = Depends(auth)):
        with sessions() as session:
            environment = session.scalar(select(Environment).where(Environment.profile_id == profile_id).order_by(Environment.scanned_at.desc()))
            if environment is None:
                raise HTTPException(409, detail={"code": "environment_not_ready", "message": "먼저 작업 경로를 승인해 주세요."})
            root = Path(environment.scan_root)
        try:
            manifest = register_input(root, body.relative_paths, ROOT / "runtime" / "inputs", body.task_type)
        except (ValueError, OSError) as error:
            raise HTTPException(422, detail={"code": "invalid_input_path", "message": "승인된 경로의 일반 파일만 선택해 주세요."}) from error
        return manifest

    @router.get("/api/inputs/suggestions")
    def suggestions(task_type: str = Query(..., pattern="^(code_review|test_generation)$"), profile_id: str = Depends(auth)):
        if task_type != "code_review" or not (FIXTURE / "app.py").is_file():
            return []
        manifest = register_input(FIXTURE, ["app.py"], ROOT / "runtime" / "inputs", task_type)
        return [{"suggestion_id": manifest["input_id"], "kind": "fixture", "label": "코드 리뷰 예제",
                 "meta": "파일 1개", "relative_paths": ["app.py"], "excluded_paths": []}]

    def run_job(comparison_id: str, input_id: str, method_prompt: str, bundle: str, digest: str,
                model: str, runner_version: str):
        with sessions.begin() as session:
            comparison = session.get(Comparison, comparison_id)
            if comparison.status != "queued":
                return
            expected_input_sha256 = comparison.input_sha256
            comparison.status = "running"
        try:
            manifest = load_input(input_id, ROOT / "runtime" / "inputs")
            if manifest["input_sha256"] != expected_input_sha256:
                raise ValueError("comparison input changed")
            result = execute_comparison(root=ROOT / "runtime" / "inputs" / input_id / "files",
                selected_paths=[Path(path) for path in manifest["relative_paths"]],
                candidate_bundle=Path(bundle), approved_bundle_sha256=digest,
                model_id=model, runner_version=runner_version, method_prompt=method_prompt,
                output_root=ROOT / "runtime" / "comparisons" / comparison_id,
                codex_command=[os.environ.get("CODEX_BIN", "codex")],
                expected_input_sha256=expected_input_sha256)
            with sessions.begin() as session:
                comparison = session.get(Comparison, comparison_id)
                for name, arm in (("baseline", result.baseline), ("candidate", result.candidate)):
                    session.add(Run(id="run_" + uuid4().hex, comparison_id=comparison_id, arm=name,
                        status=arm.status, skill_usage_status=arm.skill_usage_status,
                        result_json=arm.final_result, event_log_path=str(arm.output_path),
                        metrics_json={"seconds": arm.seconds}, failure_code=None if arm.status == "completed" else arm.status,
                        ended_at=utc_now()))
                comparison.status = "done" if result.baseline.status == result.candidate.status == "completed" and result.candidate.skill_usage_status == "confirmed" else "failed"
                comparison.completed_at = utc_now()
        except Exception:
            with sessions.begin() as session:
                comparison = session.get(Comparison, comparison_id)
                comparison.status = "failed"
                comparison.completed_at = utc_now()

    @router.post("/api/comparisons", status_code=202)
    def create(body: ComparisonInput, background: BackgroundTasks, profile_id: str = Depends(auth)):
        with sessions.begin() as session:
            existing = next((item for item in session.scalars(select(Comparison).where(Comparison.profile_id == profile_id))
                             if item.plan.get("idempotency_key") == body.idempotency_key), None)
            if existing and existing.plan.get("idempotency_key") == body.idempotency_key:
                return {"comparison_id": existing.id, "status": existing.status}
            method = session.get(WorkMethod, (body.method_id, body.method_version))
            if method is None or method.profile_id != profile_id:
                raise HTTPException(404, detail={"code": "work_method_not_found", "message": "작업 방식을 찾을 수 없습니다."})
            version = session.get(AssetVersion, body.candidate_version_id)
            adapter = session.scalar(select(CandidateAdapter).where(CandidateAdapter.asset_version_id == body.candidate_version_id))
            if version is None or adapter is None or adapter.support_level not in ("comparable", "applicable") or not adapter.reviewed:
                raise HTTPException(409, detail={"code": "candidate_not_comparable", "message": "이 후보는 비교할 수 없습니다."})
            if version.bundle_sha256 != adapter.approved_bundle_sha256:
                raise HTTPException(409, detail={"code": "candidate_version_changed", "message": "승인된 후보 버전과 현재 버전이 다릅니다."})
            if len(body.input_ids) != 1 or body.task_type != method.task_type or not body.confirmed_upload_scope:
                raise HTTPException(422, detail={"code": "invalid_comparison_input", "message": "비교 입력을 확인해 주세요."})
            try:
                manifest = load_input(body.input_ids[0], ROOT / "runtime" / "inputs")
            except (ValueError, OSError):
                raise HTTPException(422, detail={"code": "invalid_input_id", "message": "등록된 비교 입력을 다시 선택해 주세요."}) from None
            if manifest.get("task_type") != body.task_type:
                raise HTTPException(422, detail={"code": "invalid_comparison_input", "message": "입력의 작업 유형이 비교 유형과 다릅니다."})
            environment = session.scalar(select(Environment).where(Environment.profile_id == profile_id).order_by(Environment.scanned_at.desc()))
            if environment is None or not version.artifact_path:
                raise HTTPException(409, detail={"code": "environment_not_ready", "message": "실행 환경과 후보 파일을 확인해 주세요."})
            comparison_id = "cmp_" + uuid4().hex
            session.add(Comparison(id=comparison_id, profile_id=profile_id, method_id=method.id,
                method_version=method.version, candidate_version_id=version.id,
                input_manifest=[manifest],
                input_sha256=manifest["input_sha256"], plan={"idempotency_key": body.idempotency_key,
                "model_id": environment.model_id, "runner_version": environment.runner_version,
                "timeout_seconds": 180}, status="queued"))
            job = (comparison_id, manifest["input_id"], method.prompt_text, version.artifact_path,
                   adapter.approved_bundle_sha256, environment.model_id, environment.runner_version)
        background.add_task(run_job, *job)
        return {"comparison_id": comparison_id, "status": "queued"}

    @router.get("/api/comparisons/{comparison_id}")
    def detail(comparison_id: str, profile_id: str = Depends(auth)):
        with sessions() as session:
            comparison = session.get(Comparison, comparison_id)
            if comparison is None or comparison.profile_id != profile_id:
                raise HTTPException(404, detail={"code": "comparison_not_found", "message": "비교 결과를 찾을 수 없습니다."})
            runs = list(session.scalars(select(Run).where(Run.comparison_id == comparison_id)))
            version = session.get(AssetVersion, comparison.candidate_version_id)
            asset = session.get(Asset, version.asset_id)
            return {"comparison_id": comparison.id, "status": comparison.status,
                "verdict": comparison.user_decision, "verdict_reason": comparison.decision_reason,
                "baseline_label": "기존 방식", "candidate_label": asset.display_name,
                "change_title": asset.display_name, "rubric_version": "review-v1", "runs_per_arm": 1,
                "method_id": comparison.method_id, "method_version": comparison.method_version,
                "candidate_version_id": comparison.candidate_version_id, "inputs": comparison.input_manifest,
                "plan": comparison.plan, "runs": [{"arm": run.arm, "status": run.status,
                    "output_text": (run.result_json or {}).get("summary"), "result": run.result_json,
                    "seconds": run.metrics_json.get("seconds"), "skill_usage_status": run.skill_usage_status,
                    "inputs": [{"input_id": item.get("input_id"), "status": run.status,
                        "metrics": {}, "output_text": (run.result_json or {}).get("summary"),
                        "seconds": run.metrics_json.get("seconds")} for item in comparison.input_manifest]}
                    for run in runs], "findings": [], "llm_judgment": None,
                "limits": ["각 방식 1회 실행", "자동 우열 판정 없음"], "is_prerecorded": False}

    @router.get("/api/candidates/{version_id}")
    def candidate_detail(version_id: str, profile_id: str = Depends(auth)):
        with sessions() as session:
            version = session.get(AssetVersion, version_id)
            if version is None:
                raise HTTPException(404, detail={"code": "candidate_not_found", "message": "후보를 찾을 수 없습니다."})
            asset = session.get(Asset, version.asset_id)
            adapter = session.scalar(select(CandidateAdapter).where(CandidateAdapter.asset_version_id == version_id))
            return {"candidate_version_id": version.id, "title": asset.display_name,
                "revision": version.revision, "source_url": version.metadata_json.get("source_url"),
                "provenance": {"source_url": version.metadata_json.get("source_url"), "bundle_sha256": version.bundle_sha256},
                "permissions": {"declared": [], "allowed": adapter.permissions if adapter else {}},
                "risk_signals": [], "replaces": [],
                "support_level": adapter.support_level if adapter else "information_only"}

    @router.get("/api/history")
    def history(profile_id: str = Depends(auth)):
        with sessions() as session:
            events = session.scalars(select(EventLog).where(EventLog.profile_id == profile_id).order_by(EventLog.created_at.desc())).all()
            return [{"event_id": event.id, "event_type": event.event_type,
                "subject_id": event.subject_id, "payload": event.payload,
                "created_at": event.created_at} for event in events]

    @router.post("/api/comparisons/{comparison_id}/cancel")
    def cancel(comparison_id: str, profile_id: str = Depends(auth)):
        with sessions.begin() as session:
            comparison = session.get(Comparison, comparison_id)
            if comparison is None or comparison.profile_id != profile_id:
                raise HTTPException(404, detail={"code": "comparison_not_found", "message": "비교 작업을 찾을 수 없습니다."})
            if comparison.status != "queued":
                raise HTTPException(409, detail={"code": "comparison_not_cancellable", "message": "이미 실행 중이거나 종료된 작업입니다."})
            comparison.status = "cancelled"
        return {"status": "cancelled"}

    @router.patch("/api/comparisons/{comparison_id}/decision")
    def decide(comparison_id: str, body: DecisionInput, profile_id: str = Depends(auth)):
        if body.choice not in ("baseline", "candidate", "abstain"):
            raise HTTPException(422, detail={"code": "invalid_decision", "message": "선택 값을 확인해 주세요."})
        with sessions.begin() as session:
            comparison = session.get(Comparison, comparison_id)
            if comparison is None or comparison.profile_id != profile_id:
                raise HTTPException(404, detail={"code": "comparison_not_found", "message": "비교 결과를 찾을 수 없습니다."})
            if comparison.status != "done":
                raise HTTPException(409, detail={"code": "comparison_not_done", "message": "완료된 비교에서만 선택할 수 있습니다."})
            previous = comparison.user_decision
            comparison.user_decision = body.choice
            comparison.decision_reason = body.reason
            comparison.decided_at = utc_now()
            session.add(EventLog(id="event_" + uuid4().hex, profile_id=profile_id,
                event_type="comparison_decision_recorded", subject_id=comparison_id,
                payload={"choice": body.choice, "previous_choice": previous, "reason": body.reason,
                         "comparison_id": comparison_id}))
        return {"user_decision": body.choice}

    return router
