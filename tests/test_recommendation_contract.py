"""추천 관련성 응답과 OpenAPI의 중첩 계약이 실제 평가 결과와 일치하는지 확인한다."""
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.main import create_app
from backend.personalization import rank_candidate
from backend.schemas import (ContextEvidence, FreshnessSignal, RecommendationAssessment,
                             RecommendationCard, RecommendationMatches)
from backend.seed import seed


def assessment():
    """비공개 입력 대신 실제 평가 함수로 원문 근거와 공백 연결 응답을 만든다."""
    user = NS(id="contract", version=1, role="백엔드 개발", primary_language="python",
        task_types=["code_review"], goals=["api_security"], preferences={},
        context_json={"frameworks": ["fastapi"], "tools": ["codex_cli"]})
    method = NS(id="contract-method", version=1, task_type="code_review", is_active=True,
        prompt_text="비공개 API 인증 로직을 확인해줘.", required_checks=[], workflow_json=[
            {"id": "security", "label": "API 보안 확인", "capabilities": ["api_security"],
             "status": "gap", "weight": 5}])
    version = NS(id="contract-version", revision="a" * 40, bundle_sha256="b" * 64,
        metadata_json={}, tags={"task_types": ["code_review"], "dimensions": {
            "languages": ["python"], "frameworks": ["fastapi"], "tools": ["codex_cli"],
            "capabilities": ["api_security"], "evidence": [{"dimension": "capabilities",
                "value": "api_security", "path": "SKILL.md", "line": 2,
                "excerpt": "Reviews API security."}]}})
    return rank_candidate(user, {"code_review": method}, None, NS(id="contract-asset"),
        version, NS(source_url="https://github.com/example/review"),
        NS(enabled=True, kind="github_repo", url="https://github.com/example/review"),
        now=datetime(2026, 11, 15, tzinfo=timezone.utc))


def test_typed_assessment_preserves_actual_ranker_response():
    """원래 평가 응답을 필드 손실이나 의미 변경 없이 중첩 모델로 직렬화한다."""
    original = assessment()
    typed = RecommendationAssessment.model_validate(original)
    assert typed.model_dump() == original
    assert typed.eligible and typed.matches.confidence == "contextual"
    assert typed.workflow_fit[0].relation == "fills_gap"
    assert typed.workflow_fit[0].evidence[0].path == "SKILL.md"
    assert typed.freshness.status == "unknown" and typed.freshness.bonus == 0
    assert "비공개 API" not in typed.model_dump_json()


def test_assessment_defaults_are_conservative_and_card_compatible():
    """평가를 생략한 기존 카드 생성도 동작하며 실행이나 개선 확인을 암시하지 않는다."""
    card = RecommendationCard(recommendation_id="rec", candidate_version_id="version", adapter_id=None,
        work_method_id=None, work_method_version=None, title="후보", change_type="new", task_types=["code_review"],
        score=0, related_reasons=[], source_url="https://github.com/example/review", compatibility="unknown",
        support_level="information_only", status="new", reason=None, latest_comparison=None, demo=False)
    assert isinstance(card.assessment, RecommendationAssessment)
    assert not card.assessment.eligible
    assert card.assessment.matches.confidence == "limited"
    assert card.assessment.freshness.model_dump() == {"status": "unknown", "basis": None,
        "timestamp": None, "age_days": None, "bonus": 0, "is_release_date": False}


def test_openapi_describes_nested_assessment_instead_of_untyped_dictionary():
    """프론트가 점수와 공백 연결 및 원문 근거의 실제 타입을 Swagger에서 확인한다."""
    contract = create_app(database_url="postgresql://contract:contract@localhost/unused").openapi()
    models = contract["components"]["schemas"]
    assert models["RecommendationCard"]["properties"]["assessment"]["$ref"].endswith("/RecommendationAssessment")
    properties = models["RecommendationAssessment"]["properties"]
    assert properties["matches"]["$ref"].endswith("/RecommendationMatches")
    assert properties["workflow_fit"]["items"]["$ref"].endswith("/WorkflowFit")
    assert properties["freshness"]["$ref"].endswith("/FreshnessSignal")
    assert properties["evidence"]["items"]["$ref"].endswith("/ContextEvidence")
    assert "성능" in properties["score"]["description"]
    assert models["ContextEvidence"]["properties"]["dimension"]["enum"] == [
        "task_types", "capabilities", "languages", "frameworks", "tools"]
    assert models["FreshnessSignal"]["properties"]["status"]["enum"] == ["observed", "unknown"]
    assert "사용자가 직접 등록" in models["ProfileContext"]["properties"]["frameworks"]["description"]
    assert "검증" in models["WorkflowStep"]["properties"]["status"]["description"]


def test_confidence_and_freshness_never_accept_performance_confirmation_labels():
    """관련성 근거에 호환 확정이나 출시 및 성능 검증 상태가 섞이지 않게 한다."""
    with pytest.raises(ValidationError):
        RecommendationMatches(confidence="confirmed")
    with pytest.raises(ValidationError):
        FreshnessSignal(status="released")
    with pytest.raises(ValidationError):
        FreshnessSignal(is_release_date=True)
    with pytest.raises(ValidationError):
        RecommendationAssessment(score=101)
    with pytest.raises(ValidationError):
        ContextEvidence(dimension="capabilities", value="api_security", path="SKILL.md", line=0, excerpt="API security")


def test_negative_breakdown_and_future_match_fields_are_preserved():
    """기존 검사 중복 감점과 확장 가능한 매칭 정보의 값을 유지한다."""
    typed = RecommendationAssessment(breakdown={"workflow_overlap": -10},
        matches={"confidence": "limited", "additional_condition": "확인 필요"})
    assert typed.breakdown["workflow_overlap"] == -10
    assert typed.model_dump()["matches"]["additional_condition"] == "확인 필요"


def test_actual_recommendation_response_validates_nested_contract(database):
    """실제 PostgreSQL의 프로필과 작업 단계에서 반환한 추천을 새 계약으로 검증한다."""
    _, sessions, config = database
    with sessions.begin() as session:
        seed(session)
    headers = {"X-Local-Token": "test-token"}
    with TestClient(create_app(database_url=config.attributes["database_url"], local_token="test-token")) as client:
        created = client.post("/api/work-methods", headers=headers, json={"task_type": "code_review",
            "prompt_text": "입력 검증을 확인해줘.", "workflow": [{"id": "validation", "label": "입력 검증",
                "capabilities": ["input_validation"], "status": "gap", "weight": 5}]})
        assert created.status_code == 201
        response = client.get("/api/recommendations", headers=headers)
    assert response.status_code == 200
    cards = response.json()["items"]
    assert cards and any(item["assessment"]["workflow_fit"] for item in cards)
    for item in cards:
        typed = RecommendationCard.model_validate(item)
        assert typed.assessment.eligible
        assert typed.assessment.score == typed.score
        assert typed.assessment.freshness.is_release_date is False
