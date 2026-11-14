from fastapi.testclient import TestClient

from backend.main import create_app

HEADERS = {"X-Local-Token": "test-local-token"}
PROFILE = {"role": "developer", "primary_language": "python", "task_types": ["code_review", "test_generation"]}
METHOD = {"task_type": "code_review", "prompt_text": "Review validation.",
          "required_checks": ["known_regressions_not_missed"], "scoring_preferences": {"correctness": "required"}}


def test_work_method_versions_and_active_list(database):
    """작업 방식 재등록 시 이전 내용과 ID를 보존하고 활성 버전만 전환하는지 확인한다."""
    engine, _, _ = database
    with TestClient(create_app(database_url=str(engine.url), local_token="test-local-token")) as client:
        assert client.post("/api/work-methods", headers=HEADERS, json=METHOD).status_code == 404
        client.put("/api/profile", headers=HEADERS, json=PROFILE)
        first = client.post("/api/work-methods", headers=HEADERS, json=METHOD)
        assert first.status_code == 201
        method_id = first.json()["method_id"]
        second = client.post("/api/work-methods", headers=HEADERS, json={**METHOD, "prompt_text": "Review errors."}).json()
        assert second["method_id"] == method_id and second["version"] == 2
        assert client.get("/api/work-methods", headers=HEADERS).json() == [second]
        original = client.get(f"/api/work-methods/{method_id}?version=1", headers=HEADERS).json()
        assert original["prompt_text"] == METHOD["prompt_text"] and not original["is_active"]
        assert len(client.get("/api/work-methods?active_only=false", headers=HEADERS).json()) == 2
        assert client.get(f"/api/work-methods/{method_id}?version=99", headers=HEADERS).status_code == 404


def test_tasks_have_separate_active_methods_and_auth(database):
    """작업 유형별 활성 방식과 필수 인증, OpenAPI 배열 계약을 검증한다."""
    engine, _, _ = database
    with TestClient(create_app(database_url=str(engine.url), local_token="test-local-token")) as client:
        client.put("/api/profile", headers=HEADERS, json=PROFILE)
        client.post("/api/work-methods", headers=HEADERS, json=METHOD)
        client.post("/api/work-methods", headers=HEADERS, json={**METHOD, "task_type": "test_generation"})
        assert len(client.get("/api/work-methods", headers=HEADERS).json()) == 2
        assert client.get("/api/work-methods").status_code == 401
        schema = client.get("/openapi.json").json()
        response_schema = schema["paths"]["/api/work-methods"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        assert response_schema["type"] == "array"
