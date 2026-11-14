"""기존 작업 방식을 불변 버전으로 등록하고 프론트 및 비교 엔진에 제공한다."""
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.models import Profile, WorkMethod
from backend.schemas import ErrorResponse, WorkMethodInput, WorkMethodResponse


def method_response(method):
    """DB 작업 방식의 고정 ID와 버전을 공개 응답으로 변환한다."""
    return WorkMethodResponse(method_id=method.id, version=method.version,
        task_type=method.task_type, prompt_text=method.prompt_text,
        required_checks=method.required_checks, scoring_preferences=method.scoring_preferences,
        is_active=method.is_active, created_at=method.created_at)


def work_method_router(sessions, auth):
    """로컬 인증을 공유하는 작업 방식 등록 및 조회 경로를 만든다."""
    router = APIRouter(prefix="/api/work-methods", tags=["work-methods"],
        responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}})

    @router.post("", response_model=WorkMethodResponse, status_code=201, summary="기존 작업 방식 등록")
    def create_method(body: WorkMethodInput, profile_id: str = Depends(auth)):
        """같은 작업의 이전 버전을 보존하고 새 버전으로 활성 방식을 전환한다."""
        try:
            with sessions.begin() as session:
                if session.get(Profile, profile_id) is None:
                    raise HTTPException(404, detail={"code": "profile_not_found", "message": "먼저 내 작업 환경을 등록해 주세요."})
                previous = session.scalar(select(WorkMethod).where(WorkMethod.profile_id == profile_id,
                    WorkMethod.task_type == body.task_type, WorkMethod.is_active.is_(True)))
                method_id = previous.id if previous else "wm_" + uuid4().hex
                version = previous.version + 1 if previous else 1
                if previous:
                    previous.is_active = False
                    session.flush()
                method = WorkMethod(id=method_id, version=version, profile_id=profile_id, **body.model_dump())
                session.add(method)
                session.flush()
                return method_response(method)
        except IntegrityError:
            raise HTTPException(409, detail={"code": "method_conflict", "message": "작업 방식이 동시에 변경됐습니다. 다시 조회한 뒤 등록해 주세요."}) from None

    @router.get("", response_model=list[WorkMethodResponse], summary="등록된 작업 방식 목록")
    def list_methods(active_only: bool = True, profile_id: str = Depends(auth)):
        """기본적으로 활성 작업 방식을 반환하고 필요하면 과거 버전도 함께 조회한다."""
        with sessions() as session:
            statement = select(WorkMethod).where(WorkMethod.profile_id == profile_id)
            if active_only:
                statement = statement.where(WorkMethod.is_active.is_(True))
            return [method_response(method) for method in session.scalars(statement.order_by(WorkMethod.task_type, WorkMethod.version.desc()))]

    @router.get("/{method_id}", response_model=WorkMethodResponse, summary="작업 방식의 고정 버전 조회")
    def get_method(method_id: str, version: int = Query(..., ge=1), profile_id: str = Depends(auth)):
        """B가 지정한 버전을 그대로 반환하며 최신 버전으로 자동 치환하지 않는다."""
        with sessions() as session:
            method = session.get(WorkMethod, (method_id, version))
            if method is None or method.profile_id != profile_id:
                raise HTTPException(404, detail={"code": "work_method_not_found", "message": "해당 작업 방식의 버전을 찾을 수 없습니다."})
            return method_response(method)

    return router
