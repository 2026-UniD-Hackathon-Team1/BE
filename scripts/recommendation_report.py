"""추천 평가 기록을 읽기 쉬운 한국어 HTML과 로컬 Markdown 요약으로 만든다."""
import html
import json
import os
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit


LABELS = {"input_validation": "입력 검증", "exception_handling": "예외 처리",
    "regression_test": "회귀 테스트", "test_coverage": "테스트 커버리지", "documentation": "문서화",
    "api_security": "API 보안", "accessibility": "접근성", "performance": "성능 분석",
    "browser_testing": "브라우저 테스트", "test_design": "테스트 설계",
    "database_changes": "DB 변경", "typing": "타입 검사"}
FIXTURE_NAMES = {"py-validation": "FastAPI 입력 검증", "py-security": "FastAPI 보안 검토",
    "py-security-old": "다른 보안 검토 후보 (과거 원문)", "py-regression": "FastAPI 회귀 검사",
    "django-validation": "Django 입력 검증", "react-browser": "React 브라우저 테스트",
    "react-typing": "React 타입 검사", "py-performance": "FastAPI 성능 검토",
    "py-docs": "FastAPI 문서화", "general-docs": "일반 문서화",
    "claude-security": "Claude Code 보안 검토", "cursor-browser": "Cursor 브라우저 테스트",
    "react-accessibility": "React 접근성 리뷰", "go-validation": "Go 입력 검증",
    "rust-test-design": "Rust 테스트 설계", "java-api-security": "Java Spring API 보안",
    "java-database": "Java Spring DB 변경", "flask-database": "Flask DB 변경"}
SHORT_REASONS = {"등록한 작업 유형과 일치하지 않음": "작업 유형 불일치",
    "후보에 확인된 기능이 없어 개인화 추천을 보류함": "지원 기능의 원문 근거 부족",
    "등록한 목표나 작업 공백 및 필수 검사와 연결되는 기능이 없어 추천을 보류함": "현재 목표나 검사 공백과 연결되지 않음",
    "현재 프로젝트 언어와 후보의 명시된 대상이 다름": "프로젝트 언어 불일치",
    "현재 프로젝트 프레임워크와 후보의 명시된 대상이 다름": "프레임워크 불일치",
    "현재 AI 도구와 후보의 명시된 대상이 다름": "사용하는 AI 도구 불일치",
    "후보 기능을 현재 작업 단계에서 모두 수행하고 있어 새로운 보완 근거가 없음": "이미 수행 중인 검사와 중복",
    "동일한 전체 번들 버전이 이미 설치됨": "동일 번들 설치됨"}


def _text(value):
    """표시 문장의 공백과 구분 기호만 정리하고 문서를 실행하지 않는다."""
    return " ".join(str(value).replace("\u00b7", ", ").split())


def _markdown(value):
    """외부 원문이 표나 링크 구문으로 해석되지 않게 표시용 문자를 이스케이프한다."""
    text = html.escape(_text(value), quote=False)
    for character in ("\\", "`", "*", "_", "[", "]", "|"):
        text = text.replace(character, "\\" + character)
    return text


def _live_data(report):
    """새 요약 필드를 우선하고 이전 실행의 최종 수집 이벤트도 읽는다."""
    live = report.get("summary", {}).get("live_collection")
    if isinstance(live, dict) and live:
        return live
    for event in reversed(report.get("events", [])):
        data = event.get("data")
        if isinstance(data, dict) and "candidate_count" in data and "request_count" in data:
            return data
    return {}


def _goal(person):
    """등록한 목표와 검사 공백을 기술 이름보다 먼저 간단히 표시한다."""
    payload = person.get("payload", {})
    goals = payload.get("goals", [])
    if goals:
        return ", ".join(LABELS.get(goal, goal) for goal in goals)
    steps = person.get("method_body", {}).get("workflow", [])
    return "등록한 검사 모두 수행 중" if steps else "목표와 환경 정보 부족"


def _reasons(item):
    """일반적인 작업 일치 문구보다 실제 검사 공백을 보완하는 근거를 먼저 보여준다."""
    assessment = item.get("assessment", {})
    reasons = []
    for step in assessment.get("workflow_fit", []):
        if step.get("status") == "gap":
            capabilities = ", ".join(LABELS.get(value, value) for value in step.get("matched_capabilities", []))
            reasons.append(f"{step.get('label', '검사 공백')} 보완: {capabilities}")
    if not reasons:
        matched = assessment.get("matches", {}).get("matched_requested_capabilities", [])
        if matched:
            reasons.append("목표 지원: " + ", ".join(LABELS.get(value, value) for value in matched))
    reasons.extend(reason for reason in item.get("related_reasons", []) if not reason.startswith("현재 "))
    return list(dict.fromkeys(_text(reason) for reason in reasons))[:2] or ["원문과 등록한 작업 문맥의 관련성 확인"]


def _held_reason(result):
    """추천을 보류한 공통 원인을 긴 후보 목록 대신 두 문장 이내로 요약한다."""
    reasons = Counter(reason for item in result.get("excluded", [])
        for reason in item.get("excluded_reasons", item.get("reasons", [])))
    return " / ".join(SHORT_REASONS.get(reason, reason) for reason, _ in reasons.most_common(2)) or "추천할 충분한 근거 없음"


def _candidate(item):
    """고정 평가 이름과 실제 저장소 경로를 구분하고 선택 이유를 짧게 모은다."""
    title = item.get("title", "후보")
    if title.startswith("[고정 평가] "):
        title = FIXTURE_NAMES.get(title[8:].strip(), title[8:].strip())
    elif " / " in title:
        repository, folder = title.split(" / ", 1)
        title = repository + " / " + folder.rsplit("/", 1)[-1]
    if item.get("variant_count", 1) > 1:
        title += f" (배포 경로 {item['variant_count']}개를 한 대안으로 묶음)"
    reasons = _reasons(item)
    uncertainty = item.get("assessment", {}).get("uncertainties", [])
    setup = [value.get("label", "") for value in item.get("requirements", {}).get("items", [])
             if value.get("kind") in {"external_api", "host_model", "cli"}]
    audit = item.get("source_audit", {})
    setup = list(dict.fromkeys(setup + audit.get("setup_notes", [])))
    decisions = audit.get("decisions", [])
    verdict = "원문 검토 대기"
    if decisions:
        verdict = "원문 검토: " + ", ".join(LABELS.get(claim["capability"], claim["capability"]) + " "
            + {"confirmed": "선언 확인", "limited": "조건 제한", "rejected": "근거 거절"}[claim["verdict"]] for claim in decisions)
    proof = decisions or audit.get("evidence", [])
    return {"name": _text(title), "reasons": reasons,
        "uncertainty": _text(uncertainty[0]) if uncertainty else "",
        "setup": setup[:3], "usage": audit.get("usage_summary") or "구체적인 사용 방법은 원문 검토 전입니다.",
        "source_proof": proof[:2], "review_verdict": verdict,
        "review_notes": [claim["note"] for claim in decisions][:2], "limitations": audit.get("limitations", [])[:2]}


def _rows(people, key, *, pending_label="진행 중"):
    """진행 중과 보류 및 실제 추천을 구분한 표의 공통 행을 만든다."""
    rows = []
    for person in people:
        result = person.get(key)
        pending = not isinstance(result, dict)
        result = result if not pending else {}
        if key == "live_result":
            result = {**result, "excluded": [item for item in result.get("excluded", [])
                if not item.get("title", "").startswith("[고정 평가]")]}
        items = result.get("items", [])
        status = pending_label if pending else f"{len(items)}개 추천" if items else "추천 보류"
        coverage = person.get("live_coverage") if key == "live_result" else None
        if isinstance(coverage, dict) and coverage.get("positive"):
            missing = ", ".join(LABELS.get(value, value) for value in coverage.get("missing", []))
            status = ("자동 수집 기준 충족" if coverage.get("sufficient") else "자동 수집 보강 필요") + f" / 대안 {len(items)}개"
            status += " / 부족: " + missing if missing else " / 필수 검사 충족"
            if coverage.get("reviewed_sufficient"):
                status += " / 원문 검토 기준 충족"
            elif coverage.get("manual_source_review_required"):
                status += " / 원문 직접 검토 대기"
            elif "reviewed_missing" in coverage and items:
                reviewed_missing = ", ".join(LABELS.get(value, value) for value in coverage["reviewed_missing"])
                status += (" / 검토 후 미확인: " + reviewed_missing) if reviewed_missing else f" / 검토한 독립 접근 {coverage.get('reviewed_independent_alternatives', 0)}개"
        if key == "result" and "metrics" in person:
            metrics = person["metrics"]
            correct = not metrics.get("negative_leakage_count") and not metrics.get("missed_relevant_candidates")
            status = ("정답 일치" if correct else "정답과 다름") + f" / 기대 {metrics.get('relevant_total', 0)}개, 실제 {len(items)}개"
        rows.append({"name": _text(person.get("name", "작업")), "goal": _goal(person), "status": status,
            "candidates": [_candidate(item) for item in items[:3]],
            "held_reason": ("실제 공개 수집을 실행하지 않았습니다." if pending_label == "미실행"
                else "평가 결과를 기다리는 중") if pending else _held_reason(result) if not items else ""})
    return rows


def _percent(value):
    """정밀도나 재현율이 아직 없으면 완료된 수치처럼 표시하지 않는다."""
    return f"{value:.0%}" if isinstance(value, (int, float)) else "집계 전"


def _write_output(path, content):
    """Windows에서 보고서를 잠시 읽는 동안에는 기존 파일을 보존하고 교체를 재시도한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    for attempt in range(7):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 6:
                raise
            time.sleep(min(0.02 * 2 ** attempt, 0.5))


def _proof_link(proof, *, markdown=False):
    """미리 검증한 고정 원본 링크만 표시하고 보고서 문자열을 실행 가능한 링크로 만들지 않는다."""
    label = f"{proof.get('path', '원문')}:{proof.get('line', '?')} ({LABELS.get(proof.get('capability'), proof.get('capability', '근거'))})"
    url = proof.get("url")
    try:
        parsed = urlsplit(url) if isinstance(url, str) else None
        safe = parsed and parsed.scheme == "https" and parsed.netloc == "github.com" and not parsed.query
    except ValueError:
        safe = False
    if markdown:
        return f"[{_markdown(label)}](<{url}>)" if safe else _markdown(label)
    escaped = html.escape(_text(label))
    return '<a href="' + html.escape(url, quote=True) + '" target="_blank" rel="noopener">' + escaped + "</a>" if safe else escaped


def render_recommendation_report(report, html_path, markdown_path=None):
    """기존 평가 JSON 계약을 유지하면서 지정한 경로에 HTML과 Markdown 요약만 쓴다."""
    html_path = Path(html_path)
    markdown_path = Path(markdown_path) if markdown_path else html_path.parent / "report.md"
    summary, people = report.get("summary", {}), report.get("people", [])
    metrics, live = summary.get("quality_metrics", {}), _live_data(report)
    positive = [person for person in people if person.get("scenario_kind") != "negative"]
    negative = [person for person in people if person.get("scenario_kind") == "negative"]
    cohort_label = "정상 작업" if any(person.get("scenario_kind") for person in people) else "가상 작업"
    pending_label = "진행 중" if summary.get("live_requested") or live else "미실행"
    live_rows, synthetic_rows = _rows(positive, "live_result", pending_label=pending_label), _rows(positive, "result")
    negative_live_rows = _rows(negative, "live_result", pending_label=pending_label)
    negative_synthetic_rows = _rows(negative, "result")
    finished = sum(isinstance(person.get("live_result"), dict) for person in positive)
    recommended = sum(bool(person.get("live_result", {}).get("items")) for person in positive)
    held = finished - recommended
    coverage = [row for row in live.get("coverage", []) if row.get("positive")]
    sufficient = sum(row.get("sufficient", False) for row in coverage)
    replay = live.get("scope") == "cached_public_sample"
    count = str(live["candidate_count"]) + "개" if "candidate_count" in live else "진행 중" if summary.get("live_requested") else "미실행"
    headline = (f"{'기존 공개 원본 표본' if replay else '실제 공개 원본'} {count}를 {'다시 평가했고' if replay else '수집했고'} {cohort_label} {finished}개 중 {recommended}개에 추천했습니다."
        if live and finished else "실제 공개 원본의 수집과 추천 결과를 확인하는 중입니다." if summary.get("live_requested")
        else "합성 평가를 완료했습니다. 실제 공개 원본의 추천 결과는 아직 없습니다."
        if metrics.get("evaluated_profiles", 0) and metrics.get("evaluated_profiles") == len(people)
        else "합성 시나리오의 추천 규칙을 확인하는 중입니다.")
    limits = ["현재 지원: 공개 원문 수집, 기능 분류, 등록한 기술과 검사 공백에 맞춘 추천 및 이유 제공.",
        "후보는 정보 제공 단계입니다. 실행 호환성과 실제 성능 개선은 아직 검증하지 않았습니다."]
    if finished and recommended == 0:
        limits.insert(0, "이번 실제 표본에서는 유의미한 추천을 확인하지 못했습니다. 합성 평가 통과와 별도로 개선이 필요한 결과입니다.")
    if coverage:
        limits.insert(0, f"정상 작업 {len(positive)}개 중 추천 대안 2개 이상과 필수 검사 전체를 확보한 작업은 {sufficient}개입니다. 나머지는 수집 보강이 필요합니다.")
    if "document_reviewed_sufficient_profiles" in live:
        limits.insert(0, f"원문 직접 검토까지 마친 독립 대안과 필수 기능이 충분한 작업은 {live['document_reviewed_sufficient_profiles']} / {len(positive)}개입니다. 자동 기준 충족 수와 구분합니다.")
        limits.append("직접 검토는 저장한 원문의 해시와 줄을 대조한 문서 검토입니다. 설치, API 제공자 설정, 실제 실행과 비용 결제는 수행하지 않았습니다.")
    if replay:
        limits.append(f"기존에 수집한 공개 원본 표본을 다시 평가했습니다. 추가 GitHub 요청은 {live.get('request_count', 0)}회이며 새 크롤링 결과와 구분합니다.")
    elif live:
        limits.append(f"실제 수집은 공개 GitHub 표본에 한정했고 요청 {live.get('request_count', 0)}회 / 예산 {live.get('budget', '?')}회만 사용했습니다. 인터넷 전체를 수집한 결과가 아닙니다.")
    if live:
        if any(job.get("status") == "partial" for job in live.get("jobs", [])):
            limits.append("요청 예산 안에서 일부 폴더만 읽었습니다. 남은 원문 수집과 분류 근거 점검이 필요합니다.")
    failures = [item["title"] for item in report.get("checks", []) if item.get("passed") is False]
    limits.extend("미통과 검증: " + title for title in failures[:2])
    escape = lambda value: html.escape(_text(value))

    def html_table(rows):
        """원문 JSON 대신 이름과 목표 및 이유만 읽을 수 있는 표를 구성한다."""
        body = []
        for row in rows:
            candidates = "".join('<div class="candidate"><b>' + escape(item["name"]) + "</b><br>"
                + "<br>".join(escape(reason) for reason in item["reasons"])
                + '<small>활용: ' + escape(item["usage"]) + '</small>'
                + ('<small>설치와 비용: ' + escape(" / ".join(item["setup"])) + '</small>' if item["setup"] else '<small>설치와 비용: 원문에서 확인되지 않았거나 아직 점검 전입니다.</small>')
                + '<small>' + escape(item["review_verdict"]) + '</small>'
                + ''.join('<small>근거: ' + _proof_link(proof) + '<br><q>' + escape(proof.get("excerpt", "")[:180]) + '</q></small>' for proof in item["source_proof"])
                + ''.join('<small>검토 메모: ' + escape(note) + '</small>' for note in item["review_notes"])
                + ('<small>한계: ' + escape(" / ".join(item["limitations"])) + '</small>' if item["limitations"] else '')
                + ('<small>확인 필요: ' + escape(item["uncertainty"]) + "</small>" if item["uncertainty"] else "")
                + "</div>" for item in row["candidates"]) or escape(row["held_reason"])
            body.append(f'<tr><td><b>{escape(row["name"])}</b><small>목표: {escape(row["goal"])}</small></td>'
                f'<td>{candidates}</td><td>{escape(row["status"])}</td></tr>')
        return '<div class="table"><table><thead><tr><th>사용자 작업</th><th>후보와 선택 이유</th><th>결과</th></tr></thead><tbody>' + "".join(body) + "</tbody></table></div>"

    api = summary.get("api", "")
    parsed_api = urlsplit(api) if isinstance(api, str) else None
    swagger = '<a href="' + html.escape(api.rstrip("/") + "/docs", quote=True) + '">로컬 Swagger</a> / ' if parsed_api and parsed_api.scheme == "http" and parsed_api.hostname in {"localhost", "127.0.0.1", "::1"} and not parsed_api.username else ""
    raw = json.dumps(report, ensure_ascii=False, indent=2).replace("\u00b7", "\\u00b7")
    progress = f"자동 검증 {summary.get('checks_passed', 0)} / {summary.get('checks_total', 0)}개 통과"
    if summary.get("classifier_version"):
        progress += " / 분류 기준 " + summary["classifier_version"]
    positive_metrics = metrics.get("positive_cases", metrics)
    synthetic_note = (f"합성 평가 대상은 직접 작성한 후보 {summary.get('fixture_candidates', 0)}개와 {cohort_label} {len(positive)}개, 별도 경계 사례 {len(negative)}개입니다. "
        f"{cohort_label} {positive_metrics.get('evaluated', metrics.get('evaluated_profiles', 0))}개 집계 기준 "
        f"상위 정밀도 {_percent(positive_metrics.get('top3_precision'))}, 관련 후보 재현율 {_percent(positive_metrics.get('relevant_recall'))}, "
        f"부적절한 후보 {positive_metrics.get('negative_leakage_count', '집계 전')}개입니다.")
    page = f'''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>추천 기능 검증 결과</title><style>body{{margin:0;background:#f4f6f8;color:#172435;font:16px/1.75 system-ui}}main{{max-width:1040px;margin:auto;padding:38px 24px}}h1{{font-size:30px;line-height:1.35}}h2{{font-size:23px;margin-top:38px}}p{{margin:10px 0}}a{{color:#1454a4}}.panel,details{{background:white;border:1px solid #d7dfe7;border-radius:10px;padding:20px;margin:20px 0}}.counts{{display:flex;gap:25px;flex-wrap:wrap}}.counts b{{display:block;font-size:23px}}.muted,small{{color:#47596d}}small{{display:block;font-size:14px}}table{{width:100%;border-collapse:collapse;background:white}}th,td{{text-align:left;vertical-align:top;padding:15px;border:1px solid #d7dfe7;overflow-wrap:anywhere}}th{{background:#e9eff5}}td:first-child{{width:25%}}td:last-child{{width:18%}}.candidate+.candidate{{border-top:1px solid #d7dfe7;margin-top:13px;padding-top:13px}}.table{{overflow-x:auto}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.6 monospace}}summary{{cursor:pointer}}li{{margin:8px 0}}@media(max-width:600px){{main{{padding:22px 12px}}th,td{{padding:9px}}}}</style>
<main><h1>추천 기능 검증 결과</h1><p>{escape(headline)}</p><p class="muted">{escape(progress)}</p>
<div class="panel counts"><div>{'재평가한 공개 원본 표본' if replay else '실제 수집 원문'}<b>{escape(count)}</b></div><div>{escape(cohort_label)} 추천<b>{recommended} / {len(positive)}개</b></div><div>{escape(cohort_label)} 보류<b>{held}개</b></div><div>필수 검사와 대안 확보<b>{str(sufficient) + ' / ' + str(len(positive)) + '개' if coverage else '집계 전'}</b></div></div>
<p class="muted">{escape(cohort_label)} {finished} / {len(positive)}개 평가 완료. 보류와 설치 조건을 확인하는 경계 사례 {len(negative)}개는 별도로 표시합니다.{' 기존 원본 표본 재평가이며 추가 GitHub 요청 0회입니다.' if replay else ''}</p>
<h2>현재 범위와 부족한 부분</h2><ul>{''.join('<li>' + escape(line) + '</li>' for line in limits)}</ul>
<h2>실제 공개 원본으로 추천한 결과</h2><p>이 표는 수집한 GitHub 원문만 사용합니다. 후보가 없으면 보류하며, 진행 중인 작업은 별도로 표시합니다.</p>{html_table(live_rows)}
{'<h3>보류와 설치 조건의 경계 사례</h3>' + html_table(negative_live_rows) if negative else ''}
<h2>합성 시나리오 평가 결과</h2><p>{escape(synthetic_note)}</p><p class="muted">수작업으로 정의한 정답 집합에 대한 결과입니다. 운영 사용자 정확도나 성능 개선율을 뜻하지 않습니다. 상위 정밀도의 분모는 실제 반환한 최대 3개입니다.</p>{html_table(synthetic_rows)}
{'<h3>별도 경계 사례의 합성 평가</h3>' + html_table(negative_synthetic_rows) if negative else ''}
<p>{swagger}<a href="report.md">Markdown 요약</a> / <a href="report.json">기록 JSON</a></p>
<details><summary>전체 요청과 응답 기록 보기</summary><pre>{html.escape(raw)}</pre></details></main></html>'''
    markdown = ["# 추천 기능 검증 결과", "", headline, "", progress, "", "## 현재 범위와 부족한 부분", ""]
    markdown.extend("- " + _markdown(line) for line in limits)
    sections = [("실제 공개 원본 결과", live_rows)]
    if negative:
        sections.append(("실제 원본의 별도 경계 사례", negative_live_rows))
    sections.append(("합성 시나리오 결과", synthetic_rows))
    if negative:
        sections.append(("합성 평가의 별도 경계 사례", negative_synthetic_rows))
    for title, rows in sections:
        markdown.extend(["", "## " + title, ""])
        if title.startswith("합성"):
            markdown.extend([synthetic_note, "", "고정 정답에 대한 평가이며 운영 정확도나 성능 개선율을 뜻하지 않습니다.", ""])
        markdown.extend(["| 사용자 작업과 목표 | 추천 후보와 이유 | 결과 |", "| --- | --- | --- |"])
        for row in rows:
            candidates = "<br><br>".join(_markdown(item["name"]) + "<br>" + "<br>".join(_markdown(reason) for reason in item["reasons"])
                + "<br>활용: " + _markdown(item["usage"])
                + "<br>설치와 비용: " + (_markdown(" / ".join(item["setup"])) if item["setup"] else "확인 전")
                + "<br>" + _markdown(item["review_verdict"])
                + "".join("<br>근거: " + _proof_link(proof, markdown=True) for proof in item["source_proof"])
                + "".join("<br>검토 메모: " + _markdown(note) for note in item["review_notes"])
                + ("<br>한계: " + _markdown(" / ".join(item["limitations"])) if item["limitations"] else "")
                + ("<br>확인 필요: " + _markdown(item["uncertainty"]) if item["uncertainty"] else "") for item in row["candidates"])
            markdown.append("| " + _markdown(row["name"]) + "<br>목표: " + _markdown(row["goal"]) + " | "
                + (candidates or _markdown(row["held_reason"])) + " | " + _markdown(row["status"]) + " |")
    raw_path = os.path.relpath(html_path.parent / "report.json", markdown_path.parent).replace("\\", "/")
    markdown.extend(["", "## 상세 기록", "", f"[전체 요청과 응답 JSON](<{raw_path}>)", ""])
    outputs = [(html_path, page), (markdown_path, "\n".join(markdown))]
    if markdown_path.resolve() != (html_path.parent / "report.md").resolve():
        outputs.append((html_path.parent / "report.md", "\n".join(markdown).replace(f"(<{raw_path}>)", "(<report.json>)")))
    for path, content in outputs:
        _write_output(path, content)
    return {"html_path": str(html_path), "markdown_path": str(markdown_path)}
