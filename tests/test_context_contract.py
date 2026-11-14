"""개인 맥락의 저장과 불변 작업 방식 및 기존 DB 업그레이드를 검증한다."""
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from backend.main import create_app
from backend.models import Profile, WorkMethod

HEADERS = {"X-Local-Token": "test-context-token"}
PROFILE = {"role": "백엔드 개발", "primary_language": "python", "task_types": ["code_review"],
    "goals": ["api_security"], "context": {"frameworks": ["FastAPI"], "tools": ["Codex CLI"],
        "focus_areas": ["input_validation"]}}
METHOD = {"task_type": "code_review", "prompt_text": "인증과 입력 검증을 확인한다.",
    "workflow": [{"id": "security", "label": "권한 누락 확인", "capabilities": ["api_security"],
        "status": "gap", "weight": 5}]}


def test_context_and_workflow_roundtrip_and_versions(database):
    """기술 맥락을 정규화하고 절차 변경은 과거 버전을 덮어쓰지 않는다."""
    with TestClient(create_app(database_url=database[2].attributes["database_url"], local_token="test-context-token")) as api:
        first = api.put("/api/profile", headers=HEADERS, json=PROFILE).json()
        assert first["profile_version"] == 1
        fetched = api.get("/api/profile", headers=HEADERS).json()
        assert fetched["context"]["frameworks"] == ["fastapi"]
        assert fetched["context"]["tools"] == ["codex cli"]
        assert api.put("/api/profile", headers=HEADERS, json=PROFILE).json()["recommendations_stale"] is False
        changed = api.put("/api/profile", headers=HEADERS, json={**PROFILE,
            "context": {**PROFILE["context"], "frameworks": ["django"]}}).json()
        assert changed["profile_version"] == 2 and changed["recommendations_stale"]
        original = api.post("/api/work-methods", headers=HEADERS, json=METHOD).json()
        assert original["workflow"] == METHOD["workflow"]
        updated = api.post("/api/work-methods", headers=HEADERS, json={**METHOD, "workflow": [
            {**METHOD["workflow"][0], "status": "covered"}]}).json()
        assert updated["version"] == 2 and updated["method_id"] == original["method_id"]
        old = api.get(f"/api/work-methods/{original['method_id']}?version=1", headers=HEADERS).json()
        assert old["workflow"][0]["status"] == "gap" and old["is_active"] is False
        invalid = {**METHOD, "workflow": METHOD["workflow"] * 2}
        assert api.post("/api/work-methods", headers=HEADERS, json=invalid).status_code == 422
        assert api.put("/api/profile", headers=HEADERS, json={**PROFILE,
            "context": {"tools": ["https://private.example/token"]}}).status_code == 422


def test_context_migration_preserves_legacy_user_and_method(empty_database):
    """기존 l 스키마 데이터의 값과 버전을 보존하고 새 맥락만 빈 값으로 채운다."""
    engine, sessions, config = empty_database
    command.upgrade(config, "l20261115a01")
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO profile (id,role,primary_language,task_types_json,goals_json,preferences_json,version,updated_at) "
            "VALUES ('legacy','developer','python','[\"code_review\"]','[]','{}',7,'2026-11-15T00:00:00Z')"))
        connection.execute(text("INSERT INTO work_method (id,version,profile_id,task_type,prompt_text,required_checks_json,scoring_preferences_json,is_active,created_at) "
            "VALUES ('old',3,'legacy','code_review','preserve me','[]','{}',true,'2026-11-15T00:00:00Z')"))
    command.upgrade(config, "head")
    with sessions() as session:
        profile = session.get(Profile, "legacy")
        method = session.get(WorkMethod, ("old", 3))
        assert profile.version == 7 and profile.context_json == {} and profile.task_types == ["code_review"]
        assert method.prompt_text == "preserve me" and method.workflow_json == [] and method.is_active
        assert session.scalar(select(WorkMethod.version)) == 3


def test_legacy_profile_update_keeps_context_until_explicit_reset(database):
    """새 필드를 모르는 기존 프론트의 수정이 등록한 기술과 제약을 잃지 않게 한다."""
    with TestClient(create_app(database_url=database[2].attributes["database_url"], local_token="test-context-token")) as api:
        assert api.put("/api/profile", headers=HEADERS, json=PROFILE).status_code == 200
        before = api.get("/api/profile", headers=HEADERS).json()["context"]
        legacy = {key: value for key, value in PROFILE.items() if key != "context"}
        assert api.put("/api/profile", headers=HEADERS, json={**legacy, "goals": ["test_coverage"]}).status_code == 200
        assert api.get("/api/profile", headers=HEADERS).json()["context"] == before
        assert api.put("/api/profile", headers=HEADERS, json={**legacy, "context": {}}).status_code == 200
        assert api.get("/api/profile", headers=HEADERS).json()["context"]["frameworks"] == []
