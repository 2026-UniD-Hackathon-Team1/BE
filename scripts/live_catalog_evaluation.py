"""고정 평가와 분리해 실제 공개 검색과 수집 및 프로필별 추천을 관찰한다."""
import base64
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from sqlalchemy import func, select

from backend.models import Asset, AssetVersion, CatalogSyncJob, ChangeEvent, Source
from scripts.catalog_sample import export_sample, load_sample
from backend.catalog.eligibility import repository_fixture
from backend.catalog.dimensions import RULES
from backend.catalog.inspection import inspect_bundle


def load_source_reviews(path):
    """로컬 검토 메모를 제한된 JSON으로 읽고 실행 명령이나 외부 파일 참조는 처리하지 않는다."""
    if path is None:
        return {}
    source = Path(path)
    if source.stat().st_size > 1_000_000:
        raise ValueError("source review file too large")
    data = json.loads(source.read_text(encoding="utf-8"))
    rows = data.get("reviews") if isinstance(data, dict) else None
    if not isinstance(data, dict) or data.get("format_version") != 1 or not isinstance(rows, list) or len(rows) > 200:
        raise ValueError("invalid source review file")
    result = {}
    for row in rows:
        if not isinstance(row, dict) or not re.fullmatch(r"[a-f0-9]{64}", str(row.get("bundle_sha256", ""))):
            raise ValueError("source review requires bundle sha256")
        digest = row["bundle_sha256"]
        if digest in result:
            raise ValueError("duplicate source review bundle")
        claims = row.get("capabilities", [])
        if not isinstance(claims, list) or len(claims) > 30:
            raise ValueError("invalid source review capabilities")
        seen = set()
        for claim in claims:
            if (not isinstance(claim, dict) or claim.get("capability") not in RULES["capabilities"]
                    or claim.get("verdict") not in {"confirmed", "limited", "rejected", "pending"}
                    or claim["capability"] in seen):
                raise ValueError("invalid source review verdict")
            seen.add(claim["capability"])
            if claim["verdict"] != "pending" and (not isinstance(claim.get("note"), str) or not claim["note"].strip() or len(claim["note"]) > 1000):
                raise ValueError("source review requires a note")
        if any(claim["verdict"] != "pending" for claim in claims):
            reviewer, stamp = row.get("reviewer"), row.get("reviewed_at")
            if not isinstance(reviewer, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,60}", reviewer):
                raise ValueError("source review requires reviewer label")
            try:
                reviewed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                raise ValueError("source review requires timestamp") from None
            if reviewed.tzinfo is None:
                raise ValueError("source review timestamp requires timezone")
        for field in ("usage_summary", "note", "independence_group"):
            if field in row and (not isinstance(row[field], str) or len(row[field]) > 1000):
                raise ValueError("source review text too long")
        for field in ("limitations", "setup_notes"):
            values = row.get(field, [])
            if not isinstance(values, list) or len(values) > 12 or any(not isinstance(value, str) or len(value) > 600 for value in values):
                raise ValueError("invalid source review limitations")
        result[digest] = row
    return result


def source_line_url(source_url, path, line):
    """고정 GitHub 커밋의 상대 파일 경로와 줄 번호만 원문 링크로 연결한다."""
    if not isinstance(path, str) or not path or "\\" in path or any(part in {"", ".", ".."} for part in path.split("/")):
        return None
    try:
        parsed = urlsplit(source_url)
        parts = parsed.path.split("/")
        if (parsed.scheme != "https" or parsed.netloc != "github.com" or parsed.query or parsed.fragment
                or len(parts) < 5 or parts[3] != "tree" or not re.fullmatch(r"[a-f0-9]{40}", parts[4])):
            return None
    except (ValueError, TypeError, AttributeError):
        return None
    parts[3] = "blob"
    if parts[-1] == ".":
        parts.pop()
    target = "/".join(parts).rstrip("/") + "/" + quote(path, safe="/")
    return urlunsplit((parsed.scheme, parsed.netloc, target, "", "L" + str(line)))


def _checked_evidence(evidence, documents, source_url):
    """기록된 문구가 같은 번들의 실제 파일과 해당 줄에 존재하는지 대조한다."""
    if not isinstance(evidence, dict):
        return None
    path, line, excerpt = evidence.get("path"), evidence.get("line"), evidence.get("excerpt")
    content = documents.get(path)
    if not isinstance(content, bytes) or not isinstance(line, int) or isinstance(line, bool) or line < 1:
        return None
    try:
        lines = content.decode("utf-8-sig").splitlines()
    except UnicodeError:
        return None
    if not isinstance(excerpt, str) or not excerpt or len(excerpt) > 240 or line > len(lines) or excerpt not in lines[line - 1]:
        return None
    return {"path": path, "line": line, "excerpt": excerpt, "url": source_line_url(source_url, path, line),
        "file_sha256": hashlib.sha256(content).hexdigest()}


def audit_candidate_source(card, version, reviews):
    """자동 근거 대조와 해시가 일치하는 직접 검토 메모를 분리하고 미검토를 유지한다."""
    digest = version.bundle_sha256
    inspection = inspect_bundle(version.metadata_json, digest)
    verified = inspection["integrity_status"] == "verified"
    files = version.metadata_json.get("bundle", {}).get("files", {})
    documents = {path: base64.b64decode(content, validate=True) for path, content in files.items()} if verified else {}
    requested = card.get("assessment", {}).get("matches", {}).get("matched_requested_capabilities", [])
    evidence = []
    for item in card.get("assessment", {}).get("evidence", []):
        if item.get("dimension") == "capabilities" and item.get("value") in requested:
            checked = _checked_evidence(item, documents, card.get("source_url"))
            if checked:
                evidence.append({**checked, "capability": item["value"]})
    record = reviews.get(digest, {})
    decisions = []
    for claim in record.get("capabilities", []):
        checked = _checked_evidence(claim, documents, card.get("source_url"))
        if claim["capability"] in requested and claim["verdict"] != "pending" and checked:
            decisions.append({**checked, "capability": claim["capability"], "verdict": claim["verdict"], "note": claim["note"]})
    reviewed_caps = {decision["capability"] for decision in decisions}
    return {"bundle_sha256": digest, "integrity_verified": verified, "evidence": evidence,
        "review_status": "document_review_recorded" if decisions else "not_reviewed",
        "reviewed_at": record.get("reviewed_at") if decisions else None,
        "reviewer": record.get("reviewer") if decisions else None, "decisions": decisions,
        "unreviewed_capabilities": sorted(set(requested) - reviewed_caps),
        "usage_summary": record.get("usage_summary", "") if decisions else "",
        "limitations": record.get("limitations", []) if decisions else [],
        "setup_notes": record.get("setup_notes", []) if decisions else [],
        "independence_group": record.get("independence_group") if decisions else None,
        "verification_scope": "stored_document_only", "execution_verified": False, "setup_verified": False}


def _review_template(card):
    """아직 검토하지 않은 입력 양식을 만들고 자동 확인을 직접 검토로 표시하지 않는다."""
    audit = card["source_audit"]
    claims = {}
    for item in audit["evidence"]:
        claims.setdefault(item["capability"], {"capability": item["capability"], "verdict": "pending", "path": item["path"],
            "line": item["line"], "excerpt": item["excerpt"], "note": ""})
    return {"bundle_sha256": audit["bundle_sha256"], "title": card["title"], "reviewer": "",
        "reviewed_at": None, "usage_summary": "", "limitations": [], "setup_notes": [], "independence_group": "",
        "capabilities": list(claims.values())}


def evaluate_public_sample(simulation):
    """프로필별 두 대안과 필수 공백의 원문 커버리지를 확인하고 부족한 부분을 먼저 표시한다."""
    matrix, templates = [], {}
    reviews = load_source_reviews(getattr(simulation, "source_reviews", None))
    for person in simulation.people:
        simulation.user = person["headers"]
        result = simulation.call("공개 원본 재평가", person["name"] + " 원본 추천", "GET",
            "/api/recommendations?include_excluded=true", log=False)
        person["live_result"] = result
        with simulation.sessions() as session:
            for card in result["items"]:
                version = session.get(AssetVersion, card["candidate_version_id"])
                card["source_audit"] = audit_candidate_source(card, version, reviews)
                template = _review_template(card)
                if template["bundle_sha256"] not in templates:
                    templates[template["bundle_sha256"]] = template
                else:
                    known = {claim["capability"] for claim in templates[template["bundle_sha256"]]["capabilities"]}
                    templates[template["bundle_sha256"]]["capabilities"].extend(
                        claim for claim in template["capabilities"] if claim["capability"] not in known)
        requested = set(person["payload"].get("goals", []))
        for step in person["method_body"].get("workflow", []):
            if step["status"] == "gap":
                requested.update(step["capabilities"])
        covered = {capability for card in result["items"] for capability in
            card["assessment"]["matches"]["matched_requested_capabilities"]}
        positive = person.get("scenario_kind", "positive") == "positive"
        confirmed = {claim["capability"] for card in result["items"] for claim in card["source_audit"]["decisions"] if claim["verdict"] == "confirmed"}
        independent = {card["source_audit"]["independence_group"] for card in result["items"]
            if card["source_audit"]["independence_group"] and any(claim["verdict"] == "confirmed" for claim in card["source_audit"]["decisions"])}
        unreviewed = any(card["source_audit"]["unreviewed_capabilities"] for card in result["items"])
        row = {"profile": person["name"], "scenario_id": person.get("scenario_id"), "positive": positive,
            "recommendations": len(result["items"]), "requested": sorted(requested),
            "covered": sorted(covered & requested), "missing": sorted(requested - covered),
            "sufficient": len(result["items"]) >= 2 and requested <= covered,
            "manual_source_review_required": bool(result["items"]) and unreviewed,
            "source_review_status": "no_candidates" if not result["items"] else "pending" if unreviewed else "document_review_recorded",
            "reviewed_covered": sorted(confirmed & requested), "reviewed_missing": sorted(requested - confirmed),
            "reviewed_independent_alternatives": len(independent),
            "reviewed_sufficient": len(independent) >= 2 and requested <= confirmed and not unreviewed}
        person["live_coverage"] = row
        matrix.append(row)
    positives = [row for row in matrix if row["positive"]]
    simulation.live_summary.update(coverage=matrix, positive_profiles=len(positives),
        sufficiently_covered_profiles=sum(row["sufficient"] for row in positives),
        document_reviewed_sufficient_profiles=sum(row["reviewed_sufficient"] for row in positives),
        recommended_profiles=sum(bool(row["recommendations"]) for row in positives),
        abstained_profiles=sum(not row["recommendations"] for row in positives),
        sufficiency_rule="정상 작업별 최소 두 추천 대안과 등록한 필수 목표 및 검사 공백 전체의 원문 기능 근거")
    (simulation.public / "source-review-template.json").write_text(json.dumps(
        {"format_version": 1, "verification_scope": "stored_document_only", "reviews": list(templates.values())}, ensure_ascii=False, indent=2), encoding="utf-8")
    simulation.record("수집 품질 반복", "프로필별 추천 대안과 아직 부족한 검사", data=simulation.live_summary)
    simulation.check("정상 작업 12개 각각에 추천 대안과 필수 검사 근거를 충분히 수집함",
        len(positives) == 12 and all(row["sufficient"] for row in positives), matrix)


def run_replay_collection(simulation, path):
    """이미 수집한 실제 공개 원문으로 새 규칙을 재평가하며 원격 요청은 하지 않는다."""
    with simulation.sessions.begin() as session:
        session.get(Source, simulation.fixture_source_id).enabled = False
    restored = load_sample(simulation.sessions, path)
    with simulation.sessions() as session:
        rows = session.execute(select(Asset, AssetVersion).join(AssetVersion, AssetVersion.asset_id == Asset.id)
            .join(Source, Source.id == Asset.source_id).where(Source.enabled.is_(True))).all()
        allowed = [(asset, version) for asset, version in rows if not repository_fixture(asset, version)]
    simulation.live_summary = {"scope": "cached_public_sample", "request_count": 0,
        "candidate_count": len(allowed), "skill_count": sum(asset.kind == "skill" for asset, _ in allowed),
        "excluded_repository_fixtures": len(rows) - len(allowed),
        "sources": restored["sources"], "original_sample_exported_at": restored["exported_at"],
        "production_accuracy_claimed": False, "recommended_profiles": 0, "abstained_profiles": 0}
    simulation.record("원본 표본 재평가", "고정 SHA 원문 표본을 최신 분류기로 다시 평가", data=restored)
    evaluate_public_sample(simulation)


def finished(value):
    """수집 작업이 외부 호출을 마치거나 중단된 상태인지 확인한다."""
    return value["status"] not in {"queued", "running"}


def run_live_collection(simulation, *, budget=36):
    """한 공통 카탈로그를 최대 예산 안에서 수집하고 모든 가상 사용자에게 재사용한다."""
    if not 1 <= budget <= 50:
        raise ValueError("live request budget must be 1..50")
    people = [simulation.people[index] for index in (0, 2)]
    plans = []
    for person in people:
        simulation.user = person["headers"]
        plans.append(simulation.call("실제 발견 계획", person["name"] + " 공개 검색 계획", "GET",
            "/api/catalog/discovery-plan", log=False))
    simulation.record("실제 발견 계획", "사용자 맥락에서 만든 공개 검색과 폴더 우선순위", data={
        "plans": plans, "external_get_budget": budget, "shared_across_all_profiles": True,
        "existing_pc_credentials_used": False, "paid_model_calls": 0})
    # 고정 평가용 원문은 실수집 결과와 함께 추천하거나 실제 후보 수로 세지 않는다.
    with simulation.sessions.begin() as session:
        session.get(Source, simulation.fixture_source_id).enabled = False
    body = {"profile_ids": [person["profile_id"] for person in people], "max_requests": min(24, budget),
        "discovery_limit": 1, "batch_size": 3}
    initial = simulation.call("실제 공개 수집", "프로필에서 새 저장소를 발견하고 고정 원문 수집", "POST",
        "/api/catalog/discovery-sync", body=body, auth="operator", key="live-context-" + simulation.tag,
        expected=(202,), log=False)
    job = simulation.poll("/api/catalog/jobs/" + initial["id"], finished, auth="operator", seconds=150)
    jobs = [job]
    simulation.record("실제 공개 수집", "최초 수집 결과와 쿼리별 실행 상태", data=job)
    used = job["request_count"]
    # 동일 요청 키를 반복해도 GitHub 호출이 다시 발생하지 않는지 실제 API로 확인한다.
    repeated = simulation.call("실제 수집 멱등성", "같은 요청 키 재조회", "POST", "/api/catalog/discovery-sync",
        body=body, auth="operator", key="live-context-" + simulation.tag, expected=(202,), log=False)
    simulation.check("같은 수집 요청 키는 기존 결과를 재사용하고 원격 요청을 늘리지 않음",
        repeated["id"] == job["id"] and repeated["request_count"] == job["request_count"])
    remaining = budget - used
    if remaining >= 5 and any(item.get("resume_required") for item in job["result"]["items"]):
        initial = simulation.call("고정 회차 재개", "남은 예산으로 이미 발견한 원본 이어 읽기", "POST",
            "/api/catalog/sync", body={"resume_pending": True, "max_requests": remaining, "batch_size": 5},
            auth="operator", key="live-resume-" + simulation.tag, expected=(202,), log=False)
        resumed = simulation.poll("/api/catalog/jobs/" + initial["id"], finished, auth="operator", seconds=150)
        jobs.append(resumed)
        used += resumed["request_count"]
        simulation.record("고정 회차 재개", "고정 커밋의 후속 수집 결과", data=resumed)
    with simulation.sessions() as session:
        rows = session.execute(select(Asset, AssetVersion).join(AssetVersion, AssetVersion.asset_id == Asset.id)
            .join(Source, Source.id == Asset.source_id).where(Source.enabled.is_(True))).all()
        sources = list(session.scalars(select(Source).where(Source.enabled.is_(True))))
        corpus = [{"title": asset.display_name, "kind": asset.kind, "candidate_version_id": version.id,
            "revision": version.revision, "bundle_sha256": version.bundle_sha256,
            "task_types": version.tags.get("task_types", []), "dimensions": version.tags.get("dimensions", {}),
            "source_url": version.metadata_json.get("source_url")} for asset, version in rows]
        actual_used = session.scalar(select(func.sum(CatalogSyncJob.request_count))) or 0
        duplicate_events = session.execute(select(ChangeEvent.to_version_id, func.count()).group_by(
            ChangeEvent.to_version_id).having(func.count() > 1)).all()
        source_progress = [{"source_id": source.id, "last_error": source.last_error,
            "completed_units": source.sync_state.get("batch", {}).get("next"),
            "total_units": len(source.sync_state.get("batch", {}).get("folders", [])) + 1,
            "folder_hints": source.sync_state.get("batch", {}).get("folder_hints", [])} for source in sources]
    simulation.check("실제 총 GitHub 요청이 명시한 예산 이하임", used == actual_used and used <= budget,
        {"request_count": used, "budget": budget})
    simulation.check("고정 평가 후보가 실수집 후보 수에 섞이지 않음", all(
        not item["title"].startswith("[고정 평가]") for item in corpus))
    simulation.check("버전별 변화 이벤트가 중복 생성되지 않음", not duplicate_events)
    simulation.live_summary = {"request_count": used, "budget": budget, "candidate_count": len(corpus),
        "skill_count": sum(item["kind"] == "skill" for item in corpus), "sources": source_progress,
        "recommended_profiles": 0, "abstained_profiles": 0, "scope": "bounded_public_github_sample",
        "production_accuracy_claimed": False, "jobs": jobs}
    simulation.record("실제 원본", "수집한 원본과 태그 및 줄 번호 근거", data=corpus)
    export_sample(simulation.sessions, simulation.directory / "catalog-sample.json")
    for person in simulation.people:
        simulation.user = person["headers"]
        result = simulation.call("실제 원본 추천", person["name"] + " 실수집 후보 추천", "GET",
            "/api/recommendations?include_excluded=true", log=False)
        person["live_result"] = result
        simulation.live_summary["recommended_profiles" if result["items"] else "abstained_profiles"] += 1
        simulation.check(person["name"] + " 실제 원본만 정보 제공으로 추천", all(
            not item["demo"] and not item["title"].startswith("[고정 평가]") and
            item["support_level"] == "information_only" and item["performance_status"] == "not_compared"
            for item in result["items"]))
        simulation.record("실제 원본 추천", person["name"] + " 실제 추천과 제외 이유", data={
            "top3": result["items"][:3], "excluded_count": len(result["excluded"]),
            "abstained": not result["items"]})
    simulation.record("실수집 범위", "공개 원본 표본의 추천 범위와 보류", data=simulation.live_summary)
    simulation.check("공개 검색에서 실제 스킬 원문을 하나 이상 수집함", simulation.live_summary["skill_count"] > 0)
    simulation.check("실제 원문과 작업 공백이 연결되는 사용자 추천을 하나 이상 확인함",
        simulation.live_summary["recommended_profiles"] > 0)
