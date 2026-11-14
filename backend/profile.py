"""온보딩 및 설정 화면에서 사용하는 프로필 저장과 조회."""
from fastapi import APIRouter, Depends, HTTPException

from backend.models import Profile, utc_now
from backend.schemas import ErrorResponse, ProfileInput, ProfileResponse, ProfileSaved


def profile_router(sessions, auth):
    """같은 로컬 프로필을 저장하고 다시 읽는 인증된 라우터를 구성한다."""
    router = APIRouter(prefix="/api/profile", tags=["profile"],
                       responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}})

    @router.get("", response_model=ProfileResponse, summary="내 프로필 조회")
    def get_profile(profile_id: str = Depends(auth)):
        """온보딩 완료 여부를 판단할 프로필을 반환하며 미등록은 404로 구분한다."""
        with sessions() as session:
            profile = session.get(Profile, profile_id)
            if profile is None:
                raise HTTPException(404, detail={"code": "profile_not_found", "message": "먼저 내 작업 환경을 등록해 주세요."})
            return ProfileResponse(profile_id=profile.id, profile_version=profile.version,
                role=profile.role, primary_language=profile.primary_language,
                task_types=profile.task_types, goals=profile.goals,
                preferences=profile.preferences, context=profile.context_json, updated_at=profile.updated_at)

    @router.put("", response_model=ProfileSaved, summary="내 프로필 등록 및 수정")
    def save_profile(body: ProfileInput, profile_id: str = Depends(auth)):
        """내용이 바뀔 때만 프로필 버전을 올리고 추천 재계산 필요 여부를 반환한다."""
        values = body.model_dump()
        values["context_json"] = values.pop("context")
        with sessions.begin() as session:
            profile = session.get(Profile, profile_id)
            if profile is not None and "context" not in body.model_fields_set:
                # 기존 프론트가 새 선택 필드를 보내지 않아도 등록한 기술 맥락을 지우지 않는다.
                values["context_json"] = profile.context_json
            changed = profile is None or any(getattr(profile, key) != value for key, value in values.items())
            if profile is None:
                profile = Profile(id=profile_id, **values)
                session.add(profile)
            elif changed:
                for key, value in values.items():
                    setattr(profile, key, value)
                profile.version += 1
                profile.updated_at = utc_now()
            session.flush()
            return ProfileSaved(profile_id=profile.id, profile_version=profile.version, recommendations_stale=changed)

    return router
