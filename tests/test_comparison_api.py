from fastapi.testclient import TestClient
from pathlib import Path
from types import SimpleNamespace
import pytest

from backend.main import create_app
from backend.seed import seed
from backend.models import AssetVersion, CandidateAdapter, Comparison, EventLog, Run, WorkMethod
from backend.models import Environment
from sqlalchemy import select
import backend.comparisons as comparison_routes


def test_comparison_routes_require_token_and_expose_fixture_suggestion(database):
    engine, sessions, _ = database
    with sessions.begin() as session:
        seed(session)
    with TestClient(create_app(database_url=engine.url.render_as_string(hide_password=False), local_token="test-token")) as client:
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
    with TestClient(create_app(database_url=engine.url.render_as_string(hide_password=False), local_token="test-token")) as client:
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
    with TestClient(create_app(database_url=engine.url.render_as_string(hide_password=False), local_token="test-token")) as client:
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
    with TestClient(create_app(database_url=engine.url.render_as_string(hide_password=False), local_token="test-token")) as client:
        invalid = client.post("/api/inputs", headers=headers,
            json={"relative_paths": ["../outside.py"], "task_type": "code_review"})
        assert invalid.status_code == 422
        valid = client.post("/api/inputs", headers=headers,
            json={"relative_paths": ["app.py"], "task_type": "code_review"})
        assert valid.status_code == 201
        assert valid.json()["input_id"].startswith("input_")


@pytest.mark.parametrize("input_count", [2, 3])
def test_comparison_keeps_inputs_separate_by_arm(database, tmp_path, monkeypatch, input_count):
    engine, sessions, _ = database
    monkeypatch.setattr(comparison_routes, "ROOT", tmp_path)
    root = tmp_path / "approved"
    root.mkdir()
    (root / "first.py").write_text("print('first')\n")
    (root / "second.py").write_text("print('second')\n")
    (root / "third.py").write_text("print('third')\n")
    seen = []

    def fake_execute_comparison(**kwargs):
        name = kwargs["selected_paths"][0].name
        seen.append((name, kwargs["output_root"], kwargs["expected_input_sha256"]))
        def arm(label):
            return SimpleNamespace(status="completed", skill_usage_status=("confirmed" if label == "candidate" else "not_expected"),
                final_result={"summary": f"{label}:{name}", "findings": []},
                output_path=kwargs["output_root"] / label / "events.jsonl", seconds=1.5)
        return SimpleNamespace(baseline=arm("baseline"), candidate=arm("candidate"))

    monkeypatch.setattr(comparison_routes, "execute_comparison", fake_execute_comparison)
    with sessions.begin() as session:
        seed(session)
        session.add(WorkMethod(id="wm_multi", version=1, profile_id="local", task_type="code_review",
            prompt_text="review", is_active=True))
        session.add(Environment(id="env_multi", profile_id="local", runner="codex_cli",
            runner_version="test", model_id="fixed", installed_skills=[],
            settings_hash="x", scan_root=str(root)))
        version = session.get(AssetVersion, "demo_ver_01")
        version.artifact_path = str(tmp_path / "candidate")
        session.add(CandidateAdapter(id="adapter_multi", asset_version_id=version.id, runner="codex_cli",
            supported_tasks=["code_review"], approved_bundle_sha256=version.bundle_sha256,
            support_level="comparable", reviewed=True))

    headers = {"X-Local-Token": "test-token"}
    with TestClient(create_app(database_url=engine.url.render_as_string(hide_password=False), local_token="test-token")) as client:
        ids = [client.post("/api/inputs", headers=headers,
            json={"relative_paths": [name], "task_type": "code_review"}).json()["input_id"]
            for name in ("first.py", "second.py", "third.py")[:input_count]]
        body = {"method_id": "wm_multi", "method_version": 1, "candidate_version_id": "demo_ver_01",
            "task_type": "code_review", "input_ids": ids, "confirmed_upload_scope": True,
            "idempotency_key": "two-inputs"}
        response = client.post("/api/comparisons", headers=headers, json=body)
        assert response.status_code == 202
        detail = client.get(f"/api/comparisons/{response.json()['comparison_id']}", headers=headers).json()
        assert detail["status"] == "done"
        assert [item["input_id"] for item in detail["inputs"]] == ids
        assert len(seen) == input_count
        assert seen[0][1] != seen[1][1]
        assert {run["arm"] for run in detail["runs"]} == {"baseline", "candidate"}
        for run in detail["runs"]:
            assert [item["input_id"] for item in run["inputs"]] == ids
            assert len({item["event_log_path"] for item in run["inputs"]}) == input_count
            assert [item["result"]["summary"] for item in run["inputs"]] == [
                f"{run['arm']}:{name}" for name in ("first.py", "second.py", "third.py")[:input_count]]
        duplicate = client.post("/api/comparisons", headers=headers,
            json={**body, "input_ids": [ids[0], ids[0]], "idempotency_key": "duplicate-inputs"})
        assert duplicate.status_code == 422

        def fail_on_second(**kwargs):
            if kwargs["selected_paths"][0].name == "second.py":
                raise ValueError("changed input")
            return fake_execute_comparison(**kwargs)

        monkeypatch.setattr(comparison_routes, "execute_comparison", fail_on_second)
        failed = client.post("/api/comparisons", headers=headers,
            json={**body, "idempotency_key": "second-fails"})
        failed_detail = client.get(f"/api/comparisons/{failed.json()['comparison_id']}", headers=headers).json()
        assert failed_detail["status"] == "failed"
        assert [item["status"] for item in failed_detail["runs"][0]["inputs"]] == [
            "completed", *(["failed"] * (input_count - 1))]
        assert client.patch(f"/api/comparisons/{failed.json()['comparison_id']}/decision", headers=headers,
            json={"choice": "candidate", "reason": "not complete"}).status_code == 409

        def candidate_fails_on_second(**kwargs):
            result = fake_execute_comparison(**kwargs)
            if kwargs["selected_paths"][0].name == "second.py":
                result.candidate.status = "failed"
            return result

        monkeypatch.setattr(comparison_routes, "execute_comparison", candidate_fails_on_second)
        partial = client.post("/api/comparisons", headers=headers,
            json={**body, "idempotency_key": "candidate-fails"})
        partial_detail = client.get(f"/api/comparisons/{partial.json()['comparison_id']}", headers=headers).json()
        assert partial_detail["status"] == "failed"
        by_arm = {run["arm"]: run for run in partial_detail["runs"]}
        assert by_arm["baseline"]["status"] == "completed"
        assert by_arm["candidate"]["status"] == "failed"
        assert by_arm["candidate"]["inputs"][1]["status"] == "failed"
    with sessions() as session:
        assert len(session.scalars(select(Run).where(
            Run.comparison_id == response.json()["comparison_id"])).all()) == 2
