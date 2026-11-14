from fastapi.testclient import TestClient

from backend.main import create_app
from backend.seed import seed
from backend.models import Comparison, EventLog, WorkMethod
from backend.models import Environment
from sqlalchemy import select


def test_comparison_routes_require_token_and_expose_fixture_suggestion(database):
    engine, sessions, _ = database
    with sessions.begin() as session:
        seed(session)
    with TestClient(create_app(database_url=str(engine.url), local_token="test-token")) as client:
        assert client.get("/api/inputs/suggestions?task_type=code_review").status_code == 401
        response = client.get("/api/inputs/suggestions?task_type=code_review",
            headers={"X-Local-Token": "test-token"})
        assert response.status_code == 200
        assert response.json()[0]["kind"] == "fixture"
        assert response.json()[0]["excluded_paths"] == []


def test_comparison_creation_rejects_information_only_candidate(database):
    engine, sessions, _ = database
    with sessions.begin() as session:
        seed(session)
    with TestClient(create_app(database_url=str(engine.url), local_token="test-token")) as client:
        response = client.post("/api/comparisons", headers={"X-Local-Token": "test-token"},
            json={"method_id":"missing", "method_version":1,
                  "candidate_version_id":"demo_ver_01", "input_ids":["fixture-review-01"],
                  "task_type":"code_review", "confirmed_upload_scope": True,
                  "idempotency_key":"one"})
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "work_method_not_found"


def test_completed_comparison_records_user_decision_and_history(database):
    engine, sessions, _ = database
    with sessions.begin() as session:
        seed(session)
        session.add(WorkMethod(id="wm_1", version=1, profile_id="local", task_type="code_review",
            prompt_text="review", is_active=True))
        session.flush()
        session.add(Comparison(id="cmp_1", profile_id="local", method_id="wm_1", method_version=1,
            candidate_version_id="demo_ver_01", input_manifest=[{"input_id":"fixture-review-01"}],
            input_sha256="a" * 64, plan={"model_id":"fixed"}, status="done"))
    headers = {"X-Local-Token": "test-token"}
    with TestClient(create_app(database_url=str(engine.url), local_token="test-token")) as client:
        assert client.get("/api/comparisons/cmp_1", headers=headers).json()["verdict"] is None
        response = client.patch("/api/comparisons/cmp_1/decision", headers=headers,
            json={"choice":"candidate", "reason":"결과가 더 명확함"})
        assert response.status_code == 200
        assert client.get("/api/comparisons/cmp_1", headers=headers).json()["verdict"] == "candidate"
        assert client.get("/api/history", headers=headers).json()[0]["payload"]["reason"] == "결과가 더 명확함"


def test_register_input_rejects_path_escape(database, tmp_path):
    engine, sessions, _ = database
    root = tmp_path / "approved"
    root.mkdir()
    (root / "app.py").write_text("pass\n")
    with sessions.begin() as session:
        seed(session)
        session.add(Environment(id="env_1", profile_id="local", runner="codex_cli",
            runner_version="test", model_id="fixed", installed_skills=[],
            settings_hash="x", scan_root=str(root)))
    headers = {"X-Local-Token": "test-token"}
    with TestClient(create_app(database_url=str(engine.url), local_token="test-token")) as client:
        invalid = client.post("/api/inputs", headers=headers,
            json={"relative_paths": ["../outside.py"], "task_type": "code_review"})
        assert invalid.status_code == 422
        valid = client.post("/api/inputs", headers=headers,
            json={"relative_paths": ["app.py"], "task_type": "code_review"})
        assert valid.status_code == 201
        assert valid.json()["input_id"].startswith("input_")
