"""고정 카탈로그 버전과 현재 작업을 연결한 추천 및 사용자 선택 API."""
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select

from backend.catalog.service import stable_id
from backend.catalog.metadata import public_facts
from backend.catalog.eligibility import repository_fixture
from backend.catalog.requirements import declared_requirements
from backend.catalog.families import candidate_family
from backend.candidates import safe_evidence, compatibility as candidate_compatibility
from backend.environment_data import current_environment
from backend.catalog.inspection import inspect_bundle, public_url, verified_source
from backend.models import (Asset, AssetVersion, CandidateAdapter, ChangeEvent, Comparison,
                            Environment, Profile, Recommendation, RecommendationDecision,
                            Source, WorkMethod, utc_now)
from backend.schemas import (ErrorResponse, LatestComparison, RecommendationAction,
                             RecommendationCard, RecommendationList)
from backend.personalization import build_context, rank_candidate
from backend.recommendation_state import known_applied_versions


def distinct_alternatives(rows, context):
    """같은 스킬의 배포 미러와 동일 번들을 한 대안으로 묶고 맥락에 맞는 경로를 고른다."""
    parents = list(range(len(rows)))

    def root(index):
        """중복 관계의 대표 위치를 경로 압축으로 찾는다."""
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    seen = {}
    for index, row in enumerate(rows):
        family = candidate_family(row[1], row[2])
        row[7]["matches"]["alternative_group"] = family["family_key"]
        keys = ["family:" + family["family_key"]]
        if family["bundle_sha256"]:
            keys.append("bundle:" + family["bundle_sha256"])
        for key in keys:
            if key in seen:
                parents[root(index)] = root(seen[key])
            seen[key] = index
    groups = {}
    for index, row in enumerate(rows):
        groups.setdefault(root(index), []).append((index, row))
    current = set(context["effective"]["tools"]) & {"codex_cli", "claude_code", "cursor", "github_copilot"}
    result, duplicates = [], []
    for members in groups.values():
        def preference(member):
            """같은 기능이면 현재 도구와 충돌하지 않는 경로와 높은 관련성을 먼저 고른다."""
            index, row = member
            tags = row[2].tags
            tools = set(tags.get("supported_tools", [])) & {"codex_cli", "claude_code", "cursor", "github_copilot"}
            return (bool(current and tools and not current & tools), -row[0], index)

        _, selected = min(members, key=preference)
        variants = sorted(row[2].id for _, row in members)
        selected[7]["matches"]["variant_version_ids"] = variants
        selected[7]["matches"]["variant_count"] = len(variants)
        if len(variants) > 1:
            selected[6].append(f"같은 스킬의 {len(variants)}개 배포 경로를 하나의 추천 대안으로 표시함")
        result.append(selected)
        duplicates.extend({"candidate_version_id": row[2].id, "title": row[1].display_name,
            "excluded_reasons": ["동일 스킬의 다른 배포 경로 또는 동일한 전체 번들이므로 대안 수에 중복 집계하지 않음"]}
            for _, row in members if row is not selected)
    order = {id(row): index for index, row in enumerate(rows)}
    result.sort(key=lambda row: order[id(row)])
    return result, duplicates


def assess_candidates(session, profile):
    """현재 원본을 개인 작업 단계와 연결하고 제외 및 보류한 근거도 남긴다."""
    methods = {method.task_type: method for method in session.scalars(select(WorkMethod).where(
        WorkMethod.profile_id == profile.id, WorkMethod.is_active.is_(True)))}
    environment = current_environment(session, profile.id)
    context = build_context(profile, methods, environment)
    applied = known_applied_versions(session, profile.id, environment)
    context["last_confirmed_installation"] = {"candidate_version_ids": sorted(applied),
        "verification": "completed_agent_apply_records", "live_files_verified": False}
    rows = session.execute(select(Asset, AssetVersion, ChangeEvent, Source)
        .join(AssetVersion, AssetVersion.asset_id == Asset.id)
        .join(ChangeEvent, ChangeEvent.to_version_id == AssetVersion.id)
        .join(Source, Source.id == Asset.source_id)
        .order_by(ChangeEvent.first_seen_at.desc(), Asset.id)).all()
    matched = []
    excluded = []
    for asset, version, change, source in rows:
        if not version.metadata_json.get("demo") and source.sync_state.get("current_versions", {}).get(asset.id) != version.id:
            continue
        if repository_fixture(asset, version):
            excluded.append({"candidate_version_id": version.id, "title": asset.display_name,
                "excluded_reasons": ["저장소 내부 테스트 데이터는 일반 추천 후보에서 제외함"]})
            continue
        if version.id in applied:
            excluded.append({"candidate_version_id": version.id, "title": asset.display_name,
                "excluded_reasons": ["현재 기기에서 동일한 전체 번들 버전의 설치 완료가 기록됨"]})
            continue
        # 데모 소스는 의도적으로 비활성이며 실제 비활성 소스와 구분해 평가한다.
        target_source = {"enabled": True, "kind": source.kind, "url": source.url} if version.metadata_json.get("demo") else source
        assessment = rank_candidate(profile, methods, environment, asset, version, change, target_source)
        if not assessment["eligible"]:
            excluded.append({"candidate_version_id": version.id, "title": asset.display_name,
                             "excluded_reasons": assessment["excluded_reasons"]})
            continue
        task = assessment["matches"]["task_type"]
        evidence = next((item for item in assessment["evidence"]
                         if item["dimension"] == "task_types" and item["value"] == task), None)
        if evidence:
            assessment["reasons"].append(f"원문에 작업이 명시됨: {evidence['path']}:{evidence['line']}")
        matched.append((assessment["score"], asset, version, change, task,
                        methods.get(task), assessment["reasons"], assessment))
    matched.sort(key=lambda row: row[1].id)
    matched.sort(key=lambda row: row[3].first_seen_at, reverse=True)
    matched.sort(key=lambda row: row[0], reverse=True)
    matched, duplicates = distinct_alternatives(matched, context)
    excluded.extend(duplicates)
    return matched, excluded, context


def matching_candidates(session, profile):
    """기존 내부 호출자가 사용하던 추천 튜플 형식을 유지한다."""
    return [row[:7] for row in assess_candidates(session, profile)[0]]


def approved_adapter(session, version, task):
    """후보 상세와 같은 출처 및 무결성 기준을 만족하는 단일 실행 어댑터만 인정한다."""
    adapters = session.scalars(select(CandidateAdapter).where(
        CandidateAdapter.asset_version_id == version.id,
        CandidateAdapter.approved_bundle_sha256 == version.bundle_sha256,
        CandidateAdapter.reviewed.is_(True), CandidateAdapter.support_level.in_(["comparable", "applicable"]))).all()
    if len(adapters) != 1:
        return None
    metadata = version.metadata_json if isinstance(version.metadata_json, dict) else {}
    asset = session.get(Asset, version.asset_id)
    source = session.get(Source, asset.source_id)
    if repository_fixture(asset, version):
        return None
    change = session.scalar(select(ChangeEvent).where(ChangeEvent.to_version_id == version.id)
        .order_by(ChangeEvent.first_seen_at, ChangeEvent.id).limit(1))
    url = public_url(metadata.get("source_url")) or public_url(change.source_url if change else None) or public_url(source.url)
    if not verified_source(metadata, version.revision, source.kind, url, source.url):
        return None
    repository = metadata.get("repository", {})
    if asset.kind != "skill" or isinstance(repository, dict) and repository.get("archived") is True:
        return None
    inspection = inspect_bundle(metadata, version.bundle_sha256)
    return adapters[0] if inspection["integrity_status"] == "verified" and inspection["manifest_present"] else None


def recommendation_router(sessions, auth):
    """로컬 인증을 공유하는 추천 조회 및 상태 변경 경로를 구성한다."""
    router = APIRouter(prefix="/api/recommendations", tags=["recommendations"],
        responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}})

    @router.get("", response_model=RecommendationList, summary="내 작업에 맞는 변화 목록")
    def list_recommendations(task_type: str | None = None, include_hidden: bool = False,
                             include_excluded: bool = False,
                             profile_id: str = Depends(auth)):
        """관련성 근거와 선택 상태를 반환하고 숨긴 카드는 요청할 때만 포함한다."""
        items = []
        with sessions.begin() as session:
            profile = session.get(Profile, profile_id)
            if profile is None:
                raise HTTPException(404, detail={"code": "profile_not_found", "message": "먼저 내 작업 환경을 등록해 주세요."})
            matched, excluded, context = assess_candidates(session, profile)
            for score, asset, version, change, task, method, reasons, assessment in matched:
                if task_type and task_type != task:
                    continue
                recommendation_id = stable_id("rec", profile_id + ":" + change.id)
                recommendation = session.get(Recommendation, recommendation_id)
                if recommendation is None:
                    recommendation = Recommendation(id=recommendation_id, profile_id=profile_id,
                        change_id=change.id, score=score, reasons=reasons)
                    session.add(recommendation)
                    session.flush()
                elif recommendation.score != score or recommendation.reasons != reasons:
                    recommendation.score = score
                    recommendation.reasons = reasons
                    recommendation.updated_at = utc_now()
                variants = assessment["matches"].get("variant_version_ids", [version.id])
                decision = session.scalar(select(RecommendationDecision)
                    .join(Recommendation, Recommendation.id == RecommendationDecision.recommendation_id)
                    .join(ChangeEvent, ChangeEvent.id == Recommendation.change_id)
                    .join(AssetVersion, AssetVersion.id == ChangeEvent.to_version_id)
                    .where(RecommendationDecision.profile_id == profile_id,
                           Recommendation.profile_id == profile_id,
                           ChangeEvent.to_version_id.in_(variants), AssetVersion.revision == version.revision)
                    .order_by(RecommendationDecision.updated_at.desc(), RecommendationDecision.recommendation_id).limit(1))
                status = decision.status if decision else "new"
                if status != "new" and not include_hidden:
                    continue
                adapter = approved_adapter(session, version, task)
                environment = current_environment(session, profile_id)
                comparable = bool(adapter and method and environment and adapter.runner == environment.runner
                                  and task == "code_review" and task in version.tags.get("task_types", [])
                                  and task in adapter.supported_tasks and (
                                      environment.device_id is None and version.artifact_path or
                                      environment.device_id is not None and
                                      candidate_compatibility(session, profile_id, adapter)[0] == "confirmed"))
                comparison = session.scalar(select(Comparison).where(
                    Comparison.profile_id == profile_id, Comparison.recommendation_id == recommendation_id)
                    .order_by(Comparison.created_at.desc()))
                metadata_source = session.get(Source, asset.source_id)
                items.append(RecommendationCard(recommendation_id=recommendation_id,
                    alternative_group=assessment["matches"]["alternative_group"],
                    variant_count=assessment["matches"]["variant_count"],
                    variant_version_ids=assessment["matches"]["variant_version_ids"],
                    requirements=declared_requirements(version.metadata_json, version.bundle_sha256),
                    source_metadata=public_facts(version.metadata_json, verified_source(version.metadata_json,
                        version.revision, metadata_source.kind, change.source_url, metadata_source.url)),
                    candidate_version_id=version.id, adapter_id=adapter.id if comparable else None,
                    work_method_id=method.id if method else None,
                    work_method_version=method.version if method else None,
                    title=asset.display_name, change_type=change.change_type,
                    task_types=sorted(set(version.tags.get("task_types", []))), score=score,
                    related_reasons=reasons, tag_evidence=safe_evidence(version.tags), source_url=change.source_url,
                    compatibility="confirmed" if comparable else "unknown",
                    support_level=adapter.support_level if comparable else "information_only",
                    status=status, reason=decision.reason if decision else None,
                    latest_comparison=LatestComparison(comparison_id=comparison.id,
                        status=comparison.status, verdict=comparison.user_decision) if comparison else None,
                    demo=bool(version.metadata_json.get("demo")), assessment=assessment))
        return RecommendationList(items=items, context=context, excluded=excluded if include_excluded else [])

    @router.patch("/{recommendation_id}", status_code=204, summary="추천 카드 선택 상태 변경")
    def update_status(recommendation_id: str, body: RecommendationAction,
                      profile_id: str = Depends(auth)):
        """나중에, 관심 없음, 다시 보기 및 기존 방식 유지 선택을 재조회 후에도 보존한다."""
        with sessions.begin() as session:
            recommendation = session.get(Recommendation, recommendation_id)
            if recommendation is None or recommendation.profile_id != profile_id:
                raise HTTPException(404, detail={"code": "recommendation_not_found", "message": "해당 추천을 찾을 수 없습니다."})
            decision = session.get(RecommendationDecision, recommendation_id)
            if decision is None:
                decision = RecommendationDecision(recommendation_id=recommendation_id,
                    profile_id=profile_id, status=body.status, reason=body.reason)
                session.add(decision)
            else:
                decision.status = body.status
                decision.reason = body.reason
                decision.updated_at = utc_now()
        return Response(status_code=204)

    return router
