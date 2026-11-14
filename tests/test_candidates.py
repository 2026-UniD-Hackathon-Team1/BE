import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.main import create_app
from backend.models import (AgentDevice, Asset, AssetVersion, CandidateAdapter, ChangeEvent,
                            Environment, Profile, Recommendation, Source, utc_now)
from backend.seed import seed

HEADERS = {"X-Local-Token": "test-local-token"}


@pytest.fixture
def candidate(database):
    """수집 원문과 같은 고정 번들 및 출처로 후보 상세 API를 준비한다."""
    files = {"SKILL.md": b"---\nname: review\nallowed-tools: [Read, Bash(curl TOP_SECRET), WebFetch]\n---\nReview code.",
             "scripts/check.py": b"PRIVATE_SCRIPT_BODY"}
    bundle = {"files": {path: base64.b64encode(content).decode() for path, content in files.items()}}
    digest = hashlib.sha256(json.dumps(bundle, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
    source_url = "https://github.com/example/review/tree/" + "a" * 40 + "/skills/review"
    with database[1].begin() as session:
        session.add(Source(id="github:example/review", kind="github_repo", url="https://github.com/example/review",
            sync_state={"current_versions": {"asset-real": "ver-real"}}))
        session.flush()
        session.add(Asset(id="asset-real", kind="skill", canonical_key="github:example/review:skills/review",
            display_name="Review candidate", source_id="github:example/review"))
        session.flush()
        session.add(AssetVersion(id="ver-real", asset_id="asset-real", revision="a" * 40, bundle_sha256=digest,
            artifact_path="C:/TOP_SECRET/local-bundle", metadata_json={"source_verified": True, "source_url": source_url,
                "bundle": bundle, "repository": {"description": "Declared code review utility",
                    "releases": [{"published_at": "2026-11-01T00:00:00Z"}]}},
            tags={"task_types": ["code_review"], "languages": ["unknown"],
                "evidence": [{"category": "task_types", "tag": "code_review", "path": "SKILL.md", "line": 6, "excerpt": "Review code."}]}))
        session.flush()
        session.add(ChangeEvent(id="change-real", asset_id="asset-real", to_version_id="ver-real",
            change_type="new", source_url=source_url))
    with TestClient(create_app(database_url=database[2].attributes["database_url"], local_token="test-local-token")) as client:
        client.put("/api/profile", headers=HEADERS, json={"role": "developer", "primary_language": "python", "task_types": ["code_review"]})
        yield client, database[1]


def add_adapter(sessions, *, reviewed=True, digest=None, name="adapter-real", level="comparable"):
    """실제 실행 없이 검토된 어댑터 레코드의 조건만 구성한다."""
    with sessions.begin() as session:
        version = session.get(AssetVersion, "ver-real")
        session.add(CandidateAdapter(id=name, asset_version_id=version.id, runner="codex_cli",
            supported_tasks=["code_review"], approved_bundle_sha256=digest or version.bundle_sha256,
            reviewed=reviewed, support_level=level,
            permissions={"file_read": True, "private_path": "C:/TOP_SECRET", "network": ["https://TOP_SECRET"]}, runtime={}))


def test_candidate_contract_auth_unknowns_and_no_private_payload(candidate):
    """출처와 권한 계약을 제공하되 공식성 및 개선 효과를 추측하거나 원문을 노출하지 않는다."""
    client, _ = candidate
    response = client.get("/api/candidates/ver-real", headers=HEADERS)
    assert response.status_code == 200
    item = response.json()
    assert item["provenance"] == "community" and item["source_verification"] == "verified"
    assert item["integrity_status"] == "verified" and item["is_current"] is True
    assert item["support_level"] == "information_only" and item["adapter_id"] is None
    assert item["published_at"] is None and item["verified_source_at"]
    assert item["replaces"] is None and item["claimed_benefit"] is None
    assert item["maintainer"] == {"author": None, "license": None, "last_updated_at": None}
    assert item["permissions"]["verification"] == "declared"
    assert {value["kind"] for value in item["permissions"]["declared"]} == {"file_read", "command_exec", "network"}
    assert all(value["additional"] is None for value in item["permissions"]["declared"])
    assert all(value["evidence"][0]["source_url"].endswith("/SKILL.md#L3") for value in item["permissions"]["declared"])
    assert "declared_command_exec" in item["risk_signal_codes"]
    assert "TOP_SECRET" not in response.text and "PRIVATE_SCRIPT_BODY" not in response.text
    assert "bundle" not in item and "artifact_path" not in item
    assert client.get("/api/candidates/ver-real").status_code == 401
    assert client.get("/api/candidates/missing", headers=HEADERS).status_code == 404
    schema = client.get("/openapi.json").json()
    operation = schema["paths"]["/api/candidates/{version_id}"]["get"]
    assert operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith("CandidateDetail")
    assert operation["security"] == [{"LoginSession": []}, {"APIKeyHeader": []}]


@pytest.mark.parametrize("reviewed,digest,status", [(False, None, "unreviewed"), (True, "0" * 64, "hash_mismatch")])
def test_unreviewed_or_wrong_hash_adapter_cannot_enable_execution(candidate, reviewed, digest, status):
    """미검토 또는 다른 콘텐츠 해시의 어댑터를 실행 지원으로 승격하지 않는다."""
    client, sessions = candidate
    add_adapter(sessions, reviewed=reviewed, digest=digest, level="applicable")
    item = client.get("/api/candidates/ver-real", headers=HEADERS).json()
    assert item["support_level"] == "information_only" and item["adapter_id"] is None
    assert item["adapter_review_status"] == status
    assert client.get("/api/recommendations", headers=HEADERS).json()["items"][0]["support_level"] == "information_only"


def test_reviewed_adapter_does_not_imply_ready_or_audited_environment(candidate):
    """검토된 실행 지원과 현재 환경 호환성 및 보안 감사를 구분한다."""
    client, sessions = candidate
    add_adapter(sessions)
    item = client.get("/api/candidates/ver-real", headers=HEADERS).json()
    assert item["support_level"] == "comparable" and item["adapter_id"] == "adapter-real"
    assert item["compatibility"] == "unknown" and item["security_audit_status"] == "unverified"
    assert item["permissions"]["allowed"] == {"file_read": True}
    with sessions.begin() as session:
        session.add(AgentDevice(id="device-real", profile_id="local", token_hash="fixture-hash", device_name="fixture",
            agent_version="test", is_primary=True, codex_version="test-version", codex_ready=True, last_seen_at=utc_now()))
        session.flush()
        session.add(Environment(id="env-real", profile_id="local", device_id="device-real", runner="codex_cli",
            runner_version="test-version", model_id="test-model", installed_skills=[], settings_hash="fixture", scan_root="agent://fixture"))
    assert client.get("/api/candidates/ver-real", headers=HEADERS).json()["compatibility"] == "confirmed"
    with sessions.begin() as session:
        session.get(AgentDevice, "device-real").last_seen_at = (datetime.now(timezone.utc) - timedelta(seconds=61)).isoformat(timespec="microseconds").replace("+00:00", "Z")
    assert client.get("/api/candidates/ver-real", headers=HEADERS).json()["compatibility"] == "unknown"
    with sessions.begin() as session:
        session.get(AgentDevice, "device-real").last_seen_at = utc_now()
        session.get(Environment, "env-real").runner = "other_runner"
    assert client.get("/api/candidates/ver-real", headers=HEADERS).json()["compatibility"] == "incompatible"
    with sessions.begin() as session:
        session.get(AgentDevice, "device-real").revoked_at = utc_now()
    assert client.get("/api/candidates/ver-real", headers=HEADERS).json()["compatibility"] == "unknown"


def test_tampering_and_demo_never_enable_execution(candidate):
    """원문 변조와 데모 후보는 상세 및 추천 모두 정보 제공 상태로 유지한다."""
    client, sessions = candidate
    add_adapter(sessions, level="applicable")
    with sessions.begin() as session:
        version = session.get(AssetVersion, "ver-real")
        version.metadata_json = {**version.metadata_json, "bundle": {"files": {"SKILL.md": "dGFtcGVyZWQ="}}}
    item = client.get("/api/candidates/ver-real", headers=HEADERS).json()
    assert item["integrity_status"] == "mismatch" and item["support_level"] == "information_only"
    assert item["permissions"]["declared"] == []
    assert client.get("/api/recommendations", headers=HEADERS).json()["items"][0]["support_level"] == "information_only"
    with sessions.begin() as session:
        seed(session)
        version = session.get(AssetVersion, "demo_ver_01")
        session.add(CandidateAdapter(id="demo-adapter", asset_version_id=version.id, runner="codex_cli",
            supported_tasks=["code_review"], reviewed=True, support_level="applicable", approved_bundle_sha256=version.bundle_sha256))
    demo = client.get("/api/candidates/demo_ver_01", headers=HEADERS).json()
    assert demo["source_verification"] == "demo" and demo["verified_source_at"] is None
    assert demo["support_level"] == "information_only" and "demo_candidate" in demo["risk_signal_codes"]


def test_historical_version_is_not_replaced_by_current_version(candidate):
    """상세 URL의 고정 버전을 유지하고 공개일을 최신 릴리스 날짜로 추측하지 않는다."""
    client, sessions = candidate
    with sessions.begin() as session:
        source = session.get(Source, "github:example/review")
        source.sync_state = {"current_versions": {"asset-real": "ver-new"}}
        session.add(AssetVersion(id="ver-new", asset_id="asset-real", revision="b" * 40,
            bundle_sha256="1" * 64, metadata_json={}, tags={}))
    item = client.get("/api/candidates/ver-real", headers=HEADERS).json()
    assert item["candidate_version_id"] == "ver-real" and item["revision"] == "a" * 40
    assert item["is_current"] is False and item["published_at"] is None
    assert "/tree/" + "a" * 40 in item["source_url"]


def test_multiple_reviewed_adapters_and_archived_sources_need_review(candidate):
    """여러 어댑터를 임의 선택하거나 보관된 원본을 자동 실행 지원으로 올리지 않는다."""
    client, sessions = candidate
    add_adapter(sessions)
    add_adapter(sessions, name="second-adapter", level="applicable")
    value = client.get("/api/candidates/ver-real", headers=HEADERS).json()
    assert value["adapter_review_status"] == "ambiguous" and value["support_level"] == "information_only"
    with sessions.begin() as session:
        session.delete(session.get(CandidateAdapter, "second-adapter"))
        version = session.get(AssetVersion, "ver-real")
        version.metadata_json = {**version.metadata_json, "repository": {"archived": True}}
    value = client.get("/api/candidates/ver-real", headers=HEADERS).json()
    assert value["support_level"] == "information_only" and "archived_repository" in value["risk_signal_codes"]


def test_related_reasons_only_come_from_current_profile(candidate):
    """공통 후보 상세에서 다른 사용자의 추천 이유를 노출하지 않는다."""
    client, sessions = candidate
    with sessions.begin() as session:
        session.add(Profile(id="other", role="other", primary_language="python", task_types=[], goals=[]))
        session.flush()
        session.add(Recommendation(id="other-rec", profile_id="other", change_id="change-real", score=1, reasons=["OTHER_PRIVATE_REASON"]))
    assert client.get("/api/candidates/ver-real", headers=HEADERS).json()["related_reasons"] == []
    with sessions.begin() as session:
        session.add(Recommendation(id="local-rec", profile_id="local", change_id="change-real", score=1, reasons=["현재 작업 유형과 관련 있음"]))
    response = client.get("/api/candidates/ver-real", headers=HEADERS)
    assert response.json()["related_reasons"] == ["현재 작업 유형과 관련 있음"]
    assert "OTHER_PRIVATE_REASON" not in response.text


def test_unsafe_source_url_is_not_returned_or_marked_verified(candidate):
    """인증 정보가 포함된 URL은 노출하지 않고 고정 원문 링크가 없으면 미확인으로 둔다."""
    client, sessions = candidate
    add_adapter(sessions)
    with sessions.begin() as session:
        version = session.get(AssetVersion, "ver-real")
        version.metadata_json = {**version.metadata_json, "source_url": "https://user:TOP_SECRET@github.com/example/review"}
        session.get(ChangeEvent, "change-real").source_url = "file:///TOP_SECRET/SKILL.md"
    response = client.get("/api/candidates/ver-real", headers=HEADERS)
    assert response.status_code == 200 and "TOP_SECRET" not in response.text
    assert response.json()["source_verification"] == "unverified"
    assert response.json()["support_level"] == "information_only"
