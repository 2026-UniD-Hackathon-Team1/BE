"""사용자 입력과 관측 환경을 구분하고 근거 있는 작업 관련성을 계산한다."""
import hashlib
import json
import re
from datetime import datetime, timezone

from backend.catalog.dimensions import (RULES, accessibility_review_actions, database_review_actions,
                                       declared_review_actions, declares_target, extract_dimensions)
from backend.catalog.inspection import verified_source
from backend.catalog.metadata import timestamp


CONTEXT_VERSION = "workflow-context-v1"
AI_TOOLS = {"codex_cli", "claude_code", "cursor", "github_copilot"}
ALIASES = {"py": "python", "js": "javascript", "ts": "typescript", "c#": "csharp",
    "c++": "cpp", "golang": "go", "next.js": "nextjs", "next_js": "nextjs",
    "react.js": "react", "reactjs": "react", "vue.js": "vue", "vuejs": "vue",
    "nuxt.js": "nuxt", "nestjs": "nestjs", "nest.js": "nestjs",
    "codex": "codex_cli", "codexcli": "codex_cli", "claudecode": "claude_code",
    "copilot": "github_copilot", "githubcopilot": "github_copilot"}
CAP_LABELS = {"input_validation": "입력 검증", "exception_handling": "예외 처리",
    "regression_test": "회귀 테스트", "test_coverage": "테스트 커버리지", "documentation": "문서화",
    "api_security": "API 보안", "accessibility": "접근성", "performance": "성능 분석",
    "browser_testing": "브라우저 테스트", "test_design": "테스트 설계",
    "database_changes": "데이터베이스 변경", "typing": "타입 검사"}
CHECK_CAPABILITIES = {"known_regressions_not_missed": "regression_test",
    "coverage_not_decreased": "test_coverage", "minimum_test_coverage": "test_coverage"}
FRAMEWORK_FAMILIES = ({"react", "nextjs"}, {"vue", "nuxt"})
SECRET_TEXT = re.compile(r"(?:\b(?:api[_-]?key|password|secret|token)\s*[:=]|\b(?:gh[pousr]_|sk-)[A-Za-z0-9_-]{12,})", re.I)


def _get(value, key, default=None):
    """ORM 객체와 순수 테스트용 사전에서 같은 방식으로 값을 읽는다."""
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _dict(value):
    """사전이 아닌 저장값은 빈 문맥으로 취급한다."""
    return value if isinstance(value, dict) else {}


def _names(values, dimension=None):
    """별칭과 대소문자를 정리하고 미확인 값이나 제어문자를 제거한다."""
    if not isinstance(values, (list, tuple, set)):
        return []
    result = set()
    for value in values:
        if not isinstance(value, str) or len(value) > 80:
            continue
        name = re.sub(r"[ -]+", "_", value.strip().lower())
        name = ALIASES.get(name, name)
        if (name in {"unknown", "none", "미확인", ""}
                or not re.fullmatch(r"[a-z0-9][a-z0-9_.+#-]{0,63}", name)):
            continue
        if dimension == "capabilities" and name not in RULES["capabilities"]:
            continue
        result.add(name)
    return sorted(result)


def _safe_text(value, limit=80):
    """표시용 문장에서 제어문자와 비밀값으로 보이는 문자열을 제거한다."""
    if not isinstance(value, str) or SECRET_TEXT.search(value):
        return ""
    return " ".join(value.split())[:limit]


def _hash(value):
    """순서가 같은 문맥을 안정적으로 식별하고 비공개 원문은 노출하지 않는다."""
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def _methods(methods):
    """활성 작업 방식만 작업 유형과 버전 순서로 정리한다."""
    values = methods.values() if isinstance(methods, dict) else methods or []
    return sorted((method for method in values if _get(method, "is_active", True)),
        key=lambda method: (str(_get(method, "task_type", "")), str(_get(method, "id", "")),
                            int(_get(method, "version", 0) or 0)))


def _workflow(method):
    """구조화한 작업 단계를 우선하고 기존 요청에서 명시된 검사만 보조 추출한다."""
    structured = _get(method, "workflow_json", [])
    result = []
    for index, item in enumerate(structured[:30] if isinstance(structured, list) else []):
        if not isinstance(item, dict) or item.get("status") not in {"covered", "gap"}:
            continue
        caps = _names(item.get("capabilities", []), "capabilities")
        if not caps:
            continue
        raw_weight = item.get("weight", 3)
        weight = max(1, min(5, raw_weight)) if isinstance(raw_weight, int) else 3
        result.append({"id": _safe_text(item.get("id")) or f"step_{index + 1}",
            "label": _safe_text(item.get("label")) or "등록한 작업 단계", "capabilities": caps,
            "status": item["status"], "weight": weight, "source": "declared_workflow"})
    if result:
        return result
    prompt = _get(method, "prompt_text", "")
    extracted = extract_dimensions({"SKILL.md": (prompt if isinstance(prompt, str) else "")[:20000].encode("utf-8")})
    checks = _get(method, "required_checks", [])
    check_caps = set(_names(checks, "capabilities"))
    for check in checks if isinstance(checks, list) else []:
        if isinstance(check, str) and check in CHECK_CAPABILITIES:
            check_caps.add(CHECK_CAPABILITIES[check])
    for capability in sorted(set(extracted["capabilities"]) | check_caps):
        result.append({"id": f"existing_{capability}", "label": CAP_LABELS[capability] + " 요청",
            "capabilities": [capability], "status": "covered", "weight": 5 if capability in check_caps else 3,
            "source": "required_check" if capability in check_caps else "explicit_prompt"})
    return result


def build_context(profile, methods, environment):
    """프로필 선언과 현재 프로젝트 관측값 및 작업 단계의 출처를 구분한다."""
    context = _dict(_get(profile, "context_json", {}))
    metadata = _dict(_get(environment, "metadata_json", {}))
    additional = context.get("additional_languages", [])
    declared = {"languages": _names([_get(profile, "primary_language", "")] + (additional if isinstance(additional, list) else [])),
        "frameworks": _names(context.get("frameworks", [])), "tools": _names(context.get("tools", [])),
        "goals": _names(_get(profile, "goals", []), "capabilities"),
        "focus_areas": _names(context.get("focus_areas", []), "capabilities"),
        "excluded_capabilities": _names(context.get("excluded_capabilities", []), "capabilities"),
        "excluded_tools": _names(context.get("excluded_tools", [])),
        "prefer_recent": context.get("prefer_recent", True) is not False}
    runner = _names([_get(environment, "runner", "")])
    observed = {"languages": _names(metadata.get("languages", [])),
        "frameworks": _names(metadata.get("frameworks", [])),
        "tools": [tool for tool in runner if tool in AI_TOOLS]}
    effective = {dimension: observed[dimension] or declared[dimension]
        for dimension in ("languages", "frameworks")}
    # 현재 실행기가 확인되면 다른 AI 도구 선언으로 실행 환경을 덮어쓰지 않는다.
    effective["tools"] = sorted(set(observed["tools"] or [tool for tool in declared["tools"] if tool in AI_TOOLS])
        | (set(declared["tools"]) - AI_TOOLS))
    effective["goals"] = sorted(set(declared["goals"]) | set(declared["focus_areas"]))
    summaries = []
    for method in _methods(methods):
        summaries.append({"id": _get(method, "id"), "version": _get(method, "version"),
            "task_type": _get(method, "task_type"), "workflow": _workflow(method),
            "method_fingerprint": _hash({key: _get(method, key) for key in (
                "prompt_text", "required_checks", "scoring_preferences", "workflow_json", "skill_version_id")})})
    provenance = {dimension: [{"value": value, "source": source} for source, values in (
        ("profile", declared[dimension]), ("environment", observed[dimension])) for value in values]
        for dimension in ("languages", "frameworks", "tools")}
    value = {"context_version": CONTEXT_VERSION, "declared": declared, "observed": observed,
        "effective": effective, "task_types": _names(_get(profile, "task_types", [])),
        "methods": summaries, "provenance": provenance,
        "profile_version": _get(profile, "version"), "profile_identity": _hash({
            "id": _get(profile, "id"), "role": _get(profile, "role"),
            "preferences": _get(profile, "preferences", {})}),
        "environment": {key: _get(environment, key) for key in (
            "id", "device_id", "settings_hash", "scanned_at", "runner_version", "model_id")},
        "installed_fingerprint": _hash(_get(environment, "installed_skills", []))}
    value["fingerprint"] = _hash(value)
    value["context_fingerprint"] = value["fingerprint"]
    return value


def _candidate_dimensions(tags):
    """새 문서 차원을 우선하고 과거 카탈로그 태그와 허용된 기능을 합친다."""
    dimensions = _dict(tags.get("dimensions"))
    result = {}
    for key in ("languages", "frameworks", "tools", "capabilities"):
        legacy = tags.get("supported_tools", []) if key == "tools" else tags.get(key, [])
        values = dimensions.get(key, [])
        result[key] = _names((values if isinstance(values, list) else [])
            + (legacy if isinstance(legacy, list) else []), key)
    return result


def _evidence(tags, dimensions):
    """상대 경로와 허용 태그 및 짧은 원문으로 구성된 공개 근거만 남긴다."""
    result = []
    items = _dict(tags.get("dimensions")).get("evidence", [])
    targets = _dict(tags.get("dimensions")).get("target_evidence", [])
    actions = _dict(tags.get("dimensions")).get("action_evidence", [])
    legacy = tags.get("evidence", [])
    combined = ((targets[:120] if isinstance(targets, list) else [])
        + (actions[:120] if isinstance(actions, list) else [])
        + (items[:120] if isinstance(items, list) else []) + (legacy[:120] if isinstance(legacy, list) else []))
    for item in combined:
        if not isinstance(item, dict):
            continue
        dimension, value = item.get("dimension", item.get("category")), item.get("value", item.get("tag"))
        if not isinstance(dimension, str):
            continue
        values = tags.get("task_types", []) if dimension == "task_types" else dimensions.get(dimension, [])
        values = values if isinstance(values, list) else []
        path, line, excerpt = item.get("path"), item.get("line"), _safe_text(item.get("excerpt"), 240)
        if (not isinstance(value, str) or value not in values or not isinstance(path, str)
                or not path or len(path) > 256 or path.startswith(("/", "\\"))
                or ":" in path or "\\" in path or ".." in path.split("/")
                or any(ord(char) < 32 for char in path) or not isinstance(line, int)
                or isinstance(line, bool) or line < 1 or not excerpt):
            continue
        cleaned = {"dimension": dimension, "value": value, "path": path, "line": line, "excerpt": excerpt}
        if cleaned not in result:
            result.append(cleaned)
    return result[:120]


def _framework_matches(candidate, current):
    """같은 프레임워크와 명확한 상위 기반 관계만 작업 문맥 일치로 인정한다."""
    return sorted(value for value in candidate if value in current or any(
        value in family and set(current) & family for family in FRAMEWORK_FAMILIES))


def _target_claim(dimension, value, excerpt):
    """수집기와 같은 지원 대상 선언 판별기를 사용한다."""
    return declares_target(dimension, value, excerpt)


def _target_dimensions(tags, dimensions, evidence):
    """새 문서 차원은 원문 선언만 강제 조건으로 쓰고 과거 직접 태그의 계약은 보존한다."""
    keys = ("languages", "frameworks", "tools")
    if not isinstance(tags.get("dimensions"), dict):
        return {key: dimensions[key] for key in keys}, {key: "legacy_declared" for key in keys}
    targets = {key: sorted({item["value"] for item in evidence if item["dimension"] == key
        and _target_claim(key, item["value"], item["excerpt"])}) for key in keys}
    return targets, {key: "explicit_claim" if targets[key] else "unknown" for key in keys}


def _freshness(version, change, source, prefer_recent, now):
    """검증한 저장소 시각만 약한 최신성 신호로 사용하고 출시일로 해석하지 않는다."""
    metadata = _dict(_get(version, "metadata_json"))
    verified = verified_source(metadata, _get(version, "revision", "") or "", _get(source, "kind"),
        metadata.get("source_url") or _get(change, "source_url"), _get(source, "url"))
    facts = _dict(_dict(metadata.get("repository")).get("facts")) if verified else {}
    reference = now if isinstance(now, datetime) else datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    for key in ("revision_committed_at", "repository_pushed_at"):
        stamp = timestamp(facts.get(key))
        if not stamp:
            continue
        age = (reference - datetime.fromisoformat(stamp.replace("Z", "+00:00"))).total_seconds() / 86400
        if age < 0:
            continue
        bonus = (3 if age <= 30 else 2 if age <= 90 else 1 if age <= 180 else 0) if prefer_recent else 0
        return {"status": "observed", "basis": key, "timestamp": stamp, "age_days": int(age),
            "bonus": bonus, "is_release_date": False}
    return {"status": "unknown", "basis": None, "timestamp": None, "age_days": None,
        "bonus": 0, "is_release_date": False}


def _workflow_score(workflow, capabilities, evidence):
    """명시된 공백의 보완 가능성을 가점하고 이미 수행하는 검사와의 중복은 감점한다."""
    fit, gap, overlap = [], 0, 0
    for step in workflow:
        matched = sorted(set(step["capabilities"]) & set(capabilities))
        if not matched:
            continue
        ratio = len(matched) / len(step["capabilities"])
        if step["status"] == "gap":
            gap += round(5 * step["weight"] * ratio)
        else:
            overlap -= round(2 * step["weight"] * ratio)
        fit.append({"step_id": step["id"], "label": step["label"], "status": step["status"],
            "weight": step["weight"], "matched_capabilities": matched,
            "relation": "fills_gap" if step["status"] == "gap" else "overlaps_existing",
            "source": step["source"], "evidence": [item for item in evidence
                if item["dimension"] == "capabilities" and item["value"] in matched]})
    return min(35, gap), max(-15, overlap), fit


def _browser_workflow_complement(context, dimensions, evidence):
    """전체 테스트 작성과 구분해 사용자가 명시한 브라우저 검증 공백만 보완한다."""
    if ("test_generation" not in context["task_types"] or "browser_testing" not in dimensions["capabilities"]
            or not any(item["dimension"] == "capabilities" and item["value"] == "browser_testing"
                       for item in evidence)):
        return None
    for method in context["methods"]:
        if method["task_type"] != "test_generation":
            continue
        steps = [step for step in method["workflow"] if step["source"] == "declared_workflow"
            and step["status"] == "gap" and "browser_testing" in step["capabilities"]]
        if steps:
            return method, steps
    return None


def _accessibility_workflow_complement(context, tags, dimensions, evidence):
    """접근성 검토 공백에 원문으로 확인한 감사나 검증 행동만 보완 후보로 연결한다."""
    declared_actions = _dict(tags.get("dimensions")).get("review_actions", [])
    source_evidence = [item for item in evidence if item["dimension"] == "capabilities"
        and item["value"] == "accessibility" and set(accessibility_review_actions(item["excerpt"])) & set(declared_actions)]
    if "code_review" not in context["task_types"] or "accessibility" not in dimensions["capabilities"] or not source_evidence:
        return None
    for method in context["methods"]:
        if method["task_type"] != "code_review":
            continue
        steps = [step for step in method["workflow"] if step["source"] == "declared_workflow"
            and step["status"] == "gap" and "accessibility" in step["capabilities"]]
        if steps:
            return method, steps, source_evidence
    return None


def _database_workflow_complement(context, tags, dimensions, evidence):
    """DB 검토를 명시한 후보만 사용자가 선언한 DB 변경 검토 공백에 연결한다."""
    actions = _dict(tags.get("dimensions")).get("review_actions", [])
    source_evidence = [item for item in evidence if item["dimension"] == "capabilities"
        and item["value"] == "database_changes" and set(database_review_actions(item["excerpt"])) & set(actions)]
    if "code_review" not in context["task_types"] or "database_changes" not in dimensions["capabilities"] or not source_evidence:
        return None
    for method in context["methods"]:
        if method["task_type"] != "code_review":
            continue
        steps = [step for step in method["workflow"] if step["source"] == "declared_workflow"
            and step["status"] == "gap" and "database_changes" in step["capabilities"]]
        if steps:
            return method, steps, source_evidence
    return None


def rank_candidate(profile, methods, environment, asset, version, change, source, now=None):
    """실행 승인이나 성능 추정 없이 후보의 작업 관련성과 확인할 조건을 반환한다."""
    context = build_context(profile, methods, environment)
    tags = _dict(_get(version, "tags"))
    dimensions = _candidate_dimensions(tags)
    evidence = _evidence(tags, dimensions)
    targets, target_basis = _target_dimensions(tags, dimensions, evidence)
    effective, declared = context["effective"], context["declared"]
    candidate_tasks = _names(tags.get("task_types", []))
    tasks = sorted(set(candidate_tasks) & set(context["task_types"]))
    complement = _browser_workflow_complement(context, dimensions, evidence) if not tasks else None
    complement_cap = "browser_testing" if complement else None
    accessibility_complement = _accessibility_workflow_complement(context, tags, dimensions, evidence) if not tasks and not complement else None
    if accessibility_complement:
        complement, complement_cap = accessibility_complement[:2], "accessibility"
    database_complement = _database_workflow_complement(context, tags, dimensions, evidence) if not tasks and not complement else None
    if database_complement:
        complement, complement_cap = database_complement[:2], "database_changes"
    route_tasks = tasks or ([complement[0]["task_type"]] if complement else [])
    relevance_caps = [complement_cap] if complement else dimensions["capabilities"]
    matches = {key: sorted(set(targets[key]) & set(effective.get(key, [])))
        for key in ("languages", "tools")}
    matches["frameworks"] = _framework_matches(targets["frameworks"], effective["frameworks"])
    for key in ("languages", "frameworks", "tools"):
        matches["mentioned_" + key] = sorted(set(dimensions[key]) - set(targets[key]))
    matches["goals"] = sorted(set(relevance_caps) & set(effective["goals"]))
    matches.update({"task_types": tasks, "task_type": route_tasks[0] if route_tasks else None,
        "candidate_task_types": candidate_tasks, "relationship": "workflow_complement" if complement else "same_task",
        "capabilities": dimensions["capabilities"], "confidence": "limited",
        "target_claims": targets, "target_scope_basis": target_basis})
    excluded, uncertainties, reasons = [], [], []
    if not route_tasks:
        excluded.append("등록한 작업 유형과 일치하지 않음")
    if complement:
        if complement_cap == "browser_testing":
            uncertainties.append("테스트 코드 작성 전체 지원은 확인하지 못함")
        else:
            label = "접근성 감사 및 검증" if complement_cap == "accessibility" else "DB 모델 및 변경 검토"
            uncertainties.append(label + " 단계의 보완이며 코드 리뷰 전체 지원은 확인하지 못함")
            source_action_evidence = (accessibility_complement or database_complement)[2]
            matches["source_action_evidence"] = source_action_evidence
            matches["source_actions"] = sorted({action for item in source_action_evidence
                for action in declared_review_actions(item["excerpt"])})
        matches["complementary_capability"] = complement_cap
    if _get(source, "enabled", True) is False:
        excluded.append("수집 소스가 비활성 상태임")
    if _dict(_dict(_get(version, "metadata_json")).get("repository")).get("archived") is True:
        excluded.append("보관 처리된 저장소임")
    for key, label in (("languages", "언어"), ("frameworks", "프레임워크")):
        target_matches = (_framework_matches(targets[key], effective[key]) if key == "frameworks"
            else sorted(set(targets[key]) & set(effective[key])))
        if targets[key] and effective[key] and not target_matches:
            excluded.append(f"현재 프로젝트 {label}와 후보의 명시된 대상이 다름")
        elif not targets[key]:
            uncertainties.append(f"문서에 {label} 언급은 있으나 지원 대상 선언은 미확인" if dimensions[key]
                else f"후보의 지원 {label} 미확인")
    candidate_runners, current_runners = set(targets["tools"]) & AI_TOOLS, set(effective["tools"]) & AI_TOOLS
    if candidate_runners and current_runners and not candidate_runners & current_runners:
        excluded.append("현재 AI 도구와 후보의 명시된 대상이 다름")
    if not candidate_runners:
        uncertainties.append("후보의 지원 AI 도구 미확인")
    for key, label in (("languages", "언어"), ("frameworks", "프레임워크"), ("tools", "도구")):
        if matches["mentioned_" + key]:
            uncertainties.append(f"문서의 {label} 단순 언급은 지원 대상 확인이나 관련성 가점에 사용하지 않음: "
                + ", ".join(matches["mentioned_" + key]))
    if set(dimensions["capabilities"]) & set(declared["excluded_capabilities"]):
        excluded.append("사용자가 제외한 기능을 포함함")
    if set(targets["tools"]) & set(declared["excluded_tools"]):
        excluded.append("사용자가 제외한 도구를 대상으로 함")
    digest = _get(version, "bundle_sha256")
    installed = _get(environment, "installed_skills", [])
    installed = installed if isinstance(installed, list) else []
    if (isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest)
            and any(isinstance(skill, dict) and skill.get("excluded") is not True
                and skill.get("bundle_sha256") == digest for skill in installed)):
        excluded.append("동일한 전체 번들 버전이 이미 설치됨")
    if environment is None:
        uncertainties.append("현재 연결 프로젝트의 관측 환경이 없음")
    for key in ("languages", "frameworks"):
        if context["observed"][key] and declared[key] and not set(context["observed"][key]) & set(declared[key]):
            uncertainties.append(f"등록한 {key}와 스캔 결과가 달라 현재 프로젝트 관측값을 우선함")
    extra_tools = sorted(set(targets["tools"]) - AI_TOOLS - set(effective["tools"]))
    if extra_tools:
        uncertainties.append("추가 도구의 설치 조건 확인 필요: " + ", ".join(extra_tools))
    freshness = _freshness(version, change, source, declared["prefer_recent"], now)
    if freshness["status"] == "unknown":
        uncertainties.append("확인된 원본 변경 시각이 없어 최신성 가점을 부여하지 않음")
    breakdown = {"task_relevance": 18 if tasks else 0, "goal_fit": min(24, 8 * len(matches["goals"])),
        "language_fit": 6 if matches["languages"] else 0, "framework_fit": 10 if matches["frameworks"] else 0,
        "tool_fit": 4 if set(matches["tools"]) & AI_TOOLS else 0, "method_alignment": 0,
        "workflow_gap": 0, "workflow_overlap": 0,
        "freshness": freshness["bonus"]}
    selected, best = None, None
    for task in route_tasks:
        method = complement[0] if complement else next((item for item in context["methods"] if item["task_type"] == task), None)
        workflow = complement[1] if complement else method["workflow"] if method else []
        gap, overlap, fit = _workflow_score(workflow, relevance_caps, evidence)
        alignment = min(24, sum(12 if item["source"] == "required_check" else 8
            for item in fit if item["source"] in {"required_check", "explicit_prompt"}))
        choice = (gap + overlap + alignment, len(fit), task, method, gap, overlap, fit, alignment)
        if best is None or choice[:2] > best[:2]:
            best = choice
    workflow_fit = []
    if best:
        (_, _, task, selected, breakdown["workflow_gap"], breakdown["workflow_overlap"],
         workflow_fit, breakdown["method_alignment"]) = best
        matches["task_type"] = task
    workflow = complement[1] if complement else selected["workflow"] if selected else []
    gaps = {capability for step in workflow if step["status"] == "gap"
        for capability in step["capabilities"]}
    explicit_checks = {capability for step in workflow if step["source"] in {"required_check", "explicit_prompt"}
        for capability in step["capabilities"]}
    covered = {capability for step in workflow if step["source"] == "declared_workflow" and step["status"] == "covered"
        for capability in step["capabilities"]}
    requested = set(effective["goals"]) | gaps | explicit_checks
    candidate_caps = set(relevance_caps)
    matches["requested_capabilities"] = sorted(requested)
    matches["matched_requested_capabilities"] = sorted(candidate_caps & requested)
    if not candidate_caps:
        excluded.append("후보에 확인된 기능이 없어 개인화 추천을 보류함")
    elif requested and not candidate_caps & requested:
        excluded.append("등록한 목표나 작업 공백 및 필수 검사와 연결되는 기능이 없어 추천을 보류함")
    if candidate_caps and candidate_caps <= covered and not candidate_caps & gaps:
        excluded.append("후보 기능을 현재 작업 단계에서 모두 수행하고 있어 새로운 보완 근거가 없음")
    has_context_fit = any(matches[key] for key in ("goals", "languages", "frameworks", "tools")) or bool(workflow_fit)
    if route_tasks and not has_context_fit:
        excluded.append("작업 유형 외에 개인 작업과 연결할 근거가 부족하여 추천을 보류함")
        matches["confidence"] = "abstained"
    elif has_context_fit:
        matches["confidence"] = "contextual"
    if complement:
        label = {"browser_testing": "브라우저 검증", "accessibility": "접근성 감사 및 검증",
                 "database_changes": "DB 모델 및 변경 검토"}[complement_cap]
        reasons.append("등록한 " + label + " 단계를 보완할 후보로 연결함")
    elif tasks:
        reasons.append("현재 " + ("코드 리뷰" if matches["task_type"] == "code_review" else "테스트 작성") + " 작업과 관련 있음")
    if matches["goals"]:
        reasons.append("등록한 목표와 연결되는 기능: " + ", ".join(CAP_LABELS[item] for item in matches["goals"]))
    for key, label in (("languages", "프로젝트 언어"), ("frameworks", "프로젝트 프레임워크"), ("tools", "사용 도구")):
        if matches[key]:
            reasons.append(label + " 문맥과 일치: " + ", ".join(matches[key]))
    for item in workflow_fit[:5]:
        capability_labels = ", ".join(CAP_LABELS[cap] for cap in item["matched_capabilities"])
        reasons.append((f"등록한 '{item['label']}' 단계의 보완 후보: " if item["status"] == "gap"
            else f"기존 '{item['label']}' 검사와 중복되어 대체 효과는 실제 비교 필요: ") + capability_labels)
    if dimensions["capabilities"] and not any(item["dimension"] == "capabilities" for item in evidence):
        uncertainties.append("기능 태그의 원문 위치 근거가 없어 추가 확인 필요")
    matches["method_id"] = selected["id"] if selected else None
    matches["method_version"] = selected["version"] if selected else None
    if excluded:
        matches["confidence"] = "abstained"
    return {"eligible": not excluded, "score": max(0, min(100, sum(breakdown.values()))),
        "reasons": reasons, "excluded_reasons": excluded, "breakdown": breakdown,
        "matches": matches, "workflow_fit": workflow_fit, "uncertainties": uncertainties,
        "freshness": freshness, "evidence": evidence, "context_fingerprint": context["fingerprint"]}
