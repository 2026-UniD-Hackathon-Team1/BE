"""프론트가 계약을 확인할 수 있는 API와 OpenAPI 문서 진입점."""
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import SQLAlchemyError

from backend.db import make_engine, session_factory
from backend.settings import database_url as configured_database_url
from backend.auth import local_auth
from backend.models import Asset, AssetVersion, ChangeEvent, Source
from backend.profile import profile_router
from backend.recommendations import recommendation_router
from backend.work_methods import work_method_router
from backend.comparisons import comparison_router
from backend.schemas import CandidateList, CandidateSummary, ErrorResponse, HealthResponse, SourceList, SourceSummary

def candidate_rows(session):
    """현재 수집 버전과 데모 후보를 정렬해 반환하며 이전 버전은 목록에서 제외한다."""
    rows = session.execute(select(Asset, AssetVersion, ChangeEvent, Source)
        .join(AssetVersion, AssetVersion.asset_id == Asset.id)
        .join(ChangeEvent, ChangeEvent.to_version_id == AssetVersion.id)
        .join(Source, Source.id == Asset.source_id)
        .order_by(ChangeEvent.first_seen_at.desc(), Asset.id)).all()
    return [row for row in rows if row[1].metadata_json.get("demo") or
            row[3].sync_state.get("current_versions", {}).get(row[0].id) == row[1].id]


def serialize_candidate(asset, version, change, source):
    """공개 목록에는 출처와 버전만 제공하며 원본 번들과 로컬 경로는 노출하지 않는다."""
    return CandidateSummary(candidate_version_id=version.id, asset_id=asset.id, kind=asset.kind,
        title=asset.display_name, revision=version.revision, bundle_sha256=version.bundle_sha256,
        source_url=change.source_url, first_seen_at=change.first_seen_at,
        classification_status=version.metadata_json.get("classification_status", "demo"),
        tag_evidence=version.tags.get("evidence", []),
        demo=bool(version.metadata_json.get("demo", False)))


def create_app(*, database_url: str | None = None, cors_origins: list[str] | None = None,
               local_token: str | None = None):
    """설정을 주입할 수 있는 API를 만들고 시작 시 DB 변경이나 외부 통신은 하지 않는다."""
    url = configured_database_url(database_url)
    origins = cors_origins if cors_origins is not None else [
        item.strip() for item in os.environ.get("CORS_ORIGINS", "http://localhost:5173").split(",") if item.strip()]
    engine = make_engine(url)
    sessions = session_factory(engine)
    auth = local_auth(local_token if local_token is not None else os.environ.get("APP_LOCAL_TOKEN"))

    @asynccontextmanager
    async def lifespan(app):
        """API 종료 시 연결을 반환하고 스키마 생성은 별도 마이그레이션에 맡긴다."""
        try:
            yield
        finally:
            engine.dispose()

    app = FastAPI(title="갈아탈까 API", version="0.1.0", lifespan=lifespan,
        description="공통 카탈로그와 로컬 프로필, 작업 방식 및 추천 API입니다. 개인 API는 Authorize에 X-Local-Token을 입력하세요.",
        openapi_tags=[{"name": "system", "description": "상태 확인"},
                      {"name": "catalog", "description": "발견한 후보와 수집 상태. 성능 검증 결과가 아닙니다."},
                      {"name": "profile", "description": "내 작업 환경 등록과 온보딩 상태 조회. 로컬 단일 사용자용입니다."},
                      {"name": "work-methods", "description": "기존 작업 방식의 불변 버전 등록과 조회"},
                      {"name": "recommendations", "description": "작업 관련성 추천과 카드 상태 변경. 점수는 성능 점수가 아닙니다."}],
        responses={422: {"model": ErrorResponse}, 503: {"model": ErrorResponse}})
    if origins:
        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "PUT", "POST", "PATCH"],
                           allow_headers=["Content-Type", "X-Local-Token"])
    app.include_router(profile_router(sessions, auth))
    app.include_router(work_method_router(sessions, auth))
    app.include_router(comparison_router(sessions, auth))
    app.include_router(recommendation_router(sessions, auth))

    @app.exception_handler(HTTPException)
    async def http_error(request, error):
        """API 오류를 프론트가 처리할 공통 형식으로 반환한다."""
        detail = error.detail if isinstance(error.detail, dict) else {"code": "request_failed", "message": str(error.detail)}
        return JSONResponse(status_code=error.status_code, content={"error": detail})

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        """검증 실패 시 입력 원문이나 비밀값을 되돌려 보내지 않는다."""
        return JSONResponse(status_code=422, content={"error": {"code": "validation_error", "message": "요청 형식을 확인해 주세요."}})

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request, error):
        """DB 접속 오류를 연결 정보 없이 서비스 준비 오류로 반환한다."""
        return JSONResponse(status_code=503, content={"error": {"code": "database_unavailable", "message": "DB 연결과 마이그레이션을 확인해 주세요."}})

    @app.get("/api/health", response_model=HealthResponse, tags=["system"], summary="API와 DB 준비 상태 확인")
    def health():
        """DB 연결 및 필수 수집 스키마가 준비됐는지 확인한다."""
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            if not inspect(connection).has_table("source") or "sync_state" not in {
                column["name"] for column in inspect(connection).get_columns("source")
            }:
                raise HTTPException(503, detail={"code": "migration_required", "message": "최신 마이그레이션을 적용해 주세요."})
        return HealthResponse()

    @app.get("/api/catalog/candidates", response_model=CandidateList, tags=["catalog"], summary="발견한 현재 후보 목록")
    def list_candidates(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
                        kind: str | None = Query(None, pattern="^(skill|repo)$")):
        """정보 제공 상태의 최신 후보를 유형별로 필터링하고 페이지 단위로 반환한다."""
        with sessions() as session:
            rows = candidate_rows(session)
            if kind:
                rows = [row for row in rows if row[0].kind == kind]
            return CandidateList(items=[serialize_candidate(*row) for row in rows[offset:offset + limit]],
                                 total=len(rows), limit=limit, offset=offset)

    @app.get("/api/catalog/sources", response_model=SourceList, tags=["catalog"], summary="수집 소스 상태 목록")
    def list_sources():
        """공개 소스별 마지막 수집 결과를 반환하며 인증 정보는 포함하지 않는다."""
        with sessions() as session:
            return SourceList(items=[SourceSummary(source_id=source.id, kind=source.kind, url=source.url,
                enabled=source.enabled, last_success_at=source.last_success_at, last_error=source.last_error)
                for source in session.scalars(select(Source).order_by(Source.id))])

    return app


app = create_app()
