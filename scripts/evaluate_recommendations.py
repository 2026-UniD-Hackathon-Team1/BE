"""별도 PostgreSQL과 실제 HTTP로 여러 작업 문맥의 추천 근거를 평가한다."""
import argparse
import html
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.catalog.classification import CLASSIFIER_VERSION, classify_bundle
from backend.catalog.github_client import SkillBundle
from backend.catalog.service import bundle_payload, stable_id
from backend.login import hash_password
from backend.models import Asset, AssetVersion, ChangeEvent, Environment, Source, UserAccount
from scripts.simulate_user_flow import Simulation, redact

CAP_PROSE = {"input_validation": "input validation", "exception_handling": "exception handling",
    "regression_test": "regression tests", "test_coverage": "test coverage", "documentation": "documentation",
    "api_security": "API security", "accessibility": "accessibility", "performance": "performance analysis",
    "browser_testing": "browser testing", "test_design": "test design",
    "database_changes": "database migrations", "typing": "type safety"}
# 기대 결과는 순위 구현의 계산값이 아니라 각 시나리오의 목적을 보고 직접 정한다.
CORPUS = [
    ("py-validation", "code_review", "Python", "FastAPI", "Codex CLI", ["input_validation", "exception_handling"], 2),
    ("py-security", "code_review", "Python", "FastAPI", "Codex CLI", ["api_security", "input_validation"], 2),
    ("py-regression", "code_review", "Python", "FastAPI", "Codex CLI", ["regression_test", "test_coverage"], 2),
    ("django-validation", "code_review", "Python", "Django", "Codex CLI", ["input_validation"], 2),
    ("react-browser", "test_generation", "TypeScript", "React", "Codex CLI", ["browser_testing", "test_design", "accessibility"], 2),
    ("react-typing", "code_review", "TypeScript", "React", "Codex CLI", ["typing"], 2),
    ("py-performance", "code_review", "Python", "FastAPI", "Codex CLI", ["performance"], 2),
    ("py-docs", "code_review", "Python", "FastAPI", "Codex CLI", ["documentation"], 2),
    ("general-docs", "code_review", "", "", "Codex CLI", ["documentation"], 2),
    ("rust-review", "code_review", "Rust", "", "Codex CLI", ["input_validation"], 2),
    ("claude-security", "code_review", "Python", "FastAPI", "Claude Code", ["api_security"], 2),
    ("cursor-browser", "test_generation", "TypeScript", "React", "Cursor IDE", ["browser_testing"], 2),
    ("py-security-old", "code_review", "Python", "FastAPI", "Codex CLI", ["api_security", "input_validation"], 365),
    ("java-database", "code_review", "Java", "Spring Boot", "Codex CLI", ["database_changes"], 2),
    ("py-generic", "code_review", "Python", "FastAPI", "Codex CLI", [], 2),
    ("unknown-review", "code_review", "", "", "", [], 2),
    ("archived-security", "code_review", "Python", "FastAPI", "Codex CLI", ["api_security"], 2),
    ("react-accessibility", "code_review", "TypeScript", "React", "Codex CLI", ["accessibility"], 2),
    ("go-validation", "code_review", "Go language", "", "Codex CLI", ["input_validation"], 2),
    ("rust-test-design", "test_generation", "Rust", "", "Codex CLI", ["test_design"], 2),
    ("java-api-security", "code_review", "Java", "Spring Boot", "Codex CLI", ["api_security"], 2),
    ("flask-database", "code_review", "Python", "Flask", "Codex CLI", ["database_changes"], 2),
    ("go-docs", "code_review", "Go language", "", "Codex CLI", ["documentation"], 2),
    ("react-security", "code_review", "TypeScript", "React", "Codex CLI", ["api_security"], 2),
    ("java-docs", "code_review", "Java", "Spring Boot", "Codex CLI", ["documentation"], 2),
]


def scenarios(marker):
    """같은 언어라도 검사 공백에 따라 기대 추천이 달라지는 가상 작업을 정의한다."""
    rows = [
        ("FastAPI 보안 공백", "python", "fastapi", "code_review", ["api_security"], ["api_security"], "codex_cli", ["py-security", "py-security-old"]),
        ("FastAPI 회귀 검사 공백", "python", "fastapi", "code_review", ["regression_test", "test_coverage"], ["regression_test", "test_coverage"], "codex_cli", ["py-regression"]),
        ("React 브라우저 테스트", "typescript", "react", "test_generation", ["browser_testing", "test_design"], ["browser_testing", "test_design"], "codex_cli", ["react-browser"]),
        ("Django 입력 검증", "python", "django", "code_review", ["input_validation"], ["input_validation"], "codex_cli", ["django-validation"]),
        ("FastAPI 성능 검토", "python", "fastapi", "code_review", ["performance"], ["performance"], "codex_cli", ["py-performance"]),
        ("FastAPI 문서화", "python", "fastapi", "code_review", ["documentation"], ["documentation"], "codex_cli", ["py-docs", "general-docs"]),
        ("Claude Code 사용", "python", "fastapi", "code_review", ["api_security"], ["api_security"], "claude_code", ["claude-security"]),
        ("React 접근성 리뷰", "typescript", "react", "code_review", ["accessibility"], ["accessibility"], "codex_cli", ["react-accessibility"]),
        ("Go 입력 검증 리뷰", "go", "", "code_review", ["input_validation"], ["input_validation"], "codex_cli", ["go-validation"]),
        ("Rust 테스트 설계 생성", "rust", "", "test_generation", ["test_design"], ["test_design"], "codex_cli", ["rust-test-design"]),
        ("Java Spring DB 및 API 리뷰", "java", "spring", "code_review", ["database_changes", "api_security"], ["database_changes", "api_security"], "codex_cli", ["java-database", "java-api-security"]),
        ("Flask DB 변경 리뷰", "python", "flask", "code_review", ["database_changes"], ["database_changes"], "codex_cli", ["flask-database"]),
        ("검사 항목 모두 수행 중", "python", "fastapi", "code_review", [], [], "codex_cli", []),
        ("작업 환경 미확인", "unknown", "", "code_review", [], [], "", []),
        ("한 보안 번들 설치 후 다른 대안", "python", "fastapi", "code_review", ["api_security"], ["api_security"], "codex_cli", ["py-security-old"]),
        ("manifest 해시만 알고 있음", "python", "fastapi", "code_review", ["api_security"], ["api_security"], "codex_cli", ["py-security", "py-security-old"]),
        ("Flask 타입 검사 후보 없음", "python", "flask", "code_review", ["typing"], ["typing"], "codex_cli", []),
    ]
    scenario_ids = ["fastapi-security", "fastapi-regression", "react-browser", "django-validation",
        "fastapi-performance", "fastapi-docs", "claude-security", "react-accessibility", "go-validation",
        "rust-test-design", "java-spring-review", "flask-database", "covered-all", "unknown-context",
        "installed-bundle", "manifest-only", "no-matching-candidate"]
    rationales = ["FastAPI와 Codex 대상 중 API 보안 공백을 명시적으로 지원하는 두 후보만 관련 있음",
        "회귀 검사와 커버리지를 함께 제공하는 FastAPI 후보만 관련 있음",
        "React와 Codex 대상의 브라우저 검사 및 테스트 설계 후보만 관련 있음",
        "Django 입력 검증 후보만 관련 있으며 FastAPI 전용 후보는 대상이 다름",
        "등록한 FastAPI 성능 분석 공백을 지원하는 후보만 관련 있음",
        "FastAPI 전용 및 언어 제한을 명시하지 않은 문서화 후보 두 개가 관련 있음",
        "Claude Code를 명시한 보안 후보만 관련 있으며 Codex 전용 후보는 대상이 다름",
        "React와 Codex 대상 중 접근성 검토를 명시한 후보만 관련 있음",
        "Go 입력 검증 후보만 관련 있으며 Go 문서화와 다른 언어 후보는 목적이 다름",
        "Rust 테스트 생성 중 테스트 설계를 명시한 후보만 관련 있음",
        "Java Spring 대상 DB 변경 또는 API 보안을 명시한 두 후보가 관련 있음",
        "Python Flask 대상 DB 변경 후보만 관련 있음",
        "기능을 모두 기존 단계에서 수행하므로 새 공백 보완 후보가 없어야 함",
        "목표와 기술 문맥이 확인되지 않아 추천을 보류해야 함",
        "동일 전체 번들은 제외하고 같은 목적의 별도 자산 대안은 유지함. 설치 이력 버전 검증과 구분",
        "manifest 해시만으로 전체 번들 동일성을 단정하지 않아 보안 후보 두 개를 유지함",
        "Python Flask의 타입 검사 공백을 지원하는 후보가 고정 목록에 없어 추천을 보류해야 함"]
    result = []
    for index, (name, language, framework, task, goals, gaps, tool, expected) in enumerate(rows):
        scenario_id = scenario_ids[index]
        covered = list(CAP_PROSE) if scenario_id == "covered-all" else ["input_validation"] if scenario_id == "fastapi-security" else []
        workflow = [{"id": "covered", "label": "이미 수행하는 검사", "capabilities": covered,
                     "status": "covered", "weight": 4}] if covered else []
        if gaps:
            workflow.append({"id": "gap", "label": "보완이 필요한 검사", "capabilities": gaps,
                             "status": "gap", "weight": 5})
        result.append({"scenario_id": scenario_id, "scenario_kind": "positive" if index < 12 else "negative",
            "name": name, "expected": {"relevant": expected, "empty": not expected,
                "rationale": rationales[index]},
            "payload": {"role": "개발 작업 평가 " + marker, "primary_language": language,
                "task_types": [task], "goals": goals, "context": {"frameworks": [framework] if framework else [],
                    "tools": [tool] if tool else [], "prefer_recent": True}},
            "method_body": {"task_type": task, "prompt_text": "비공개 작업 설명 " + marker,
                "workflow": workflow}, "installed": "full" if scenario_id == "installed-bundle" else "manifest" if scenario_id == "manifest-only" else None})
    return result


class RecommendationEvaluation(Simulation):
    def __init__(self, database_url, *, live_github=False, catalog_cache=None, source_reviews=None):
        """기존 시연과 다른 실행 폴더와 DB 스키마에 평가를 격리한다."""
        self.people, self.fixture_versions, self.fixture_documents, self.checks = [], {}, {}, []
        self.fixture_source_id = "github:local-evaluation/fixture-skills"
        self.catalog_cache = Path(catalog_cache) if catalog_cache else None
        self.source_reviews = Path(source_reviews) if source_reviews else None
        super().__init__(database_url, live_github=live_github)

    def render(self):
        """기록 JSON은 보존하고 실제 수집과 합성 평가를 분리한 요약을 갱신한다."""
        from scripts.recommendation_report import render_recommendation_report
        from backend.catalog.dimensions import DIMENSIONS_VERSION

        summary = {"checks_passed": sum(item["passed"] for item in self.checks), "checks_total": len(self.checks),
            "personas": len(self.people), "fixture_candidates": len(self.fixture_versions),
            "positive_profiles": sum(person.get("scenario_kind") == "positive" for person in self.people),
            "negative_cases": sum(person.get("scenario_kind") == "negative" for person in self.people),
            "transport": "actual_loopback_http", "database": "isolated_local_postgresql_schema",
            "paid_model_calls": 0, "performance_compared": False, "quality_metrics": self.quality_summary(),
            "live_collection": getattr(self, "live_summary", {}), "live_requested": self.live_github or bool(self.catalog_cache),
            "api": self.api, "viewer": self.viewer_url}
        summary.update(classifier_version=CLASSIFIER_VERSION, dimensions_version=DIMENSIONS_VERSION)
        report = {"summary": summary, "checks": self.checks, "people": redact(self.people), "events": self.events}
        (self.public / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        render_recommendation_report(report, self.public / "index.html")

    def check(self, title, condition, data=None):
        """독립적으로 정한 기대 조건의 통과 여부를 JSON 수치와 함께 기록한다."""
        self.checks.append({"title": title, "passed": bool(condition)})
        self.require("추천 검증", title, condition, data)

    def quality_summary(self):
        """고정 정답 집합의 정밀도와 재현율 및 부적절한 후보 누출을 합산한다."""
        metrics = [person["metrics"] for person in self.people if "metrics" in person]
        top_count = sum(item["top3_returned"] for item in metrics)
        relevant_count = sum(item["relevant_total"] for item in metrics)
        result = {"evaluation_basis": "manually_defined_synthetic_expected_sets", "evaluated_profiles": len(metrics),
            "top3_precision": sum(item["top3_relevant"] for item in metrics) / top_count if top_count else None,
            "relevant_recall": sum(item["relevant_returned"] for item in metrics) / relevant_count if relevant_count else None,
            "negative_leakage_count": sum(item["negative_leakage_count"] for item in metrics),
            "abstention_count": sum(item["abstained"] for item in metrics),
            "expected_abstention_count": sum(item["expected_abstention"] for item in metrics),
            "correct_abstention_count": sum(item["abstained"] and item["expected_abstention"] for item in metrics),
            "precision_denominator": "actual_returned_up_to_3_per_profile", "production_accuracy_claimed": False}
        for kind in ("positive", "negative"):
            group = [person["metrics"] for person in self.people if "metrics" in person and person.get("scenario_kind") == kind]
            top_total = sum(item["top3_returned"] for item in group)
            relevant_total = sum(item["relevant_total"] for item in group)
            result[kind + "_cases"] = {"evaluated": len(group),
                "top3_precision": sum(item["top3_relevant"] for item in group) / top_total if top_total else None,
                "relevant_recall": sum(item["relevant_returned"] for item in group) / relevant_total if relevant_total else None,
                "negative_leakage_count": sum(item["negative_leakage_count"] for item in group),
                "abstention_count": sum(item["abstained"] for item in group),
                "nonempty_count": sum(not item["abstained"] for item in group)}
        return result

    def person(self, scenario_id):
        """목록 순서에 의존하지 않고 고정 시나리오 키로 작업을 찾는다."""
        return next(person for person in self.people if person["scenario_id"] == scenario_id)

    def seed_corpus(self):
        """작성한 문서를 실제 번들 해시와 분류기로 처리하고 고정 평가 출처로 저장한다."""
        revision, base = "e" * 40, "https://github.com/local-evaluation/fixture-skills"
        now, current = datetime.now(timezone.utc), {}
        with self.sessions.begin() as session:
            session.add(Source(id=self.fixture_source_id, kind="github_repo", url=base, enabled=True))
            session.flush()
            for key, task, language, framework, tool, capabilities, age in CORPUS:
                task_text = "code review" if task == "code_review" else "test generation"
                lines = [f"# Fixed evaluation fixture: {key}", f"Provides {task_text}."]
                if language or framework or tool:
                    lines.append("Supports " + " and ".join(filter(None, [language, framework, tool])) + ".")
                if capabilities:
                    lines.append("Provides " + ", ".join(CAP_PROSE[item] for item in capabilities) + ".")
                bundle = SkillBundle("skills/" + key, {"SKILL.md": "\n".join(lines).encode("utf-8")})
                stamp = (now - timedelta(days=age)).isoformat().replace("+00:00", "Z")
                repository = {"archived": key == "archived-security", "facts": {
                    "publisher": "local-evaluation", "repository_license": "MIT", "revision_committed_at": stamp}}
                payload, digest = bundle_payload(bundle, repository)
                asset_id = stable_id("asset", self.fixture_source_id + ":" + key)
                version_id = stable_id("ver", asset_id + ":" + digest)
                url = base + "/tree/" + revision + "/skills/" + key
                session.add(Asset(id=asset_id, kind="skill", canonical_key=self.fixture_source_id + ":" + key,
                    display_name="[고정 평가] " + key, source_id=self.fixture_source_id))
                session.flush()
                session.add(AssetVersion(id=version_id, asset_id=asset_id, revision=revision,
                    bundle_sha256=digest, tags=classify_bundle(bundle.files), metadata_json={"bundle": payload,
                        "repository": repository, "source_url": url, "source_verified": True,
                        "simulation_fixture": True, "classification_status": "rule_classified",
                        "classifier_version": CLASSIFIER_VERSION}))
                session.flush()
                session.add(ChangeEvent(id=stable_id("change", version_id), asset_id=asset_id,
                    to_version_id=version_id, change_type="new", source_url=url))
                current[asset_id] = version_id
                self.fixture_versions[key] = {"version_id": version_id, "bundle_sha256": digest}
                self.fixture_documents[version_id] = {path: content.decode("utf-8") for path, content in bundle.files.items()}
            session.get(Source, self.fixture_source_id).sync_state = {"current_versions": current, "simulation_fixture": True}
        self.record("고정 평가 전제", f"직접 작성한 {len(CORPUS)}개 문서를 실제 번들 해시와 분류기로 저장", "fixture",
            {"candidate_keys": list(self.fixture_versions), "external_requests": 0,
             "source_verified_flag": "합성 원본의 입력 계약 검사용이며 실제 GitHub 검증 이력이 아님"})

    def prepare_people(self):
        """가입 빈도 제한을 우회하는 요청 없이 시연 계정을 준비하고 실제 HTTP로 로그인한다."""
        self.people = scenarios("private-evaluation-marker-" + self.tag)
        password_hash = hash_password(self.password)
        with self.sessions.begin() as session:
            for index, person in enumerate(self.people):
                person.update(profile_id=f"evaluation_{self.tag}_{index}", username=f"eval_{self.tag}_{index}")
                session.add(UserAccount(id=person["profile_id"], username=person["username"], password_hash=password_hash))
        self.record("고정 평가 전제", "격리 DB에 가상 계정만 준비하고 계정별 실제 로그인 사용", "fixture",
            {"accounts": len(self.people), "shared_hash": "같은 임시 시연 비밀번호에만 사용", "signup_limit_modified": False})
        for person in self.people:
            login = self.call("로그인", person["name"] + " 로그인", "POST", "/api/auth/login", auth="none",
                body={"username": person["username"], "password": self.password}, log=False)
            self.user = person["headers"] = {"Authorization": "Bearer " + login["token"]}
            self.call("프로필", person["name"] + " 저장", "PUT", "/api/profile", body=person["payload"], log=False)
            person["method"] = self.call("작업 단계", person["name"] + " 작업 방식 등록", "POST", "/api/work-methods",
                body=person["method_body"], expected=(201,), log=False)
            if person["installed"]:
                installed = self.fixture_versions["py-security"]
                skill = {"name": "py-security", "excluded": False,
                    "bundle_sha256" if person["installed"] == "full" else "manifest_sha256": installed["bundle_sha256"]}
                with self.sessions.begin() as session:
                    session.add(Environment(id="environment_" + person["profile_id"], profile_id=person["profile_id"],
                        runner="codex_cli", runner_version="evaluation", model_id="not-executed", installed_skills=[skill],
                        settings_hash="evaluation-only", scan_root="evaluation-only", metadata_json={}))

    def query(self, person, suffix=""):
        """선택한 가상 계정으로 실제 추천 API를 조회한다."""
        self.user = person["headers"]
        return self.call("실제 추천 HTTP", person["name"] + " 추천 조회", "GET",
            "/api/recommendations?include_excluded=true" + suffix, log=False)

    def keys(self, result, *, top=None):
        """버전 식별자를 사람이 읽을 수 있는 고정 평가 후보 이름으로 바꾼다."""
        lookup = {item["version_id"]: key for key, item in self.fixture_versions.items()}
        items = result.get("items", [])
        return [lookup.get(item["candidate_version_id"], item["candidate_version_id"]) for item in items[:top]]

    def evaluate_people(self):
        """각 작업의 전체 정답 집합과 제외 대상 및 실제 원문 근거를 검증한다."""
        for person in self.people:
            result = person["result"] = self.query(person)
            returned, top = set(self.keys(result)), self.keys(result, top=3)
            expected = person["expected"]
            relevant = set(expected["relevant"])
            person["metrics"] = {"top3_precision": len(set(top) & relevant) / len(top) if top else None,
                "top3_returned": len(top), "top3_relevant": len(set(top) & relevant),
                "relevant_recall": len(returned & relevant) / len(relevant) if relevant else None,
                "relevant_total": len(relevant), "relevant_returned": len(returned & relevant),
                "negative_leakage_count": len(returned - relevant), "unexpected_candidates": sorted(returned - relevant),
                "missed_relevant_candidates": sorted(relevant - returned), "abstained": not returned,
                "expected_abstention": expected["empty"]}
            self.check(person["name"] + " 전체 정답 집합 일치 및 부적절한 후보 누출 없음", returned == relevant,
                {"returned": sorted(returned), "expected": expected, "metrics": person["metrics"]})
            self.check(person["name"] + " 추천 기능의 원문 위치와 문구 확인", all(self.valid_evidence(item) for item in result["items"]),
                [{"title": item["title"], "matched_capabilities": item["assessment"]["matches"].get("matched_requested_capabilities", []),
                  "evidence": item["assessment"].get("evidence", [])} for item in result["items"]])
            self.check(person["name"] + " 실행 및 성능 미확인 표기", all(
                item["support_level"] == "information_only" and item["performance_status"] == "not_compared"
                for item in result["items"]))
        self.check("정상 작업 12개는 합성 원문에서 각 목적에 맞는 후보를 확인함", len([
            person for person in self.people if person["scenario_kind"] == "positive" and person["result"]["items"]]) == 12)
        self.check("동일 Python 환경에서도 보안 공백과 회귀 공백의 1순위가 달라짐",
            self.keys(self.person("fastapi-security")["result"], top=1) != self.keys(self.person("fastapi-regression")["result"], top=1))
        self.check("Django 작업에서 FastAPI 전용 후보 제외", "py-security" not in self.keys(self.person("django-validation")["result"]))
        self.check("Claude Code 작업에서 Codex 전용 후보 제외", "py-security" not in self.keys(self.person("claude-security")["result"]))
        self.check("모든 검사를 이미 수행하는 작업은 추천을 보류함", self.person("covered-all")["result"]["items"] == [])
        self.check("보안 공백에 문서화와 성능 및 기능 미확인 일반 후보가 섞이지 않음",
            not {"py-docs", "py-performance", "py-generic", "py-validation"} & set(self.keys(self.person("fastapi-security")["result"])))
        self.check("동일 전체 번들 설치는 제외하고 manifest 해시만 같으면 제외하지 않음",
            "py-security" not in self.keys(self.person("installed-bundle")["result"]) and "py-security" in self.keys(self.person("manifest-only")["result"]))
        security = self.keys(self.person("fastapi-security")["result"])
        self.check("관련성 조건이 같은 후보 중 최근 원본이 과거 원본보다 먼저 나옴",
            security.index("py-security") < security.index("py-security-old"))
        self.check("보관 저장소는 추천하지 않음", all("archived-security" not in self.keys(person["result"]) for person in self.people))

    def valid_evidence(self, item):
        """추천에 연결된 각 기능의 근거가 저장한 원본 문서의 해당 줄에 있는지 확인한다."""
        assessment = item.get("assessment", {})
        matched = assessment.get("matches", {}).get("matched_requested_capabilities", [])
        documents = self.fixture_documents.get(item["candidate_version_id"], {})
        observed = set()
        for evidence in assessment.get("evidence", []):
            if evidence.get("dimension") != "capabilities":
                continue
            lines = documents.get(evidence.get("path"), "").splitlines()
            number, excerpt = evidence.get("line", 0), evidence.get("excerpt", "")
            if isinstance(number, int) and 1 <= number <= len(lines) and excerpt and len(excerpt) <= 240 and excerpt in lines[number - 1]:
                observed.add(evidence.get("value"))
        return bool(matched) and set(matched) <= observed

    def edit_and_feedback(self):
        """프로필 재계산과 작업 방식 불변 버전 및 숨김 선택 보존을 검증한다."""
        person = self.person("fastapi-security")
        before = person["result"]
        changed = {**person["payload"], "goals": ["performance"], "context": {**person["payload"]["context"],
            "excluded_capabilities": ["api_security", "input_validation"]}}
        self.user = person["headers"]
        self.call("프로필 수정", "성능 중심 문맥으로 변경", "PUT", "/api/profile", body=changed, log=False)
        after = self.query(person)
        self.check("프로필 수정이 지문과 상위 추천에 반영됨", self.keys(after, top=1) == ["py-performance"]
            and before["context"]["fingerprint"] != after["context"]["fingerprint"])
        self.call("프로필 수정", "원래 프로필로 복원", "PUT", "/api/profile", body=person["payload"], log=False)
        original = person["method"]
        body = {**person["method_body"], "workflow": [{"id": "regression", "label": "회귀 검사 공백",
            "capabilities": ["regression_test", "test_coverage"], "status": "gap", "weight": 5}]}
        updated = self.call("작업 방식 버전", "새 회귀 검사 방식 저장", "POST", "/api/work-methods", body=body, expected=(201,), log=False)
        after = self.query(person)
        old = self.call("작업 방식 버전", "기존 버전 다시 조회", "GET",
            f'/api/work-methods/{original["method_id"]}?version={original["version"]}', log=False)
        self.check("작업 단계 변경이 새 버전과 재추천에 반영되고 이전 버전은 보존됨",
            updated["method_id"] == original["method_id"] and updated["version"] == original["version"] + 1
            and old["workflow"] == original["workflow"] and self.keys(after, top=1) == ["py-regression"]
            and before["context"]["fingerprint"] != after["context"]["fingerprint"])
        person["method"] = self.call("작업 방식 버전", "원래 단계의 새 버전 저장", "POST", "/api/work-methods",
            body=person["method_body"], expected=(201,), log=False)
        person = self.person("react-browser")
        result = self.query(person)
        recommendation_id = result["items"][0]["recommendation_id"]
        self.call("선택 보존", "브라우저 후보 관심 없음", "PATCH", "/api/recommendations/" + recommendation_id,
            body={"status": "dismissed", "reason": "평가용 사용자 선택"}, expected=(204,), log=False)
        self.call("선택 보존", "프로필 갱신 후 재계산", "PUT", "/api/profile",
            body={**person["payload"], "context": {**person["payload"]["context"], "prefer_recent": False}}, log=False)
        visible, hidden = self.query(person), self.query(person, "&include_hidden=true")
        self.check("재계산 후에도 관심 없음 선택과 숨김이 유지됨", all(
            item["recommendation_id"] != recommendation_id for item in visible["items"]) and any(
            item["recommendation_id"] == recommendation_id and item["status"] == "dismissed" for item in hidden["items"]))
        self.call("선택 보존", "평가용 숨김 선택 복원", "PATCH", "/api/recommendations/" + recommendation_id,
            body={"status": "new"}, expected=(204,), log=False)
        self.call("선택 보존", "원래 최신성 설정 복원", "PUT", "/api/profile", body=person["payload"], log=False)

    def check_privacy(self):
        """공개 검색 계획에서 작업 원문을 제거하고 다른 계정의 방식 접근을 막는다."""
        person = self.person("fastapi-security")
        self.user = person["headers"]
        plan = self.call("검색 개인정보", "내 기술 문맥의 공개 검색 계획", "GET", "/api/catalog/discovery-plan", log=False)
        self.check("공개 검색 계획에 역할이나 작업 프롬프트 원문을 넣지 않음",
            "private-evaluation-marker-" + self.tag not in json.dumps(plan, ensure_ascii=False), plan)
        other = self.person("fastapi-regression")["method"]
        self.call("사용자 격리", "다른 계정의 작업 방식 접근 거부", "GET",
            f'/api/work-methods/{other["method_id"]}?version={other["version"]}', expected=(404,), log=False)

    def live_collection(self):
        """명시적으로 요청한 경우에만 예산을 정한 실제 공개 수집을 실행한다."""
        if not self.live_github and self.catalog_cache:
            from scripts.live_catalog_evaluation import run_replay_collection
            run_replay_collection(self, self.catalog_cache)
            return
        if not self.live_github:
            self.record("실제 원본 수집", "실제 GitHub 수집은 명시적 옵션이 필요함", "skipped",
                {"external_requests": 0, "fixture_is_live_crawl": False})
            return
        from scripts.live_catalog_evaluation import run_live_collection
        run_live_collection(self, budget=36)


def main():
    """완료 후 선택적으로 보고서와 실제 API만 유지하고 자기 실행 자원만 정리한다."""
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--live-github", action="store_true")
    parser.add_argument("--catalog-cache", type=Path, help="기존 공개 원본 표본을 추가 외부 요청 없이 다시 평가")
    parser.add_argument("--source-reviews", type=Path, help="번들 해시와 원문 줄을 고정한 로컬 직접 검토 메모")
    parser.add_argument("--keep-running", action="store_true")
    args = parser.parse_args()
    simulation = RecommendationEvaluation(args.database_url, live_github=args.live_github,
        catalog_cache=args.catalog_cache, source_reviews=args.source_reviews)
    print("VIEWER=" + simulation.viewer_url, flush=True)
    print("SWAGGER=" + simulation.api + "/docs", flush=True)
    failed = False
    try:
        try:
            simulation.start()
            simulation.seed_corpus()
            simulation.prepare_people()
            simulation.evaluate_people()
            simulation.edit_and_feedback()
            simulation.check_privacy()
            simulation.live_collection()
            simulation.record("완료", "문맥별 추천 평가 완료", data={"viewer": simulation.viewer_url,
                "report": str(simulation.public / "index.html"), "checks": len(simulation.checks)})
        except Exception as error:
            failed = True
            simulation.record("평가 중단", "검증 오류 발생", "failed", {"type": type(error).__name__, "message": str(error)})
        if args.keep_running:
            print("평가 API와 보고서를 유지합니다. 종료하려면 Ctrl+C.", flush=True)
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        simulation.close()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
