from fastapi.testclient import TestClient

from backend.main import create_app
from backend.seed import seed


def test_docs_and_catalog_contract(database):
    """문서 스키마와 실제 카탈로그 응답이 일치하는지 확인한다."""
    engine, sessions, _ = database
    with sessions.begin() as session:
        seed(session)
    with TestClient(create_app(database_url=str(engine.url))) as client:
        assert client.get("/docs").status_code == 200
        schema = client.get("/openapi.json").json()
        assert "/api/catalog/candidates" in schema["paths"]
        assert "/api/recommendations" not in schema["paths"]
        assert client.get("/api/health").json() == {"status": "ok", "database": "ready"}
        response = client.get("/api/catalog/candidates?limit=3")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 10 and len(data["items"]) == 3
        assert all(item["demo"] and item["support_level"] == "information_only" for item in data["items"])
        assert "bundle" not in data["items"][0]
        assert client.get("/api/catalog/sources").json()["items"][0]["source_id"] == "demo"


def test_validation_errors_and_cors(database):
    """잘못된 입력과 CORS를 프론트 계약에 맞게 처리하는지 확인한다."""
    engine, _, _ = database
    with TestClient(create_app(database_url=str(engine.url), cors_origins=["http://localhost:5173"])) as client:
        response = client.get("/api/catalog/candidates?limit=0")
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"
        allowed = client.get("/api/health", headers={"Origin": "http://localhost:5173"})
        assert allowed.headers["access-control-allow-origin"] == "http://localhost:5173"
        denied = client.get("/api/health", headers={"Origin": "https://unknown.example"})
        assert "access-control-allow-origin" not in denied.headers


def test_unmigrated_database_returns_actionable_error(tmp_path):
    """빈 DB에 테이블을 자동 생성하지 않고 마이그레이션 필요 오류를 반환한다."""
    with TestClient(create_app(database_url=f"sqlite:///{(tmp_path / 'empty.db').as_posix()}")) as client:
        response = client.get("/api/health")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "migration_required"
