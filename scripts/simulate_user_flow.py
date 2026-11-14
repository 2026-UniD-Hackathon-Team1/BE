"""별도 로컬 DB와 실제 서버 및 agent 프로세스로 사용자 흐름을 시연한다."""
import argparse
import base64
import functools
import hashlib
import html
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema

from backend.db import make_engine, session_factory
from backend.models import Asset, AssetVersion, CandidateAdapter, Comparison, Run, Source


def redact(value):
    """공개 시연 기록에서 인증 값과 비밀번호 및 DB 연결 문자열을 제거한다."""
    if isinstance(value, dict):
        return {key: "[숨김]" if any(word in key.lower() for word in (
            "token", "password", "authorization", "database_url")) or key == "code" and
            isinstance(item, str) and len(item) == 9 and item[4] == "-" else redact(item)
            for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str) and len(value) > 5000:
        return value[:5000] + "…"
    return value


def free_port():
    """다른 프로그램을 종료하지 않고 비어 있는 loopback 포트를 선택한다."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        """보고서 조회 로그에 불필요한 개인 요청 정보를 남기지 않는다."""


class Simulation:
    def __init__(self, database_url, *, live_github=False):
        """원격 운영 DB를 거부하고 시연 산출물과 인증 값을 별도로 준비한다."""
        parsed = make_url(database_url)
        if parsed.host not in {"localhost", "127.0.0.1", "::1"} or parsed.database != "be_simulation":
            raise ValueError("localhost의 be_simulation DB만 사용할 수 있습니다.")
        self.tag = uuid4().hex[:10]
        self.directory = ROOT / "runtime" / "simulations" / self.tag
        self.public = self.directory / "public"
        self.private = self.directory / "private"
        self.public.mkdir(parents=True)
        self.private.mkdir()
        self.schema = "simulation_" + self.tag
        self.admin = make_engine(database_url)
        self.url = parsed.update_query_dict({"options": "-csearch_path=" + self.schema}).render_as_string(hide_password=False)
        self.engine = make_engine(self.url)
        self.sessions = session_factory(self.engine)
        self.created_schema = False
        self.events, self.children, self.logs = [], [], []
        self.started = time.monotonic()
        self.live_github = live_github
        self.api = "http://127.0.0.1:" + str(free_port())
        self.username = "demo_" + self.tag
        self.password = "Local-simulation-2026!"
        self.operator_token = secrets.token_urlsafe(32)
        self.user = {}
        self.operator = {"X-Local-Token": self.operator_token}
        self.env = {**os.environ, "DATABASE_URL": self.url, "APP_LOCAL_TOKEN": self.operator_token,
            "APP_ENV": "local", "PYTHONIOENCODING": "utf-8",
            "PYTHONPATH": str(ROOT) + os.pathsep + str(ROOT / "agent" / "src")}
        self.http = httpx.Client(base_url=self.api, timeout=45, trust_env=False)
        handler = functools.partial(QuietHandler, directory=str(self.public))
        self.viewer = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.viewer_url = f"http://127.0.0.1:{self.viewer.server_port}/"
        threading.Thread(target=self.viewer.serve_forever, daemon=True).start()
        self.record("준비", "시연을 준비하고 있습니다", "running", {"transport": "실제 loopback HTTP", "paid_model_calls": 0})

    def record(self, phase, title, status="passed", data=None):
        """실제 실행 결과를 인증 값 없이 JSON과 브라우저 보고서에 갱신한다."""
        self.events.append({"phase": phase, "title": title, "status": status,
            "seconds": round(time.monotonic() - self.started, 2), "data": redact(data)})
        self.render()
        print(f"[{status.upper()}] {title}", flush=True)

    def render(self):
        """원문을 HTML로 실행하지 않고 접을 수 있는 관찰 보고서를 만든다."""
        labels = {"passed": "확인", "failed": "실패", "blocked": "미검증", "fixture": "고정 전제", "running": "진행 중", "skipped": "생략"}
        cards = []
        for index, event in enumerate(self.events, 1):
            payload = html.escape(json.dumps(event["data"], ensure_ascii=False, indent=2))
            cards.append(f'<details class="{event["status"]}"><summary><span>{index:02}</span><b>{html.escape(event["title"])}</b>'
                f'<em>{labels[event["status"]]} / {event["seconds"]}초</em></summary><small>{html.escape(event["phase"])}</small><pre>{payload}</pre></details>')
        counts = {status: sum(event["status"] == status for event in self.events) for status in labels}
        page = f'''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="8"><title>갈아탈까 실제 프로세스 시연</title>
<style>body{{background:#0e1726;color:#e4ecf9;font:16px/1.65 system-ui;margin:0}}main{{max-width:1020px;margin:44px auto;padding:0 24px}}
h1{{font-size:34px;margin-bottom:8px}}a{{color:#91c8ff}}.intro,.stats{{background:#18263b;border:1px solid #334863;border-radius:16px;padding:20px;margin:20px 0}}
.stats{{display:flex;gap:28px;flex-wrap:wrap}}details{{background:#152238;border-left:4px solid #42bc91;border-radius:9px;margin:12px 0;padding:16px}}summary{{display:flex;gap:16px;cursor:pointer;align-items:baseline}}summary b{{flex:1}}span,small{{color:#9fb4d2}}em{{font-style:normal;font-size:13px}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.6 ui-monospace}}.failed{{border-color:#ff7373}}.blocked,.fixture{{border-color:#e8b757}}.running,.skipped{{border-color:#6a8fc7}}code{{background:#0c1422;padding:3px 7px;border-radius:5px}}footer{{margin:40px 0;color:#9fb4d2}}</style>
<main><h1>실제 서버와 Agent 연결 시연</h1><p>FE 없이 실제 HTTP 요청과 PC 파일 변경을 확인합니다. 각 줄을 눌러 응답을 펼쳐 보세요.</p>
<div class="intro"><b>실행 환경</b><br>별도 로컬 PostgreSQL / 실제 Uvicorn 1개 / 실제 Agent 2개 / 같은 PC의 임시 프로젝트 A/B<br>
운영 DB와 사용자 프로젝트는 사용하지 않습니다. 실제 Codex와 유료 모델 호출은 없습니다.<br>
적용·복원은 <b>비교 결과를 고정한 별도 시나리오</b>이며 실제 비교 성능을 검증한 결과가 아닙니다.<br>
<a href="{self.api}/docs" target="_blank" rel="noopener">로컬 Swagger 열기</a> / 시연 계정 <code>{self.username}</code> / <code>{self.password}</code><br>
Swagger의 로그인 응답 token을 Authorize → LoginSession에 넣어 직접 조회할 수 있습니다. 이 비밀번호는 임시 시연 계정 전용입니다.</div>
<div class="stats"><b>확인 {counts['passed']}</b><b>실패 {counts['failed']}</b><b>미검증 {counts['blocked']}</b><b>고정 전제 {counts['fixture']}</b></div>
{''.join(cards)}<footer>실행 ID {self.tag} / 8초마다 갱신 / 토큰과 비밀번호는 요청 기록에서 숨깁니다. 시연 계정 로그인 정보만 위에 표시합니다.</footer></main></html>'''
        temporary = self.public / "index.tmp"
        temporary.write_text(page, encoding="utf-8")
        temporary.replace(self.public / "index.html")
        (self.public / "report.json").write_text(json.dumps(self.events, ensure_ascii=False, indent=2), encoding="utf-8")

    def call(self, phase, title, method, path, *, body=None, auth="user", expected=(200,), log=True, key=None):
        """실제 HTTP 응답을 기록하고 기대하지 않은 상태 코드를 즉시 드러낸다."""
        headers = self.user if auth == "user" else self.operator if auth == "operator" else {}
        if key:
            headers = {**headers, "Idempotency-Key": key}
        response = self.http.request(method, path, json=body, headers=headers)
        data = response.json() if response.content else None
        if log or response.status_code not in expected:
            self.record(phase, title, "passed" if response.status_code in expected else "failed",
                {"method": method, "path": path, "status": response.status_code, "request": body, "response": data})
        if response.status_code not in expected:
            raise RuntimeError(title + f": HTTP {response.status_code}")
        return data

    def require(self, phase, title, condition, data=None):
        """파일과 응답의 실제 조건을 확인하고 실패를 성공으로 덮어쓰지 않는다."""
        self.record(phase, title, "passed" if condition else "failed", data)
        if not condition:
            raise RuntimeError(title)

    def spawn(self, name, args):
        """사용자에게 창을 띄우지 않고 직접 만든 하위 프로세스만 실행한다."""
        output = (self.private / (name + ".log")).open("wb")
        self.logs.append(output)
        child = subprocess.Popen(args, cwd=ROOT, env=self.env, stdout=output, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.children.append(child)
        return child

    def start(self):
        """시연 전용 스키마를 만들고 별도 포트의 실제 API를 기동한다."""
        with self.admin.begin() as connection:
            connection.execute(CreateSchema(self.schema))
        self.created_schema = True
        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(ROOT / "migrations"))
        config.attributes["database_url"] = self.url
        command.upgrade(config, "head")
        port = self.api.rsplit(":", 1)[1]
        child = self.spawn("server", [sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1",
            "--port", port, "--no-access-log", "--log-level", "warning"])
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise RuntimeError("로컬 API 시작 실패: private/server.log 확인")
            try:
                if self.http.get("/api/health", timeout=1).status_code == 200:
                    break
            except httpx.RequestError:
                pass
            time.sleep(0.3)
        self.call("실제 HTTP", "로컬 API와 PostgreSQL 준비", "GET", "/api/health", auth="none")

    def accounts(self):
        """실제 가입과 로그인 및 계정 격리, 프로필과 작업 방식을 확인한다."""
        credentials = {"username": self.username, "password": self.password}
        signup = self.call("계정", "시연 계정 가입", "POST", "/api/auth/signup", body=credentials, auth="none", expected=(201,))
        self.signup_token = signup["token"]
        login = self.call("계정", "세션 로그인", "POST", "/api/auth/login", body=credentials, auth="none")
        self.user = {"Authorization": "Bearer " + login["token"]}
        self.user_id = login["user_id"]
        self.call("계정", "로그인한 사용자 확인", "GET", "/api/auth/me")
        self.call("계정", "잘못된 비밀번호 거부", "POST", "/api/auth/login", auth="none",
            body={"username": self.username, "password": "wrong-password-only"}, expected=(401,))
        self.call("권한", "일반 계정의 수집 운영 설정 접근 차단", "PUT", "/api/catalog/refresh/schedule",
            body={"enabled": True}, expected=(403,))
        self.call("프로필", "개발 환경과 작업 등록", "PUT", "/api/profile", body={"role": "backend developer",
            "primary_language": "python", "task_types": ["code_review", "test_generation"], "goals": ["input_validation", "test_coverage"]})
        self.method = self.call("작업 방식", "기존 코드 리뷰 방식 저장", "POST", "/api/work-methods",
            body={"task_type": "code_review", "prompt_text": "변경 함수의 입력 검증과 예외 처리를 확인한다."}, expected=(201,))
        self.call("작업 방식", "고정 버전 다시 조회", "GET",
            f'/api/work-methods/{self.method["method_id"]}?version={self.method["version"]}')
        other = self.call("격리", "별도 계정 준비", "POST", "/api/auth/signup", auth="none",
            body={"username": "other_" + self.tag, "password": self.password}, expected=(201,))
        own = self.user
        self.user = {"Authorization": "Bearer " + other["token"]}
        self.call("격리", "다른 계정에서 프로필을 볼 수 없음", "GET", "/api/profile", expected=(404,))
        self.call("격리", "다른 계정에서 작업 방식 조회 차단", "GET",
            f'/api/work-methods/{self.method["method_id"]}?version={self.method["version"]}', expected=(404,))
        self.user = own

    def connect_device(self, name):
        """실제 CLI connect로 임시 프로젝트를 등록하고 run 프로세스를 시작한다."""
        root = self.private / ("project-" + name)
        (root / ".codex").mkdir(parents=True)
        (root / "requirements.txt").write_text("fastapi==0.1\n", encoding="utf-8")
        (root / ".codex" / "config.toml").write_text(
            'model="simulation-model-not-executed"\napi_key="SIMULATION_FAKE_SECRET"\n', encoding="utf-8")
        (root / "app.py").write_text(f'def example():\n    return "project-{name}"\n', encoding="utf-8")
        target = root / ".agents" / "skills" / "simulation-review"
        target.mkdir(parents=True)
        (target / "SKILL.md").write_text("---\nname: simulation-review\n---\nOriginal simulation skill.\n", encoding="utf-8")
        config_path = self.private / ("agent-" + name + ".json")
        code = self.call("기기 연결", "PC " + name + " 연결 코드 발급", "POST", "/api/agent/pairing-code")
        paired = self.spawn("connect-" + name, [sys.executable, "-m", "galtalka_agent", "connect", code["code"],
            "--server", self.api, "--root", str(root), "--device-name", "simulation-PC-" + name,
            "--config", str(config_path)])
        self.require("기기 연결", "실제 CLI로 PC " + name + " 등록", paired.wait(timeout=20) == 0,
            {"project": str(root), "config": "시연 폴더 안에만 저장"})
        self.spawn("agent-" + name, [sys.executable, "-m", "galtalka_agent", "run", "--config", str(config_path)])
        devices = self.call("기기 연결", "기기 목록에서 PC " + name + " 확인", "GET", "/api/agent/devices")
        device = next(item for item in devices["devices"] if item["device_name"] == "simulation-PC-" + name)
        return {"id": device["device_id"], "root": root, "target": target / "SKILL.md"}

    def select(self, device, name):
        """다른 PC의 파일을 암묵적으로 사용하지 않도록 기본 기기를 명시한다."""
        self.call("기기 선택", "작업 기기를 PC " + name + "로 선택", "PUT", f'/api/agent/devices/{device["id"]}/primary', expected=(204,))

    def scan(self, device, name):
        """실제 agent가 읽은 환경과 비밀 항목 제외 및 승인 경로를 확인한다."""
        response = self.call("환경 스캔", "PC " + name + " 환경 스캔 요청", "POST", "/api/environment/scan",
            body={"root": ".", "scopes": ["config", "skills"]}, expected=(200, 504), key="scan-" + name)
        if "error" in response:
            job_id = response["error"]["job_id"]
            response = self.poll("/api/environment?job_id=" + job_id, lambda value: "environment_id" in value)
        self.require("환경 스캔", "PC " + name + "의 승인 경로만 스캔", response["scan_root"] == str(device["root"].resolve()), response)
        self.require("환경 스캔", "가짜 API 키가 스캔 결과에 없음", "SIMULATION_FAKE_SECRET" not in json.dumps(response),
            {"verification": response["verification"], "model_id": response["model_id"]})
        return response

    def poll(self, path, finished, *, auth="user", seconds=100):
        """실제 비동기 작업이 끝날 때까지 상태 API로만 기다린다."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = self.call("비동기 상태", path, "GET", path, auth=auth, expected=(200, 202), log=False)
            if finished(value):
                return value
            time.sleep(0.5)
        raise RuntimeError("작업 대기 시간 초과: " + path)

    def devices(self):
        """독립적인 두 agent 프로세스와 기기별 환경 및 실제 입력을 확인한다."""
        self.a = self.connect_device("A")
        self.scan(self.a, "A")
        self.b = self.connect_device("B")
        self.call("기기 격리", "새 기본 PC에 이전 PC 환경을 대신 표시하지 않음", "GET", "/api/environment", expected=(404,))
        self.scan(self.b, "B")
        devices = self.call("기기 격리", "두 기기 등록 상태와 기본 기기 확인", "GET", "/api/agent/devices")
        self.require("기기 격리", "두 기기 토큰이 모두 유지됨", len(devices["devices"]) == 2 and
            sum(item["is_primary"] for item in devices["devices"]) == 1)
        registered = self.call("실제 입력", "선택한 PC의 파일 스냅샷 등록", "POST", "/api/inputs",
            body={"relative_paths": ["app.py"], "task_type": "code_review"}, expected=(200, 201, 202))
        if "input_sha256" not in registered:
            registered = self.poll("/api/inputs/" + registered["input_id"], lambda value: "input_sha256" in value)
        self.require("실제 입력", "PC 파일의 고정 입력 해시 확인", len(registered["input_sha256"]) == 64, registered)
        self.call("승인 범위", "미지원 쓰기 스캔 요청 차단", "POST", "/api/environment/scan",
            body={"root": ".", "scopes": ["write"]}, expected=(422,))

    def catalog(self):
        """선택한 경우에만 실제 공개 원본을 제한 수집하고 추천과 상세를 확인한다."""
        if not self.live_github:
            self.record("실수집", "실제 GitHub 수집은 명시적 옵션이 없어 생략", "skipped")
            return
        job = self.call("실수집", "공개 원본 수집 요청: 최대 20회, 토큰 없음", "POST", "/api/catalog/sync", auth="operator",
            body={"repositories": [{"repository": "mattpocock/skills", "skill_folders": [
                "skills/engineering/code-review", "skills/engineering/tdd"]}], "max_requests": 20, "batch_size": 5},
            expected=(202,), key="live-catalog")
        result = self.poll("/api/catalog/jobs/" + job["id"], lambda value: value["status"] not in {"queued", "running"},
            auth="operator", seconds=150)
        self.record("실수집", "실제 수집 결과와 요청 수", "passed" if result["status"] == "done" else "blocked", result)
        cards = self.call("추천", "현재 작업에 맞는 실제 후보 추천", "GET", "/api/recommendations")["items"]
        if not cards:
            self.record("추천", "실제 수집 후보의 추천은 확인되지 않음", "blocked", {"collection_status": result["status"]})
            return
        first = cards[0]
        detail = self.call("후보 상세", "원본 고정 버전과 안전 정보 조회", "GET", "/api/candidates/" + first["candidate_version_id"])
        self.require("후보 상세", "미검토 후보가 실행 승인으로 올라가지 않음", detail["support_level"] == "information_only" and
            detail["integrity_status"] == "verified", {"publisher": detail["source_metadata"]["publisher"],
                "support_level": detail["support_level"], "integrity": detail["integrity_status"]})
        path = "/api/recommendations/" + first["recommendation_id"]
        self.call("사용자 선택", "추천을 나중에 보기로 저장", "PATCH", path, body={"status": "snoozed"}, expected=(204,))
        hidden = self.call("사용자 선택", "숨긴 추천 재조회", "GET", "/api/recommendations?include_hidden=true")["items"]
        self.require("사용자 선택", "추천 선택이 DB에 유지됨", any(item["recommendation_id"] == first["recommendation_id"] and
            item["status"] == "snoozed" for item in hidden))
        self.call("사용자 선택", "다시 보기로 복원", "PATCH", path, body={"status": "new"}, expected=(204,))

    def prepare_apply_fixture(self):
        """비교 성능을 위조하지 않고 적용 경로만 확인할 고정 전제를 별도로 만든다."""
        content = b"---\nname: simulation-review\n---\nUpdated simulation skill for file-change verification only.\n"
        payload = {"files": {"SKILL.md": base64.b64encode(content).decode()}}
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
        with self.sessions.begin() as session:
            session.add(Source(id="simulation-source", kind="demo", url="https://example.invalid/simulation", enabled=False))
            session.flush()
            session.add(Asset(id="simulation-asset", kind="skill", canonical_key="simulation:review", display_name="적용 경로 시연용 고정 자료", source_id="simulation-source"))
            session.flush()
            session.add(AssetVersion(id="simulation-version", asset_id="simulation-asset", revision="fixture-only",
                bundle_sha256=digest, metadata_json={"demo": True, "source_verified": False, "bundle": payload}, tags={"task_types": []}))
            session.flush()
            session.add(CandidateAdapter(id="simulation-adapter", asset_version_id="simulation-version", runner="codex_cli",
                supported_tasks=["code_review"], approved_bundle_sha256=digest, support_level="applicable", reviewed=True,
                runtime={"skill_name": "simulation-review"}))
            session.add(Comparison(id="simulation-comparison", profile_id=self.user_id, method_id=self.method["method_id"],
                method_version=self.method["version"], candidate_version_id="simulation-version", input_manifest=[],
                input_sha256="a" * 64, plan={"simulation_fixture": True, "actual_model_call": False}, status="done", user_decision="candidate"))
            session.flush()
            session.add(Run(id="simulation-run", comparison_id="simulation-comparison", arm="candidate", status="completed",
                skill_usage_status="confirmed", result_json={"simulation_fixture": True, "inputs": []}, metrics_json={}))
        self.record("고정 전제 적용 실험", "적용 경로만 검증할 고정 비교 결과 준비", "fixture",
            {"comparison_is_fixture": True, "external_skill_executed": False, "actual_codex_comparison": False,
             "scope": "이후 미리보기와 실제 파일 적용 및 복원만 확인"})
        return content

    def apply_and_rollback(self):
        """실제 agent의 파일 쓰기와 복원 및 잘못된 대상 기기 차단을 확인한다."""
        content = self.prepare_apply_fixture()
        before_a = self.a["target"].read_bytes()
        before_b = self.b["target"].read_bytes()
        self.select(self.b, "B")
        preview = self.call("적용 미리보기", "실제 PC B의 변경 미리보기 요청", "POST", "/api/apply/preview",
            body={"comparison_id": "simulation-comparison"}, expected=(202,))
        view = self.poll("/api/apply/preview/" + preview["preview_id"], lambda value: value["status"] not in {"queued", "running"})
        self.require("적용 미리보기", "미리보기 대상이 PC B로 고정됨", view["status"] == "done" and view["device_id"] == self.b["id"], view)
        body = {"preview_id": preview["preview_id"], "change_set_sha256": view["change_set_sha256"]}
        self.select(self.a, "A")
        blocked = self.call("안전 차단", "다른 PC에서 적용하려는 요청 거부", "POST", "/api/apply", body=body, expected=(409,))
        self.require("안전 차단", "DEVICE_CHANGED 차단과 파일 무변경", blocked["error"]["code"] == "DEVICE_CHANGED" and
            self.a["target"].read_bytes() == before_a and self.b["target"].read_bytes() == before_b)
        self.select(self.b, "B")
        applied = self.call("실제 파일 변경", "원래 PC B에 적용 요청", "POST", "/api/apply", body=body, expected=(202,))
        outcome = self.poll("/api/apply/" + applied["apply_id"], lambda value: value["status"] != "applying")
        self.require("실제 파일 변경", "PC B 파일 변경과 PC A 파일 유지", outcome["status"] == "applied" and
            self.b["target"].read_bytes() == content and self.a["target"].read_bytes() == before_a,
            {"before": before_b.decode(), "after": self.b["target"].read_text(encoding="utf-8"), "server": outcome})
        self.select(self.a, "A")
        path = "/api/apply/" + applied["apply_id"] + "/rollback"
        rejected = self.call("안전 차단", "다른 PC에서 복원하려는 요청 거부", "POST", path, body={"confirmed": True}, expected=(409,))
        self.require("안전 차단", "복원도 원래 기기를 요구함", rejected["error"]["code"] == "DEVICE_CHANGED")
        self.select(self.b, "B")
        self.call("실제 파일 복원", "원래 PC B에 복원 요청", "POST", path, body={"confirmed": True}, expected=(202,))
        restored = self.poll("/api/apply/" + applied["apply_id"], lambda value: value["status"] in {"rolled_back", "recovery_required"})
        self.require("실제 파일 복원", "PC B 원본이 바이트 단위로 복원됨", restored["status"] == "rolled_back" and
            self.b["target"].read_bytes() == before_b and self.a["target"].read_bytes() == before_a,
            {"original": before_b.decode(), "restored": self.b["target"].read_text(encoding="utf-8"), "server": restored})
        self.call("변경 이력", "적용과 복원 이력 조회", "GET", "/api/history")

    def finish(self):
        """다른 웹 세션은 유지하면서 로그아웃과 현재 지원 경계를 확인한다."""
        active = self.user
        self.user = {"Authorization": "Bearer " + self.signup_token}
        self.call("세션", "첫 번째 웹 세션만 로그아웃", "POST", "/api/auth/logout", expected=(204,))
        self.call("세션", "폐기된 세션의 접근 거부", "GET", "/api/auth/me", expected=(401,))
        self.user = active
        self.call("세션", "다른 로그인 세션은 유지됨", "GET", "/api/auth/me")
        self.record("지원 경계", "실제 Codex 비교 실행은 아직 미검증", "blocked",
            {"reason": "현재 CLI에 run_comparison 핸들러가 연결되지 않음", "model_calls": 0,
             "not_claimed": "고정 비교 전제의 적용 실험을 실제 비교 성공으로 취급하지 않음"})
        self.record("완료", "실제 프로세스 시연 완료", "passed",
            {"api": self.api, "viewer": self.viewer_url, "processes": len([child for child in self.children if child.poll() is None]),
             "report": str(self.public / "index.html"), "projects_are_temporary": True,
             "production_database_used": False, "paid_api_used": False})

    def close(self):
        """직접 실행한 프로세스와 고유 DB 스키마만 정리하고 보고서는 보존한다."""
        for child in reversed(self.children):
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
        for output in self.logs:
            output.close()
        self.http.close()
        self.viewer.shutdown()
        self.viewer.server_close()
        self.engine.dispose()
        if self.created_schema:
            with self.admin.begin() as connection:
                connection.execute(DropSchema(self.schema, cascade=True))
        self.admin.dispose()


def main():
    """실제 동작 시연 후 선택적으로 loopback 서버를 관찰용으로 유지한다."""
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True, help="localhost의 별도 be_simulation PostgreSQL URL")
    parser.add_argument("--live-github", action="store_true", help="공개 원본 한 저장소를 최대 20회 요청으로 수집")
    parser.add_argument("--keep-running", action="store_true", help="완료 후 보고서와 서버 및 agent를 유지")
    args = parser.parse_args()
    simulation = Simulation(args.database_url, live_github=args.live_github)
    print("VIEWER=" + simulation.viewer_url, flush=True)
    print("SWAGGER=" + simulation.api + "/docs", flush=True)
    failed = False
    try:
        try:
            simulation.start()
            simulation.accounts()
            simulation.devices()
            simulation.catalog()
            simulation.apply_and_rollback()
            simulation.finish()
        except Exception as error:
            failed = True
            simulation.record("실행 중단", "시연 중 확인이 필요한 오류", "failed", {"type": type(error).__name__, "message": str(error)})
        if args.keep_running:
            print("시연 환경을 유지합니다. 종료하려면 Ctrl+C. 보고서: " + simulation.viewer_url, flush=True)
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        print("시연 환경을 정리합니다.", flush=True)
    finally:
        simulation.close()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
