"""선택한 기기에서 완료된 설치 기록을 추천의 마지막 확인 상태로 읽는다."""
import re

from sqlalchemy import select

from backend.models import AgentJob, ApplyRecord, AssetVersion, Comparison


def known_applied_versions(session, profile_id, environment):
    """소유자와 기기 및 전체 번들 해시가 일치하는 완료 설치 버전만 반환한다."""
    if environment is None:
        return set()
    owner = environment.get("profile_id") if isinstance(environment, dict) else getattr(environment, "profile_id", None)
    device = environment.get("device_id") if isinstance(environment, dict) else getattr(environment, "device_id", None)
    if owner != profile_id or not device:
        return set()
    rows = session.execute(select(ApplyRecord, AssetVersion, AgentJob)
        .join(Comparison, Comparison.id == ApplyRecord.comparison_id)
        .join(AssetVersion, AssetVersion.id == ApplyRecord.candidate_version_id)
        .join(AgentJob, AgentJob.request_key == "install:" + ApplyRecord.id)
        .where(Comparison.profile_id == profile_id,
            Comparison.candidate_version_id == ApplyRecord.candidate_version_id,
            ApplyRecord.status == "applied", AgentJob.profile_id == profile_id,
            AgentJob.device_id == device, AgentJob.kind == "apply", AgentJob.status == "done")).all()
    if not rows:
        return set()
    # 복원 요청 뒤에는 이전 설치 기록을 현재 상태로 단정하지 않는다. B의 상태 조정은 수행하지 않는다.
    rollback_keys = set(session.scalars(select(AgentJob.request_key).where(
        AgentJob.profile_id == profile_id, AgentJob.device_id == device, AgentJob.kind == "rollback",
        AgentJob.request_key.in_(["rollback:" + record.id for record, _, _ in rows]))))
    result = set()
    for record, version, job in rows:
        if "rollback:" + record.id in rollback_keys:
            continue
        payload = job.payload if isinstance(job.payload, dict) else {}
        report = job.result_json if isinstance(job.result_json, dict) else {}
        digest = version.bundle_sha256
        if (not isinstance(digest, str) or re.fullmatch(r"[a-f0-9]{64}", digest) is None
                or record.applied_sha256 != digest or payload.get("candidate_sha256") != digest
                or report.get("applied_sha256") != digest or payload.get("phase") != "install"
                or payload.get("apply_id") != record.id or report.get("apply_id") != record.id
                or report.get("status") != "applied"
                or payload.get("candidate_version_id", version.id) != version.id
                or report.get("candidate_version_id", version.id) != version.id):
            continue
        result.add(version.id)
    return result
