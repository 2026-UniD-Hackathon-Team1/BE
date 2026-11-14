"""고정 카탈로그 버전과 현재 작업을 연결한 추천 및 사용자 선택 API."""
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select

from backend.catalog.service import stable_id
from backend.models import (Asset, AssetVersion, CandidateAdapter, ChangeEvent, Comparison,
                            Environment, Profile, Recommendation, RecommendationDecision,
                            Source, WorkMethod, utc_now)
from backend.schemas import (ErrorResponse, LatestComparison, RecommendationAction,
                             RecommendationCard, RecommendationList)


def matching_candidates(session, profile):
    """현재 버전과 확인된 태그만 사용해 관련성 있는 후보를 추린다."""
    methods = {method.task_type: method for method in session.scalars(select(WorkMethod).where(
        WorkMethod.profile_id == profile.id, WorkMethod.is_active.is_(True)))}
    rows = session.execute(select(Asset, AssetVersion, ChangeEvent, Source)
        .join(AssetVersion, AssetVersion.asset_id == Asset.id)
        .join(ChangeEvent, ChangeEvent.to_version_id == AssetVersion.id)
        .join(Source, Source.id == Asset.source_id)
        .order_by(ChangeEvent.first_seen_at.desc(), Asset.id)).all()
    matched = []
    for asset, version, change, source in rows:
        if not version.metadata_json.get("demo") and source.sync_state.get("current_versions", {}).get(asset.id) != version.id:
            continue
        tags = version.tags if isinstance(version.tags, dict) else {}
        tasks = set(tags.get("task_types", [])) & set(profile.task_types)
        if not tasks:
            continue
        languages = set(tags.get("languages", [])) - {"unknown"}
        if languages and profile.primary_language not in languages:
            continue
        goals = set(tags.get("capabilities", [])) & set(profile.goals)
        task = sorted(tasks, key=lambda item: (item not in methods, item))[0]
        method = methods.get(task)
        score = (50 if method else 35) + (20 if goals else 0)
        reasons = ["현재 작업 유형과 관련 있음"]
        if goals:
            reasons.append("등록한 목표 기능과 관련 있음: " + ", ".join(sorted(goals)))
        if method:
            reasons.append("등록한 기존 작업 방식과 비교할 후보")
        matched.append((score, asset, version, change, task, method, reasons))
    return sorted(matched, key=lambda row: (-row[0], row[3].first_seen_at, row[1].id))


def approved_adapter(session, version, task):
    """관리자가 검토한 고정 해시의 어댑터만 실행 지원 후보로 인정한다."""
    return session.scalar(select(CandidateAdapter).where(
        CandidateAdapter.asset_version_id == version.id,
        CandidateAdapter.approved_bundle_sha256 == version.bundle_sha256,
        CandidateAdapter.reviewed.is_(True)))


def recommendation_router(sessions, auth):
    """로컬 인증을 공유하는 추천 조회 및 상태 변경 경로를 구성한다."""
    router = APIRouter(prefix="/api/recommendations", tags=["recommendations"],
        responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}})

    @router.get("", response_model=RecommendationList, summary="내 작업에 맞는 변화 목록")
    def list_recommendations(task_type: str | None = None, include_hidden: bool = False,
                             profile_id: str = Depends(auth)):
        """관련성 근거와 선택 상태를 반환하고 숨긴 카드는 요청할 때만 포함한다."""
        items = []
        with sessions.begin() as session:
            profile = session.get(Profile, profile_id)
            if profile is None:
                raise HTTPException(404, detail={"code": "profile_not_found", "message": "먼저 내 작업 환경을 등록해 주세요."})
            for score, asset, version, change, task, method, reasons in matching_candidates(session, profile):
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
                if recommendation.status == "applied":
                    continue
                decision = session.get(RecommendationDecision, recommendation_id)
                status = decision.status if decision else "new"
                if status != "new" and not include_hidden:
                    continue
                adapter = approved_adapter(session, version, task)
                environment = session.scalar(select(Environment).where(Environment.profile_id == profile_id))
                comparable = bool(adapter and environment and adapter.runner == environment.runner
                                  and task in adapter.supported_tasks)
                comparison = session.scalar(select(Comparison).where(
                    Comparison.profile_id == profile_id, Comparison.recommendation_id == recommendation_id)
                    .order_by(Comparison.created_at.desc()))
                items.append(RecommendationCard(recommendation_id=recommendation_id,
                    candidate_version_id=version.id, adapter_id=adapter.id if comparable else None,
                    work_method_id=method.id if method else None,
                    work_method_version=method.version if method else None,
                    title=asset.display_name, change_type=change.change_type,
                    task_types=sorted(set(version.tags.get("task_types", []))), score=score,
                    related_reasons=reasons, source_url=change.source_url,
                    compatibility="confirmed" if comparable else "unknown",
                    support_level=adapter.support_level if comparable else "information_only",
                    status=status, reason=decision.reason if decision else None,
                    latest_comparison=LatestComparison(comparison_id=comparison.id,
                        status=comparison.status, verdict=comparison.user_decision) if comparison else None,
                    demo=bool(version.metadata_json.get("demo"))))
        return RecommendationList(items=items)

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
