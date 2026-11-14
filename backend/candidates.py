"""고정 후보 버전의 출처와 선언된 권한 및 검토 상태를 제공한다."""
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from backend.candidate_schemas import CandidateDetail, Maintainer, SourceMetadata
from backend.catalog.metadata import manifest_license, public_facts
from backend.catalog.inspection import PERMISSION_LABELS, inspect_bundle, public_url, verified_source
from backend.catalog.eligibility import repository_fixture
from backend.models import (AgentDevice, Asset, AssetVersion, CandidateAdapter, ChangeEvent,
                            Environment, Recommendation, Source)
from backend.schemas import ErrorResponse, TagEvidence
from backend.environment_data import current_environment


def short_text(value, limit=1000):
    """표시용 문자열만 제한된 길이로 반환하고 메타데이터 객체는 노출하지 않는다."""
    return " ".join(value.split())[:limit] if isinstance(value, str) and value.strip() else None


def reviewed_adapter(adapters, digest):
    """단일 검토 어댑터만 선택하며 불일치와 여러 실행 후보를 구분한다."""
    valid = [item for item in adapters if item.reviewed and item.approved_bundle_sha256 == digest
             and item.support_level in {"comparable", "applicable"}]
    if len(valid) == 1:
        return valid[0], "reviewed"
    if len(valid) > 1:
        return None, "ambiguous"
    if any(item.reviewed and item.approved_bundle_sha256 == digest for item in adapters):
        return None, "reviewed"
    if any(item.reviewed and item.approved_bundle_sha256 != digest for item in adapters):
        return None, "hash_mismatch"
    return None, "unreviewed" if adapters else "missing"


def compatibility(session, profile_id, adapter):
    """알려진 도구 차이를 표시하고 실제 기기의 준비가 확인될 때만 호환을 확정한다."""
    if adapter is None:
        return "unknown", ["검토된 실행 어댑터가 없어 호환성을 확인하지 못했습니다."]
    environment = current_environment(session, profile_id)
    if environment is None or environment.runner == "unknown":
        return "unknown", ["현재 프로젝트의 실행 환경을 확인해 주세요."]
    if adapter.runner != environment.runner:
        return "incompatible", ["현재 선택한 실행 도구와 어댑터의 지원 도구가 다릅니다."]
    device = session.get(AgentDevice, environment.device_id) if environment.device_id else None
    if (device is None or device.revoked_at is not None or not device.codex_ready or not device.codex_version
        or device.last_seen_at < (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat(timespec="microseconds").replace("+00:00", "Z")
        or environment.runner_version != device.codex_version or environment.model_id == "unknown" or adapter.runtime):
        return "unknown", ["실행 도구의 준비 상태 또는 추가 실행 조건을 확인해야 합니다."]
    return "confirmed", ["검토한 어댑터의 실행 도구와 연결 기기의 준비 상태가 일치합니다."]


def safe_evidence(tags):
    """허용된 태그와 상대 경로의 원문 근거만 상세 응답에 포함한다."""
    result = []
    allowed = {"task_types": {"code_review", "test_generation"},
        "capabilities": {"input_validation", "exception_handling", "regression_test", "test_coverage", "documentation",
            "api_security", "accessibility", "performance", "browser_testing", "test_design", "database_changes", "typing"}}
    for item in tags.get("evidence", [])[:20] if isinstance(tags.get("evidence"), list) else []:
        try:
            evidence = TagEvidence.model_validate(item)
            if (evidence.tag not in allowed[evidence.category] or evidence.line < 1 or not evidence.path
                or any(ord(char) < 32 for char in evidence.path) or evidence.path.startswith(("/", "\\")) or ":" in evidence.path
                or ".." in evidence.path.split("/") or "\\" in evidence.path):
                continue
            result.append(evidence.model_copy(update={"excerpt": evidence.excerpt[:500]}))
        except ValidationError:
            continue
    return result


def candidate_detail(session, version, profile_id):
    """고정 버전의 공개 메타데이터만 조합하고 원본 번들과 실행 경로는 숨긴다."""
    asset = session.get(Asset, version.asset_id)
    source = session.get(Source, asset.source_id)
    metadata = version.metadata_json if isinstance(version.metadata_json, dict) else {}
    repository = metadata.get("repository", {})
    repository = repository if isinstance(repository, dict) else {}
    tags = version.tags if isinstance(version.tags, dict) else {}
    change = session.scalar(select(ChangeEvent).where(ChangeEvent.to_version_id == version.id)
        .order_by(ChangeEvent.first_seen_at, ChangeEvent.id).limit(1))
    source_url = public_url(metadata.get("source_url")) or public_url(change.source_url if change else None) or public_url(source.url)
    demo = metadata.get("demo") is True
    verified = verified_source(metadata, version.revision, source.kind, source_url, source.url)
    inspection = inspect_bundle(metadata, version.bundle_sha256)
    adapters = session.scalars(select(CandidateAdapter).where(CandidateAdapter.asset_version_id == version.id).order_by(CandidateAdapter.id)).all()
    adapter, review_status = reviewed_adapter(adapters, version.bundle_sha256)
    executable = bool(adapter and verified and inspection["integrity_status"] == "verified"
        and asset.kind == "skill" and inspection["manifest_present"] and repository.get("archived") is not True
        and not repository_fixture(asset, version))
    approved = adapter if executable else None
    compatible, reasons = compatibility(session, profile_id, approved)
    permissions = inspection["permissions"]
    policy = approved.permissions if approved and isinstance(approved.permissions, dict) else {}
    permissions["allowed"] = {key: value for key, value in policy.items()
        if key in PERMISSION_LABELS and isinstance(value, bool)} if approved else {}
    for declaration in permissions["declared"]:
        for evidence in declaration["evidence"]:
            evidence["source_url"] = source_url.rstrip("/") + "/" + quote(evidence["path"]) + f"#L{evidence['line']}" if verified else None
    signals = {}
    if repository_fixture(asset, version):
        signals["repository_fixture"] = "저장소 내부 테스트 데이터이며 일반 추천과 실행 승인에서 제외합니다."
    if not verified:
        signals["source_unverified"] = "출처와 고정 원문 버전의 확인이 필요합니다."
    if demo:
        signals["demo_candidate"] = "합성 데모 데이터이며 실제 수집 후보가 아닙니다."
    if inspection["integrity_status"] != "verified":
        signals["bundle_" + inspection["integrity_status"]] = "저장된 원문과 콘텐츠 해시를 확인하지 못했습니다."
    if review_status != "reviewed":
        signals["adapter_" + review_status] = "이 고정 버전의 실행 어댑터 검토 또는 선택이 필요합니다."
    if permissions["verification"] != "declared":
        signals["permissions_" + permissions["verification"]] = "필요한 권한 선언을 모두 확인하지 못했습니다."
    for declaration in permissions["declared"]:
        if declaration["kind"] != "file_read":
            signals["declared_" + declaration["kind"]] = declaration["description"]
    if inspection["executable_files"]:
        signals["executable_files"] = "번들에 실행 가능한 참조 파일이 있어 검토가 필요합니다."
    if asset.kind == "skill" and inspection["integrity_status"] == "verified" and not inspection["manifest_present"]:
        signals["manifest_missing"] = "고정 번들에서 SKILL.md를 확인하지 못했습니다."
    if repository.get("archived") is True:
        signals["archived_repository"] = "원본 저장소가 보관 상태입니다."
    current = source.sync_state.get("current_versions", {}).get(asset.id)
    source_metadata = SourceMetadata(**public_facts(metadata, verified))
    declared_license = (manifest_license(metadata) if asset.kind == "skill" else source_metadata.repository_license
                        ) if verified and inspection["integrity_status"] == "verified" else None
    related = session.scalar(select(Recommendation.reasons).join(ChangeEvent, ChangeEvent.id == Recommendation.change_id)
        .where(Recommendation.profile_id == profile_id, ChangeEvent.to_version_id == version.id)
        .order_by(Recommendation.updated_at.desc()).limit(1))
    return CandidateDetail(candidate_version_id=version.id, asset_id=asset.id, kind=asset.kind,
        title=asset.display_name, summary=short_text(repository.get("description")), revision=version.revision,
        bundle_sha256=version.bundle_sha256, source_url=source_url,
        published_at=change.published_at if change else None, first_seen_at=change.first_seen_at if change else None,
        verified_source_at=version.verified_source_at if verified else None,
        provenance="community" if verified else "unverified", source_verification="demo" if demo else "verified" if verified else "unverified",
        integrity_status=inspection["integrity_status"], maintainer=Maintainer(license=declared_license,
            last_updated_at=source_metadata.revision_committed_at if asset.kind == "repo" else None),
        source_metadata=source_metadata, dimensions=tags.get("dimensions", {}),
        compatibility=compatible, compatibility_reasons=reasons, permissions=permissions,
        risk_signals=list(signals.values()), risk_signal_codes=list(signals),
        support_level=approved.support_level if approved else "information_only",
        adapter_id=approved.id if approved else None, adapter_review_status=review_status,
        task_types=[value for value in tags.get("task_types", []) if isinstance(value, str) and value in {"code_review", "test_generation"}],
        tag_evidence=safe_evidence(tags), related_reasons=[short_text(value) for value in related or [] if isinstance(value, str) and value.strip()],
        is_current=current == version.id if current else None, demo=demo)


def candidate_router(sessions, auth):
    """A 담당 후보 상세 조회를 인증과 응답 모델에 연결한다."""
    router = APIRouter(prefix="/api/candidates", tags=["candidates"],
        responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}})

    @router.get("/{version_id}", response_model=CandidateDetail, summary="고정 후보의 출처와 안전 정보")
    def detail(version_id: str, profile_id: str = Depends(auth)):
        """후보가 없으면 404를 반환하고 과거 버전도 지정한 그대로 조회한다."""
        with sessions() as session:
            version = session.get(AssetVersion, version_id)
            if version is None:
                raise HTTPException(404, detail={"code": "candidate_not_found", "message": "후보를 찾을 수 없습니다."})
            return candidate_detail(session, version, profile_id)

    return router
