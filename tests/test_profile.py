import pytest
from fastapi.testclient import TestClient

from backend.main import create_app

PROFILE = {"role": "백엔드 개발", "primary_language": "python", "task_types": ["code_review"],
           "goals": ["input_validation"], "preferences": {"quality_required": True}}
HEADERS = {"X-Local-Token": "test-local-token"}


def test_onboarding_roundtrip_and_idempotent_save(database):
    """미등록, 저장, 재조회와 동일 요청의 버전 유지 및 실제 수정의 증가를 검증한다."""
    engine, _, _ = database
    with TestClient(create_app(database_url=engine.url.render_as_string(hide_password=False), local_token="test-local-token")) as client:
        assert client.get("/api/profile", headers=HEADERS).status_code == 404
        saved = client.put("/api/profile", headers=HEADERS, json=PROFILE)
        assert saved.status_code == 200
        assert saved.json() == {"profile_id": "local", "profile_version": 1, "recommendations_stale": True}
        fetched = client.get("/api/profile", headers=HEADERS).json()
        assert all(fetched[key] == value for key, value in PROFILE.items())
        assert fetched["updated_at"].endswith("Z")
        repeated = client.put("/api/profile", headers=HEADERS, json=PROFILE).json()
        assert repeated["profile_version"] == 1 and not repeated["recommendations_stale"]
        updated = client.put("/api/profile", headers=HEADERS, json={**PROFILE, "primary_language": "typescript"}).json()
        assert updated["profile_version"] == 2
    with TestClient(create_app(database_url=engine.url.render_as_string(hide_password=False), local_token="test-local-token")) as client:
        assert client.get("/api/profile", headers=HEADERS).json()["primary_language"] == "typescript"


@pytest.mark.parametrize("headers", [{}, {"X-Local-Token": "wrong-token"}])
def test_profile_requires_correct_token(database, headers):
    """토큰 누락과 불일치가 프로필을 읽거나 쓰지 못하게 막는지 검증한다."""
    engine, _, _ = database
    with TestClient(create_app(database_url=engine.url.render_as_string(hide_password=False), local_token="test-local-token")) as client:
        for response in (client.get("/api/profile", headers=headers), client.put("/api/profile", headers=headers, json=PROFILE)):
            assert response.status_code == 401
            assert response.json()["error"]["code"] == "invalid_local_token"
            assert "test-local-token" not in response.text


def test_missing_server_token_closes_private_api(database):
    """서버 토큰이 설정되지 않았을 때 개인 API를 무인증으로 열지 않는지 확인한다."""
    engine, _, _ = database
    with TestClient(create_app(database_url=engine.url.render_as_string(hide_password=False), local_token="")) as client:
        response = client.get("/api/profile", headers=HEADERS)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "local_token_not_configured"


def test_profile_schema_security_and_preflight(database):
    """프론트용 헤더 사전 요청과 OpenAPI 보안 계약 및 잘못된 입력을 검증한다."""
    engine, _, _ = database
    with TestClient(create_app(database_url=engine.url.render_as_string(hide_password=False), local_token="test-local-token")) as client:
        preflight = client.options("/api/profile", headers={"Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "PUT", "Access-Control-Request-Headers": "X-Local-Token,Content-Type"})
        assert preflight.status_code == 200
        schema = client.get("/openapi.json").json()
        assert schema["paths"]["/api/profile"]["get"]["security"]
        invalid = client.put("/api/profile", headers=HEADERS, json={**PROFILE, "profile_id": "other-user"})
        assert invalid.status_code == 422
        assert invalid.json()["error"]["code"] == "validation_error"
