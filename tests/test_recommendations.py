from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.main import create_app
from backend.models import Recommendation, RecommendationDecision
from backend.seed import seed

HEADERS = {"X-Local-Token": "test-local-token"}


def test_recommendations_follow_task_and_keep_user_decisions(database):
    """현재 작업 필터와 사용자 선택이 재계산 및 API 재시작 후에도 유지되는지 검증한다."""
    engine, sessions, _ = database
    with sessions.begin() as session:
        seed(session)
    profile = {"role": "developer", "primary_language": "python", "task_types": ["code_review"],
               "goals": ["input_validation"]}
    with TestClient(create_app(database_url=engine.url.render_as_string(hide_password=False), local_token="test-local-token")) as client:
        client.put("/api/profile", headers=HEADERS, json=profile)
        client.post("/api/work-methods", headers=HEADERS,
                    json={"task_type": "code_review", "prompt_text": "Review changed code"})
        response = client.get("/api/recommendations?task_type=code_review", headers=HEADERS)
        assert response.status_code == 200
        items = response.json()["items"]
        assert len(items) == 5
        assert all(item["work_method_id"] and item["assessment"]["matches"]["goals"] == ["input_validation"]
                   and "goal_fit" in item["assessment"]["breakdown"] for item in items)
        assert all(item["latest_comparison"] is None and item["performance_status"] == "not_compared" for item in items)
        assert all(item["support_level"] == "information_only" and item["demo"] for item in items)
        rec_id = items[0]["recommendation_id"]
        assert client.patch(f"/api/recommendations/{rec_id}", headers=HEADERS,
                            json={"status": "kept", "reason": "기존 방식 유지"}).status_code == 204
        assert len(client.get("/api/recommendations", headers=HEADERS).json()["items"]) == 4
    with TestClient(create_app(database_url=engine.url.render_as_string(hide_password=False), local_token="test-local-token")) as client:
        hidden = client.get("/api/recommendations?include_hidden=true", headers=HEADERS).json()["items"]
        assert len(hidden) == 5
        assert next(item for item in hidden if item["recommendation_id"] == rec_id)["status"] == "kept"
        assert client.patch(f"/api/recommendations/{rec_id}", headers=HEADERS,
                            json={"status": "new"}).status_code == 204
        assert len(client.get("/api/recommendations", headers=HEADERS).json()["items"]) == 5
    with sessions() as session:
        assert session.get(RecommendationDecision, rec_id).status == "new"
        assert session.scalar(select(func.count()).select_from(Recommendation)) == 5


def test_snooze_dismiss_validation_and_auth(database):
    """숨김 상태와 없는 추천, 잘못된 상태 및 인증 오류를 구분한다."""
    engine, sessions, _ = database
    with sessions.begin() as session:
        seed(session)
    with TestClient(create_app(database_url=engine.url.render_as_string(hide_password=False), local_token="test-local-token")) as client:
        assert client.get("/api/recommendations", headers=HEADERS).status_code == 200
        assert client.get("/api/recommendations").status_code == 401
        client.put("/api/profile", headers=HEADERS, json={"role": "dev", "primary_language": "python",
                    "task_types": ["test_generation"]})
        items = client.get("/api/recommendations", headers=HEADERS).json()["items"]
        assert len(items) == 5
        rec_id = items[0]["recommendation_id"]
        for status in ("snoozed", "dismissed"):
            assert client.patch(f"/api/recommendations/{rec_id}", headers=HEADERS,
                                json={"status": status}).status_code == 204
            assert len(client.get("/api/recommendations", headers=HEADERS).json()["items"]) == 4
        assert client.patch("/api/recommendations/missing", headers=HEADERS,
                            json={"status": "new"}).status_code == 404
        assert client.patch(f"/api/recommendations/{rec_id}", headers=HEADERS,
                            json={"status": "applied"}).status_code == 422
        schema = client.get("/openapi.json").json()
        assert "/api/recommendations/{recommendation_id}" in schema["paths"]
