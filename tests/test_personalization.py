"""여러 사용자 문맥에서 추천 순위와 근거 및 보류 경계를 검증한다."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace as NS
import pytest

from backend.personalization import build_context, rank_candidate
from backend.catalog.dimensions import extract_dimensions
from backend.catalog.classification import classify_bundle


NOW = datetime(2026, 11, 15, tzinfo=timezone.utc)


def profile(**changes):
    """다른 목표나 환경을 명시할 수 있는 순수 사용자 데이터를 만든다."""
    value = dict(id="user", version=1, role="백엔드 개발", primary_language="python",
        task_types=["code_review"], goals=[], preferences={}, context_json={})
    value.update(changes)
    return NS(**value)


def method(capability=None, **changes):
    """실제 사용자 입력 형태의 작업 방식과 선택적인 공백을 만든다."""
    value = dict(id="method", version=1, task_type="code_review", prompt_text="",
        required_checks=[], is_active=True, workflow_json=[])
    if capability:
        value["workflow_json"] = [{"id": "check", "label": "중요 검사", "capabilities": [capability],
            "status": "gap", "weight": 5}]
    value.update(changes)
    return NS(**value)


def environment(**changes):
    """프로젝트 스캔 관측값과 설치 정보만 포함하는 환경을 만든다."""
    value = dict(id="env", runner="codex_cli", runner_version="1", model_id="unknown",
        metadata_json={"languages": ["python"], "frameworks": ["fastapi"]},
        installed_skills=[], settings_hash="settings", scanned_at="2026-11-15T00:00:00Z", device_id="pc")
    value.update(changes)
    return NS(**value)


def candidate(capabilities=None, **changes):
    """고정 원본과 기능 근거를 갖는 후보 묶음을 만든다."""
    caps = capabilities if capabilities is not None else ["input_validation"]
    dimensions = dict(languages=["python"], frameworks=[], tools=[], capabilities=caps,
        evidence=[dict(dimension="capabilities", value=cap, path="SKILL.md", line=2,
            excerpt="Provides " + cap.replace("_", " ") + ".") for cap in caps])
    dimensions.update(changes.pop("dimensions", {}))
    metadata = dict(source_verified=True, source_url="https://github.com/example/review/tree/" + "a" * 40,
        repository={"archived": False, "facts": {}})
    metadata.update(changes.pop("metadata", {}))
    version = NS(id="version", revision="a" * 40, bundle_sha256="b" * 64,
        tags={"task_types": ["code_review"], "dimensions": dimensions}, metadata_json=metadata)
    for key, value in changes.items():
        setattr(version, key, value)
    return (NS(id="asset", display_name="Review", kind="skill"), version,
        NS(source_url=metadata["source_url"], first_seen_at="2026-11-15T00:00:00Z"),
        NS(kind="github_repo", url="https://github.com/example/review", enabled=True))


def rank(user=None, methods=None, env=None, item=None):
    """고정 시각으로 순수 추천 함수를 실행한다."""
    return rank_candidate(user or profile(), methods or {}, env, *(item or candidate()), now=NOW)


def documented_candidate(text):
    """실제 대표 문서에서 추출한 관측값과 줄 근거로 후보를 만든다."""
    return candidate(dimensions=extract_dimensions({"SKILL.md": text.encode("utf-8")}))


def test_same_task_users_receive_different_gap_rankings():
    """같은 언어와 작업이라도 실제 공백이 다르면 유용한 후보가 바뀐다."""
    validation, regression = candidate(["input_validation"]), candidate(["regression_test"])
    left, right = {"code_review": method("input_validation")}, {"code_review": method("regression_test")}
    assert rank(methods=left, item=validation)["score"] > rank(methods=left, item=regression)["score"]
    assert rank(methods=right, item=regression)["score"] > rank(methods=right, item=validation)["score"]
    fit = rank(methods=left, item=validation)["workflow_fit"][0]
    assert fit["relation"] == "fills_gap" and fit["evidence"][0]["path"] == "SKILL.md"


def test_covered_step_is_less_urgent_than_explicit_gap():
    """이미 수행하는 검사와 새로운 작업 공백의 우선순위를 구분한다."""
    covered = method("input_validation")
    covered.workflow_json[0]["status"] = "covered"
    existing = rank(methods={"code_review": covered})
    missing = rank(methods={"code_review": method("input_validation")})
    assert existing["breakdown"]["workflow_overlap"] < 0
    assert missing["score"] > existing["score"]
    assert "실제 비교 필요" in " ".join(existing["reasons"])


def test_current_project_observations_take_priority_and_keep_provenance():
    """프로필의 일반 언어보다 현재 연결 프로젝트를 우선하고 불일치를 알린다."""
    user = profile(context_json={"frameworks": ["Django"], "tools": ["Claude Code"]})
    env = environment(metadata_json={"languages": ["TypeScript"], "frameworks": ["Next.js"]})
    context = build_context(user, [], env)
    assert context["declared"]["languages"] == ["python"]
    assert context["observed"]["languages"] == ["typescript"]
    assert context["effective"]["frameworks"] == ["nextjs"]
    assert context["effective"]["tools"] == ["codex_cli"]
    assert {item["source"] for item in context["provenance"]["languages"]} == {"profile", "environment"}
    result = rank(user=user, env=env)
    assert not result["eligible"] and result["uncertainties"]


def test_language_and_framework_contradictions_reject():
    """Python이라는 공통점만으로 다른 프레임워크 전용 후보를 추천하지 않는다."""
    django = documented_candidate("Code review with input validation.\nThis skill supports Python and Django.")
    assert not rank(env=environment(), item=django)["eligible"]
    typescript = documented_candidate("Code review with input validation.\nThis skill targets TypeScript.")
    assert not rank(item=typescript)["eligible"]


def test_react_basis_in_next_project_can_match_without_execution_claim():
    """Next 프로젝트의 React 기반 관련성과 실행 호환성 보장을 혼동하지 않는다."""
    item = documented_candidate("Code review with input validation.\nSupports TypeScript and React.")
    result = rank(env=environment(metadata_json={"languages": ["typescript"], "frameworks": ["nextjs"]}), item=item)
    assert result["eligible"] and result["matches"]["frameworks"] == ["react"]
    assert "compatibility" not in result


def test_exclusive_other_runner_rejects_even_if_auxiliary_tool_matches():
    """pytest가 같아도 Claude 전용 후보를 Codex 환경에 맞는 것으로 취급하지 않는다."""
    user = profile(context_json={"tools": ["pytest"]})
    item = documented_candidate("Code review with input validation.\nThis skill requires Claude Code.\nUse pytest.")
    assert not rank(user=user, env=environment(), item=item)["eligible"]
    item[1].tags["dimensions"]["tools"].append("codex_cli")
    item[1].tags["dimensions"]["evidence"].append(dict(dimension="tools", value="codex_cli",
        path="SKILL.md", line=4, excerpt="This skill supports Codex CLI and Claude Code."))
    assert rank(user=user, env=environment(), item=item)["eligible"]


def test_unknown_environment_does_not_invent_codex_tool():
    """연결되지 않은 사용자에게 기본 실행 도구를 임의로 부여하지 않는다."""
    assert build_context(profile(), {}, None)["effective"]["tools"] == []
    context = build_context(profile(), {}, environment(runner="unknown"))
    assert context["observed"]["tools"] == []


def test_task_only_match_abstains_but_explicit_goal_can_support_recommendation():
    """작업 이름만 같은 빈약한 후보는 개인화 추천으로 부풀리지 않는다."""
    item = candidate(dimensions={"languages": [], "frameworks": [], "tools": []})
    assert not rank(item=item)["eligible"]
    result = rank(user=profile(goals=["input_validation"]), item=item)
    assert result["eligible"] and result["matches"]["goals"] == ["input_validation"]


def test_required_checks_and_korean_prompt_are_used_without_exposing_prompt():
    """구조화 단계가 없으면 명시된 한국어 검사와 필수 검사를 사용한다."""
    private = "고객 비공개 주문 로직의 입력 검증과 예외 처리를 확인해줘."
    work = method(prompt_text=private, required_checks=["known_regressions_not_missed"])
    result = rank(methods={"code_review": work}, item=candidate(["exception_handling", "regression_test"]))
    assert {fit["source"] for fit in result["workflow_fit"]} == {"explicit_prompt", "required_check"}
    public = json.dumps(result, ensure_ascii=False)
    assert private not in public and "고객 비공개" not in public
    assert "prompt_text" not in json.dumps(build_context(profile(), [work], None))


def test_structured_workflow_takes_priority_over_prompt_inference():
    """명시한 공백이 있으면 프롬프트의 일반 언급으로 이를 덮어쓰지 않는다."""
    work = method("regression_test", prompt_text="Check input validation.")
    context = build_context(profile(), [work], None)
    assert context["methods"][0]["workflow"][0]["capabilities"] == ["regression_test"]
    assert len(context["methods"][0]["workflow"]) == 1


def test_existing_required_check_alignment_does_not_favor_irrelevant_candidate():
    """기존 검사 중복 감점 때문에 중요한 필수 조건이 무관한 기능보다 뒤로 밀리지 않는다."""
    work = {"code_review": method(required_checks=["known_regressions_not_missed"])}
    useful = rank(methods=work, item=candidate(["regression_test"]))
    unrelated = rank(methods=work, item=candidate(["documentation"]))
    assert useful["breakdown"]["method_alignment"] > 0
    assert useful["score"] > unrelated["score"]


def test_new_capability_goals_distinguish_same_task_users():
    """보안과 접근성 등 확장된 실제 작업 목표도 추천에 반영한다."""
    item = candidate(["api_security"])
    security = rank(user=profile(context_json={"focus_areas": ["api_security"]}), item=item)
    accessibility = rank(user=profile(context_json={"focus_areas": ["accessibility"]}), item=item)
    assert security["score"] > accessibility["score"]
    assert security["eligible"] and not accessibility["eligible"]


def test_known_stack_does_not_make_irrelevant_functionality_personalized():
    """같은 Python과 도구를 쓰더라도 보안 목표에 문서화나 성능 후보를 섞지 않는다."""
    user = profile(context_json={"focus_areas": ["api_security"]})
    for caps in (["documentation"], ["performance"], []):
        assert not rank(user=user, env=environment(), item=candidate(caps))["eligible"]
    assert rank(user=user, env=environment(), item=candidate(["api_security"]))["eligible"]


def test_candidate_without_known_functionality_always_abstains():
    """언어와 작업 이름만 확인한 후보를 기능이 있는 추천처럼 내놓지 않는다."""
    result = rank(env=environment(), item=candidate([]))
    assert not result["eligible"] and result["matches"]["confidence"] == "abstained"


def test_explicitly_covered_capabilities_need_demonstrated_complement():
    """현재 모든 기능을 이미 수행하는 사용자에게 같은 기능을 새 공백처럼 추천하지 않는다."""
    work = method(workflow_json=[{"id": "complete", "label": "이미 하는 검사", "capabilities": [
        "input_validation", "exception_handling", "api_security", "documentation", "performance"],
        "status": "covered", "weight": 5}])
    for caps in (["input_validation"], ["documentation", "performance"], ["api_security"]):
        result = rank(user=profile(goals=["api_security"]), methods={"code_review": work}, item=candidate(caps))
        assert not result["eligible"]
    # 같은 명시적 기능을 공백으로 등록한 경우에는 보완 후보가 될 수 있다.
    work.workflow_json.append({"id": "missing", "label": "입력 검증 보완", "capabilities": ["input_validation"],
        "status": "gap", "weight": 5})
    assert rank(methods={"code_review": work})["eligible"]


def test_existing_prompt_alternatives_survive_strong_coverage_gate():
    """프롬프트에서 검사 언급이 발견된 경우는 검증된 충분한 지원으로 단정하지 않는다."""
    work = method(prompt_text="Check input validation.")
    result = rank(methods={"code_review": work})
    assert result["eligible"] and result["workflow_fit"][0]["source"] == "explicit_prompt"


def test_exact_bundle_installed_rejects_but_manifest_or_name_is_insufficient():
    """이름이나 manifest 해시만 같으면 전체 스킬 설치 버전으로 단정하지 않는다."""
    env = environment(installed_skills=[{"name": "Review", "manifest_sha256": "b" * 64}])
    user = profile(goals=["input_validation"])
    assert rank(user=user, env=env)["eligible"]
    env.installed_skills[0]["bundle_sha256"] = "b" * 64
    assert not rank(user=user, env=env)["eligible"]
    env.installed_skills[0]["excluded"] = True
    assert rank(user=user, env=env)["eligible"]


def test_exclusion_preferences_override_high_workflow_score():
    """명시적인 제외 설정을 작업 공백 가점으로 뒤집지 않는다."""
    user = profile(context_json={"excluded_capabilities": ["input_validation"]})
    result = rank(user=user, methods={"code_review": method("input_validation")})
    assert not result["eligible"]
    user.context_json = {"excluded_tools": ["claude_code"]}
    item = documented_candidate("Code review with input validation.\nFor Claude Code only.")
    assert not rank(user=user, item=item)["eligible"]


def test_freshness_requires_verified_source_dates_and_is_small():
    """방금 발견한 오래된 항목과 확인된 최근 원본 변경을 구분한다."""
    recent = candidate(metadata={"repository": {"facts": {"revision_committed_at": "2026-11-10T00:00:00Z"}}})
    result = rank(item=recent)
    assert result["freshness"]["bonus"] == 3 and not result["freshness"]["is_release_date"]
    unknown = rank()
    assert unknown["freshness"]["status"] == "unknown" and unknown["breakdown"]["freshness"] == 0
    recent[1].metadata_json["source_verified"] = False
    assert rank(item=recent)["freshness"]["status"] == "unknown"


def test_future_dates_and_user_recent_preference_are_respected():
    """미래 시각은 최신성 근거로 쓰지 않고 최신성 선호를 강제하지 않는다."""
    item = candidate(metadata={"repository": {"facts": {"revision_committed_at": "2026-11-16T00:00:00Z"}}})
    assert rank(item=item)["freshness"]["status"] == "unknown"
    item[1].metadata_json["repository"]["facts"]["revision_committed_at"] = "2026-11-10T00:00:00Z"
    assert rank(user=profile(context_json={"prefer_recent": False}), item=item)["freshness"]["bonus"] == 0


def test_unsafe_evidence_is_not_exposed():
    """절대 경로와 비밀값으로 보이는 문장을 공개 추천 근거에서 제외한다."""
    item = candidate()
    item[1].tags["dimensions"]["evidence"].extend([
        dict(dimension="capabilities", value="input_validation", path="C:/private.txt", line=1, excerpt="input validation"),
        dict(dimension="capabilities", value="input_validation", path="SKILL.md", line=3, excerpt="token=secret"),
        dict(dimension="capabilities", value="input_validation", path="../private", line=1, excerpt="input validation")])
    result = rank(item=item)
    assert len(result["evidence"]) == 1
    assert len(result["evidence"][0]["excerpt"]) <= 240


def test_context_fingerprint_is_stable_and_tracks_private_method_changes():
    """같은 문맥은 같은 지문이고 비공개 작업 방식 수정도 캐시 지문을 바꾼다."""
    work = method("input_validation")
    left = build_context(profile(), {"code_review": work}, None)
    assert left["fingerprint"] == build_context(profile(), [work], None)["fingerprint"]
    work.prompt_text = "비공개 프롬프트 변경"
    assert left["fingerprint"] != build_context(profile(), [work], None)["fingerprint"]
    json.dumps(left)


def test_multiple_tasks_choose_method_with_stronger_explicit_gap():
    """후보가 두 작업을 지원하면 실제 공백이 더 큰 작업 방식에 연결한다."""
    item = candidate(["regression_test"])
    item[1].tags["task_types"].append("test_generation")
    work = method("regression_test", id="tests", task_type="test_generation")
    result = rank(user=profile(task_types=["code_review", "test_generation"]),
        methods={"test_generation": work, "code_review": method("input_validation")}, item=item)
    assert result["matches"]["task_type"] == "test_generation"
    assert result["matches"]["method_id"] == "tests"
    assert result["score"] == sum(result["breakdown"].values())


def test_multiple_tasks_select_required_check_alignment_as_well():
    """여러 작업을 지원하는 후보는 연결 근거가 있는 필수 검사의 작업에 연결한다."""
    item = candidate(["regression_test"])
    item[1].tags["task_types"].append("test_generation")
    result = rank(user=profile(task_types=["code_review", "test_generation"]),
        methods={"code_review": method(required_checks=["known_regressions_not_missed"])}, item=item)
    assert result["matches"]["task_type"] == "code_review"
    assert result["matches"]["method_id"] == "method"


def test_legacy_tags_keep_contextual_matching_without_implying_verified_effect():
    """이전 태그도 읽되 원문 근거와 검증되지 않은 실행 조건의 한계를 남긴다."""
    item = candidate(tags={"task_types": ["code_review"], "languages": ["python"],
        "capabilities": ["input_validation"], "supported_tools": []})
    result = rank(user=profile(goals=["input_validation"]), item=item)
    assert result["eligible"] and result["uncertainties"]
    assert "performance" not in result and "support_level" not in result


def test_incidental_language_framework_and_tool_mentions_are_not_exclusive_targets():
    """구현 도구와 예제의 언급 때문에 실제 목표에 맞는 후보를 강제로 제외하지 않는다."""
    item = documented_candidate("Code review provides input validation.\n"
        "The Python helper reads JavaScript files.\nReact examples demonstrate output.\n"
        "Documentation includes Claude Code examples.")
    user = profile(primary_language="typescript", goals=["input_validation"],
        context_json={"frameworks": ["fastapi"], "tools": ["codex_cli"]})
    result = rank(user=user, item=item)
    assert result["eligible"]
    assert all(not values for values in result["matches"]["target_claims"].values())
    assert "지원 대상 선언은 미확인" in " ".join(result["uncertainties"])
    assert "compatibility" not in result


def test_explicit_skill_support_statement_rejects_other_project_framework():
    """문서가 지원 대상을 명시한 경우에는 같은 Python이어도 Django와 FastAPI를 구분한다."""
    item = documented_candidate("Code review provides input validation.\nThis skill supports Python and FastAPI.")
    result = rank(user=profile(goals=["input_validation"]),
        env=environment(metadata_json={"languages": ["python"], "frameworks": ["django"]}), item=item)
    assert not result["eligible"]
    assert result["matches"]["target_claims"]["languages"] == ["python"]
    assert result["matches"]["target_claims"]["frameworks"] == ["fastapi"]
    assert any("프레임워크" in reason for reason in result["excluded_reasons"])


def test_target_statement_does_not_spread_to_incidental_clause():
    """같은 줄의 다른 절에 있는 예제 언급으로 지원 대상 집합을 늘리지 않는다."""
    item = documented_candidate("Code review provides input validation.\n"
        "This skill supports Python; the tutorial includes React examples.\n"
        "This skill supports Python and uses FastAPI internally.")
    result = rank(user=profile(goals=["input_validation"]), env=environment(), item=item)
    assert result["eligible"]
    assert result["matches"]["target_claims"]["languages"] == ["python"]
    assert result["matches"]["target_claims"]["frameworks"] == []


def test_exclusive_tool_and_korean_target_declarations_are_strong_conditions():
    """AI 도구 전용 문구와 한국어 대상 선언은 단순 언급과 달리 강제 조건으로 사용한다."""
    item = documented_candidate("Code review provides input validation.\nFor Claude Code only.")
    result = rank(user=profile(goals=["input_validation"]), env=environment(), item=item)
    assert not result["eligible"] and result["matches"]["target_claims"]["tools"] == ["claude_code"]
    korean = documented_candidate("코드 리뷰와 입력 검증을 제공합니다.\nPython 및 FastAPI 전용 스킬입니다.")
    result = rank(user=profile(goals=["input_validation"]),
        env=environment(metadata_json={"languages": ["python"], "frameworks": ["django"]}), item=korean)
    assert not result["eligible"] and result["matches"]["target_claims"]["frameworks"] == ["fastapi"]
    assert result["matches"]["target_claims"]["languages"] == ["python"]


def test_unsubstantiated_new_dimensions_do_not_become_exclusive_conditions():
    """위치 근거가 없는 새 차원 목록은 지원 대상을 확인한 것으로 취급하지 않는다."""
    item = candidate(dimensions={"languages": ["typescript"], "frameworks": ["react"],
        "tools": ["claude_code"], "evidence": []})
    result = rank(user=profile(goals=["input_validation"]), env=environment(), item=item)
    assert result["eligible"]
    assert set(result["matches"]["target_scope_basis"].values()) == {"unknown"}
    assert result["uncertainties"]


def test_legacy_declared_targets_retain_explicit_mismatch_behavior():
    """문서 관측 구조가 없던 이전 직접 태그의 대상 조건은 호환성을 보존한다."""
    item = candidate(tags={"task_types": ["code_review"], "languages": ["typescript"],
        "capabilities": ["input_validation"], "supported_tools": []})
    result = rank(user=profile(goals=["input_validation"]), item=item)
    assert not result["eligible"]
    assert result["matches"]["target_scope_basis"]["languages"] == "legacy_declared"


def test_python_runtime_requirement_is_not_python_only_project_support():
    """보고서 생성에 쓰는 Python 의존성을 사용자 코드의 전용 언어로 단정하지 않는다."""
    item = documented_candidate("Code review provides input validation.\n"
        "Requires Python to render reports.\nThis skill targets TypeScript.")
    result = rank(user=profile(primary_language="typescript", goals=["input_validation"]), item=item)
    assert result["eligible"] and result["matches"]["target_claims"]["languages"] == ["typescript"]


def test_first_technology_title_cannot_hide_later_incompatible_support_claim():
    """Rust 제목의 약한 첫 언급이 뒤의 실제 지원 선언을 가려 React 사용자에게 새지 않는다."""
    item = documented_candidate("# rust-test-design\nSupports Rust and Codex CLI.\n"
        "Provides test generation and test design.")
    item[1].tags["task_types"] = ["test_generation"]
    user = profile(primary_language="typescript", task_types=["test_generation"],
        goals=["test_design"], context_json={"frameworks": ["react"], "tools": ["codex_cli"]})
    result = rank(user=user, item=item)
    assert not result["eligible"]
    assert result["matches"]["target_claims"]["languages"] == ["rust"]
    assert result["matches"]["target_scope_basis"]["languages"] == "explicit_claim"
    assert any(item["dimension"] == "languages" and item["line"] == 2
        and "Supports Rust" in item["excerpt"] for item in result["evidence"])


def test_later_multi_language_support_claim_keeps_each_supported_target():
    """첫 제목에 없는 다른 지원 언어도 함께 보존하고 실제 지원 사용자는 제외하지 않는다."""
    item = documented_candidate("# Python notes\nSupports Rust and Python and Codex CLI.\n"
        "Provides test generation and test design.")
    item[1].tags["task_types"] = ["test_generation"]
    user = profile(primary_language="rust", task_types=["test_generation"], goals=["test_design"])
    result = rank(user=user, item=item)
    assert result["eligible"] and result["matches"]["target_claims"]["languages"] == ["python", "rust"]


def test_unsafe_separate_target_evidence_cannot_create_hard_conditions():
    """별도 대상 근거도 상대 경로와 줄 번호 검증을 우회해 사용자 후보를 숨기지 않는다."""
    item = documented_candidate("# Rust notes\nProvides code review and input validation.")
    item[1].tags["dimensions"]["target_evidence"] = [{"dimension": "languages", "value": "rust",
        "path": "C:/private.txt", "line": 1, "excerpt": "Supports Rust."}]
    result = rank(user=profile(goals=["input_validation"]), item=item)
    assert result["eligible"] and result["matches"]["target_claims"]["languages"] == []


def browser_complement_case(text="Browser automation CLI for AI agents."):
    """전체 테스트 작성 대신 명시한 브라우저 검증 공백을 가진 실제 형태의 입력을 만든다."""
    item = candidate(tags=classify_bundle({"SKILL.md": text.encode()}))
    user = profile(primary_language="typescript", task_types=["test_generation"], goals=["test_design"],
        context_json={"frameworks": ["react"], "tools": ["codex_cli"]})
    work = method("browser_testing", task_type="test_generation")
    return user, {"test_generation": work}, item


def test_explicit_browser_gap_can_receive_workflow_complement_without_source_task_relabeling():
    """브라우저 자동화 원문을 전체 테스트 작성으로 부르지 않고 등록한 검증 단계에 연결한다."""
    user, methods, item = browser_complement_case()
    result = rank(user=user, methods=methods, item=item)
    assert result["eligible"] and result["breakdown"]["task_relevance"] == 0
    assert result["matches"]["relationship"] == "workflow_complement"
    assert result["matches"]["task_type"] == "test_generation"
    assert result["matches"]["candidate_task_types"] == []
    assert result["matches"]["task_types"] == [] and item[1].tags["task_types"] == []
    assert result["matches"]["method_id"] == methods["test_generation"].id
    assert result["workflow_fit"][0]["matched_capabilities"] == ["browser_testing"]
    assert "브라우저 검증 단계를 보완" in " ".join(result["reasons"])
    assert "테스트 코드 작성 전체 지원은 확인하지 못함" in result["uncertainties"]
    assert "support_level" not in result and "adapter_id" not in result


def test_general_goal_covered_step_and_legacy_prompt_do_not_create_complement_permission():
    """목표의 브라우저 단어나 이미 수행하는 검사 및 추정한 프롬프트만으로 관계를 만들지 않는다."""
    user, methods, item = browser_complement_case()
    user.goals = ["browser_testing"]
    assert not rank(user=user, item=item)["eligible"]
    methods["test_generation"].workflow_json[0]["status"] = "covered"
    assert not rank(user=user, methods=methods, item=item)["eligible"]
    legacy = method(task_type="test_generation", prompt_text="Check browser testing.")
    assert not rank(user=user, methods={"test_generation": legacy}, item=item)["eligible"]


def test_browser_complement_is_not_a_generic_capability_or_code_review_bridge():
    """브라우저 검증 보완을 다른 작업이나 입력 검증 구현 가이드의 리뷰 지원으로 일반화하지 않는다."""
    user, methods, item = browser_complement_case()
    user.task_types = ["code_review"]
    methods["test_generation"].task_type = "code_review"
    assert not rank(user=user, methods=methods, item=item)["eligible"]
    implementation = candidate(tags=classify_bundle({"SKILL.md": b"A guide to input validation."}))
    assert not rank(user=profile(goals=["input_validation"]),
        methods={"code_review": method("input_validation")}, item=implementation)["eligible"]


def test_browser_complement_keeps_language_framework_and_tool_gates():
    """단계 보완도 후보가 명시한 다른 환경의 전용 조건을 우회하지 않는다."""
    for claim in ("Supports Python.", "Supports Django.", "For Claude Code only."):
        user, methods, item = browser_complement_case("Browser automation CLI for AI agents.\n" + claim)
        assert not rank(user=user, methods=methods, item=item)["eligible"]


def test_browser_complement_requires_safe_candidate_capability_evidence():
    """기능 태그만 있거나 공개할 수 없는 위치 근거만 있으면 보완 관계를 만들지 않는다."""
    user, methods, item = browser_complement_case()
    item[1].tags["evidence"] = []
    item[1].tags["dimensions"]["evidence"] = []
    assert not rank(user=user, methods=methods, item=item)["eligible"]
    item[1].tags["dimensions"]["evidence"] = [{"dimension": "capabilities", "value": "browser_testing",
        "path": "C:/private.txt", "line": 1, "excerpt": "Browser automation CLI for AI agents."}]
    assert not rank(user=user, methods=methods, item=item)["eligible"]


def test_browser_complement_links_only_the_declared_browser_gap():
    """자동화 후보에 다른 기능이 함께 있어도 이 관계의 점수와 단계 연결은 브라우저 공백에 한정한다."""
    user, methods, item = browser_complement_case("Browser automation CLI for AI agents.\nProvides documentation.")
    methods["test_generation"].workflow_json.append({"id": "docs", "label": "문서 보완",
        "capabilities": ["documentation"], "status": "gap", "weight": 5})
    result = rank(user=user, methods=methods, item=item)
    assert result["eligible"] and len(result["workflow_fit"]) == 1
    assert result["workflow_fit"][0]["matched_capabilities"] == ["browser_testing"]
    assert result["breakdown"]["workflow_gap"] == 25


def test_real_test_generation_still_uses_same_task_relationship():
    """실제로 테스트 작성 작업을 제공하는 후보의 기존 작업 일치 경로는 바꾸지 않는다."""
    user, methods, item = browser_complement_case("Generate tests with browser automation.")
    result = rank(user=user, methods=methods, item=item)
    assert result["eligible"] and result["matches"]["relationship"] == "same_task"
    assert result["matches"]["candidate_task_types"] == ["test_generation"]
    assert result["breakdown"]["task_relevance"] == 18


def test_workflow_complement_retains_other_actual_candidate_task():
    """다른 작업 태그를 가진 후보도 단계 보완 관계와 원래 제공 작업을 구분한다."""
    user, methods, item = browser_complement_case("Code review with browser automation.")
    result = rank(user=user, methods=methods, item=item)
    assert result["eligible"] and result["matches"]["relationship"] == "workflow_complement"
    assert result["matches"]["candidate_task_types"] == ["code_review"]
    assert item[1].tags["task_types"] == ["code_review"]
    assert result["matches"]["task_type"] == "test_generation"


def test_runtime_python_requirement_does_not_bias_project_language_fit():
    """리포트 파서의 Python 실행 의존성을 피검토 코드 언어의 일치로 가점하지 않는다."""
    item = documented_candidate("Code review with input validation.\nReport parsing requires Python 3.9+.")
    results = [rank(user=profile(primary_language=language, goals=["input_validation"]), item=item)
        for language in ("python", "java", "go")]
    assert all(result["eligible"] for result in results)
    assert len({result["score"] for result in results}) == 1
    for result in results:
        assert result["breakdown"]["language_fit"] == 0
        assert result["matches"]["languages"] == []
        assert result["matches"]["mentioned_languages"] == ["python"]
        assert not any("프로젝트 언어 문맥과 일치" in reason for reason in result["reasons"])


def test_incidental_framework_and_auxiliary_tools_have_no_positive_stack_fit():
    """React 예제나 도구 사용 언급만으로 현재 프로젝트의 지원 조건을 확인했다고 하지 않는다."""
    item = documented_candidate("Code review with input validation.\nReact examples illustrate output.\n"
        "Uses pytest and Playwright.\nDocumentation includes Codex CLI examples.")
    user = profile(goals=["input_validation"], context_json={"frameworks": ["react"],
        "tools": ["codex_cli", "pytest", "playwright"]})
    result = rank(user=user, item=item)
    assert result["eligible"]
    assert result["matches"]["frameworks"] == [] and result["matches"]["tools"] == []
    assert result["matches"]["mentioned_frameworks"] == ["react"]
    assert result["matches"]["mentioned_tools"] == ["codex_cli", "playwright", "pytest"]
    assert result["breakdown"]["framework_fit"] == result["breakdown"]["tool_fit"] == 0
    assert not any("사용 도구 문맥과 일치" in reason for reason in result["reasons"])


def test_positive_stack_fit_requires_explicit_target_or_legacy_declaration():
    """초기 제목보다 뒤의 실제 지원 선언을 사용하고 기존 가중치 및 언어별 제외를 유지한다."""
    item = documented_candidate("# Python FastAPI Codex notes\nCode review with input validation.\n"
        "Supports Python and FastAPI and Codex CLI.")
    user = profile(goals=["input_validation"], context_json={"frameworks": ["fastapi"], "tools": ["codex_cli"]})
    result = rank(user=user, item=item)
    assert result["eligible"]
    assert (result["breakdown"]["language_fit"], result["breakdown"]["framework_fit"],
        result["breakdown"]["tool_fit"]) == (6, 10, 4)
    assert result["matches"]["mentioned_languages"] == []
    assert result["matches"]["languages"] == ["python"]
    assert not rank(user=profile(primary_language="typescript", goals=["input_validation"]), item=item)["eligible"]
    legacy = candidate(tags={"task_types": ["code_review"], "languages": ["python"],
        "frameworks": ["fastapi"], "supported_tools": ["codex_cli"], "capabilities": ["input_validation"]})
    old = rank(user=user, item=legacy)
    assert (old["breakdown"]["language_fit"], old["breakdown"]["framework_fit"], old["breakdown"]["tool_fit"]) == (6, 10, 4)


def test_only_weak_stack_mentions_and_task_name_cannot_qualify_recommendation():
    """목표나 공백 없이 프로젝트와 같은 단어가 등장한 것만으로 개인 추천을 만들지 않는다."""
    item = documented_candidate("Code review with input validation.\nThe Python helper reads files.\n"
        "React examples.\nUses pytest and Codex CLI.")
    user = profile(context_json={"frameworks": ["react"], "tools": ["codex_cli", "pytest"]})
    result = rank(user=user, item=item)
    assert not result["eligible"] and result["matches"]["confidence"] == "abstained"
    assert all(result["breakdown"][key] == 0 for key in ("language_fit", "framework_fit", "tool_fit"))


def test_declared_auxiliary_tool_does_not_receive_ai_runner_fit_points():
    """보조 테스트 도구가 명시돼도 AI 실행 도구 일치 가점과는 구분한다."""
    item = documented_candidate("Code review with input validation.\nSupports pytest.")
    result = rank(user=profile(goals=["input_validation"], context_json={"tools": ["pytest"]}), item=item)
    assert result["eligible"] and result["matches"]["tools"] == ["pytest"]
    assert result["breakdown"]["tool_fit"] == 0


def test_accessibility_audit_complements_only_explicit_review_gap_without_relabeling_task():
    """접근성 감사의 원문 행동을 선언한 검토 공백에만 연결하고 전체 코드 리뷰라고 부르지 않는다."""
    item = candidate(tags=classify_bundle({"SKILL.md": b"Conduct WCAG 2.2 accessibility audits with manual verification."}))
    user = profile(primary_language="typescript", goals=["accessibility"], context_json={"frameworks": ["react"]})
    work = {"code_review": method("accessibility")}
    result = rank(user=user, methods=work, item=item)
    assert result["eligible"] and result["matches"]["relationship"] == "workflow_complement"
    assert result["matches"]["candidate_task_types"] == [] and item[1].tags["task_types"] == []
    assert result["matches"]["complementary_capability"] == "accessibility"
    assert result["breakdown"]["task_relevance"] == 0
    assert result["matches"]["source_actions"] == ["accessibility_audit"]
    assert result["matches"]["source_action_evidence"][0]["path"] == "SKILL.md"
    assert any("코드 리뷰 전체 지원은 확인하지 못함" in value for value in result["uncertainties"])


def test_accessibility_implementation_guide_and_goal_only_do_not_enable_review_complement():
    """접근성 명사나 구현 기능만으로 코드 리뷰 보완 관계를 일반화하지 않는다."""
    user = profile(goals=["accessibility"])
    implementation = candidate(tags=classify_bundle({"SKILL.md": b"Implement WCAG compliant accessible components."}))
    assert not rank(user=user, methods={"code_review": method("accessibility")}, item=implementation)["eligible"]
    audit = candidate(tags=classify_bundle({"SKILL.md": b"Conduct accessibility audits."}))
    assert not rank(user=user, item=audit)["eligible"]
    covered = method("accessibility")
    covered.workflow_json[0]["status"] = "covered"
    assert not rank(user=user, methods={"code_review": covered}, item=audit)["eligible"]


def test_accessibility_complement_keeps_explicit_targets_and_safe_action_evidence_boundary():
    """감사 단계도 다른 전용 환경을 우회하거나 위치가 잘못된 행동 근거를 사용하지 않는다."""
    item = candidate(tags=classify_bundle({"SKILL.md": b"Conduct accessibility audits.\nSupports Django."}))
    user = profile(goals=["accessibility"], context_json={"frameworks": ["react"]})
    assert not rank(user=user, methods={"code_review": method("accessibility")}, item=item)["eligible"]
    item = candidate(tags=classify_bundle({"SKILL.md": b"# Accessibility guide\nConduct accessibility audits."}))
    for field in ("evidence", "action_evidence"):
        item[1].tags["dimensions"][field] = [{**value, "path": "../outside.md"}
            for value in item[1].tags["dimensions"][field]]
    item[1].tags["evidence"] = []
    assert not rank(user=user, methods={"code_review": method("accessibility")}, item=item)["eligible"]


def test_explicit_database_model_review_complements_only_database_review_gap():
    """DB 모델 검토 행동을 명시한 원문만 해당 변경 검토 공백의 단계 대안으로 연결한다."""
    item = candidate(tags=classify_bundle({"SKILL.md": b"Designs or reviews relational, document, key-value, graph, or search data\nmodels. Use for schema changes and database migrations."}))
    user = profile(goals=["database_changes"], context_json={"frameworks": ["flask"]})
    result = rank(user=user, methods={"code_review": method("database_changes")}, item=item)
    assert result["eligible"] and result["matches"]["relationship"] == "workflow_complement"
    assert result["matches"]["source_actions"] == ["database_model_review"]
    assert result["matches"]["complementary_capability"] == "database_changes"
    assert result["breakdown"]["task_relevance"] == 0 and item[1].tags["task_types"] == []
    assert result["matches"]["source_action_evidence"][0]["line"] == 1
    assert not rank(user=user, item=item)["eligible"]


@pytest.mark.parametrize("text", ["Implements SQL migrations safely.", "Reviews API schema changes.",
    "Designs relational data models for database migrations."])
def test_implementation_only_and_api_schema_sources_are_not_database_review_complements(text):
    """DB 구현이나 API 스키마만 다루는 자료를 DB 검토 행동으로 확대하지 않는다."""
    item = candidate(tags=classify_bundle({"SKILL.md": text.encode()}))
    assert not rank(user=profile(goals=["database_changes"]),
        methods={"code_review": method("database_changes")}, item=item)["eligible"]
