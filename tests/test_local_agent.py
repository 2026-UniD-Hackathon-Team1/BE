import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from galtalka_agent.cli import main
from galtalka_agent.client import APIError, AgentClient, ClaimedJob
from galtalka_agent.config import (AgentConfig, AgentError, load_config, normalize_server,
    runtime_lock, save_config, write_json)
from galtalka_agent.worker import AgentRunner

TOKEN = "t" * 43
CLAIM_TOKEN = "c" * 43
JOB_ID = "job_" + "a" * 32
SERVER = "https://example.test"


def claim_data(kind="scan"):
    """실제 서버 계약과 같은 모의 작업 응답을 만든다."""
    expires = (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat()
    return {"job_id": JOB_ID, "kind": kind, "attempt": 1, "claim_token": CLAIM_TOKEN,
            "lease_expires_at": expires, "payload": {"scope_id": "approved"}}


def test_connect_stores_config_and_restarts_without_leaking_token(tmp_path, capsys):
    """코드 교환과 설정 재사용을 확인하고 원문 토큰을 출력하지 않는다."""
    path = tmp_path / "connection" / "config.json"
    seen = []

    def handle(request):
        """모의 서버에서 코드 교환과 이후 기기 인증을 검사한다."""
        seen.append(request.url.path)
        if request.url.path == "/api/agent/pair":
            assert "Authorization" not in request.headers
            assert json.loads(request.content)["code"] == "ABCD-EFGH"
            return httpx.Response(200, json={"agent_token": TOKEN})
        assert request.headers["Authorization"] == "Bearer " + TOKEN
        if request.url.path.endswith("heartbeat"):
            assert json.loads(request.content) == {"codex_version": None, "codex_ready": False}
            return httpx.Response(204)
        return httpx.Response(200, content="null", headers={"Content-Type": "application/json"})

    transport = httpx.MockTransport(handle)
    assert main(["connect", "ABCD-EFGH", "--server", SERVER, "--root", str(tmp_path),
                 "--config", str(path), "--device-name", "test-pc"], transport=transport) == 0
    config = load_config(path)
    assert config.agent_token == TOKEN and config.project_root == tmp_path.resolve()
    assert TOKEN not in repr(config)
    assert main(["run", "--once", "--config", str(path)], transport=transport) == 0
    assert seen == ["/api/agent/pair", "/api/agent/heartbeat", "/api/agent/jobs"]
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err


@pytest.mark.parametrize("url", ["http://example.test", "https://user:secret@example.test",
    "https://example.test/?token=secret", "https://example.test/#secret", "file:///tmp/config"])
def test_insecure_servers_are_rejected_before_pairing(tmp_path, url, capsys):
    """원격 평문 연결과 URL의 인증 정보 및 비밀값 전달을 차단한다."""
    def forbidden(request):
        """입력 검증 실패 시 외부 요청이 발생하지 않음을 확인한다."""
        pytest.fail("network must not be used")
    assert main(["connect", "ABCD-EFGH", "--server", url, "--root", str(tmp_path),
        "--config", str(tmp_path / "config.json")], transport=httpx.MockTransport(forbidden)) == 1
    assert "secret" not in capsys.readouterr().err
    assert not (tmp_path / "config.json").exists()


def test_local_development_and_missing_root_validation(tmp_path):
    """로컬 HTTP는 허용하지만 없는 프로젝트 루트를 자동 선택하지 않는다."""
    assert normalize_server("http://127.0.0.1:8765/") == "http://127.0.0.1:8765"
    def forbidden(request):
        """승인 루트가 없으면 코드 교환을 하지 않는다."""
        pytest.fail("network must not be used")
    assert main(["connect", "ABCD-EFGH", "--server", SERVER, "--root", str(tmp_path / "missing"),
        "--config", str(tmp_path / "config.json")], transport=httpx.MockTransport(forbidden)) == 1


def test_runtime_lock_rejects_duplicate_and_releases_on_exit(tmp_path):
    """동일 설정의 중복 실행은 차단하고 종료 후에는 다시 실행할 수 있다."""
    path = tmp_path / "config.json"
    with runtime_lock(path):
        with pytest.raises(AgentError):
            with runtime_lock(path):
                pytest.fail("second lock must fail")
    with runtime_lock(path):
        pass


def test_redirect_and_server_error_do_not_expose_token(tmp_path, capsys):
    """인증 헤더를 리디렉션 대상에 보내지 않고 오류 본문도 출력하지 않는다."""
    path = tmp_path / "config.json"
    save_config(path, AgentConfig(SERVER, tmp_path, TOKEN))
    seen = []
    def handle(request):
        """다른 서버로 보내는 리디렉션을 반환한다."""
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://other.test/"}, text=TOKEN)
    assert main(["run", "--once", "--config", str(path)], transport=httpx.MockTransport(handle)) == 1
    assert seen == [SERVER + "/api/agent/heartbeat"]
    assert TOKEN not in capsys.readouterr().err


def test_unknown_job_is_reported_without_running_commands(tmp_path):
    """핸들러가 없는 작업을 임의 실행하지 않고 실패로 보고한다."""
    async def scenario():
        """작업 수신과 보고를 모의 HTTP로만 수행한다."""
        reports = []
        def handle(request):
            """임의 명령이 포함된 작업을 수신하고 실패 보고를 수집한다."""
            if request.url.path.endswith("heartbeat"):
                return httpx.Response(204)
            if request.method == "GET":
                value = claim_data("shell")
                value["payload"] = {"command": "must never execute"}
                return httpx.Response(200, json=value)
            reports.append(json.loads(request.content))
            return httpx.Response(204)
        client = AgentClient(SERVER, TOKEN, transport=httpx.MockTransport(handle))
        runner = AgentRunner(client, AgentConfig(SERVER, tmp_path, TOKEN), tmp_path / "config.json")
        try:
            await runner.run(once=True)
            assert reports[0]["status"] == "failed"
            assert reports[0]["claim_token"] == CLAIM_TOKEN
            assert not runner.pending_path.exists()
        finally:
            await client.close()
    asyncio.run(scenario())


def test_result_retry_after_restart_does_not_execute_twice(tmp_path):
    """결과 응답 유실 후 재시작하면 같은 결과를 보고하고 작업은 다시 실행하지 않는다."""
    async def scenario():
        """결과 첫 전송만 실패하는 모의 서버로 재시작 경로를 검증한다."""
        executed, reports = [], []
        async def handler(payload, root):
            """승인 루트만 받아 모의 결과를 반환한다."""
            executed.append((payload, root))
            return {"languages": ["python"]}
        def handle(request):
            """같은 결과 재전송 본문이 유지되는지 확인한다."""
            if request.url.path.endswith("heartbeat") or request.url.path.endswith("progress"):
                return httpx.Response(204)
            if request.method == "GET":
                return httpx.Response(200, json=claim_data())
            reports.append(json.loads(request.content))
            return httpx.Response(503 if len(reports) == 1 else 204, text=TOKEN)
        config = AgentConfig(SERVER, tmp_path, TOKEN)
        client = AgentClient(SERVER, TOKEN, transport=httpx.MockTransport(handle))
        try:
            runner = AgentRunner(client, config, tmp_path / "config.json", handlers={"scan": handler})
            with pytest.raises(APIError):
                await runner.run(once=True)
            assert runner.pending_path.exists()
            restarted = AgentRunner(client, config, tmp_path / "config.json", handlers={"scan": handler})
            await restarted.run(once=True)
            assert len(executed) == 1
            assert executed[0][1] == tmp_path
            assert reports[0] == reports[1] and reports[1]["status"] == "done"
            assert not restarted.pending_path.exists()
        finally:
            await client.close()
    asyncio.run(scenario())


def test_interrupted_claim_is_reported_failed_on_restart(tmp_path):
    """실행 중 중단된 보류 기록을 재실행하지 않고 실패로 보고한다."""
    async def scenario():
        """이전 프로세스의 실행 기록만 남은 상태를 모의한다."""
        reports = []
        def handle(request):
            """복구 시 새 작업 인수를 금지하고 실패 보고만 허용한다."""
            assert request.method != "GET"
            if request.url.path.endswith("result"):
                reports.append(json.loads(request.content))
            return httpx.Response(204)
        config = AgentConfig(SERVER, tmp_path, TOKEN)
        client = AgentClient(SERVER, TOKEN, transport=httpx.MockTransport(handle))
        runner = AgentRunner(client, config, tmp_path / "config.json")
        runner.start_pending(ClaimedJob.parse(claim_data()))
        try:
            await runner.run(once=True)
            assert reports[0]["status"] == "failed"
            assert not runner.pending_path.exists()
        finally:
            await client.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("status", [401, 409])
def test_lost_authority_cancels_handler(tmp_path, status):
    """진행 보고에서 인증 또는 실행 권한이 사라지면 핸들러를 취소한다."""
    async def scenario():
        """모의 시간과 진행 오류로 취소 및 보류 처리 경로를 확인한다."""
        clock = [0]
        stopped = asyncio.Event()
        async def handler(payload, root):
            """가짜 장기 작업이 취소됐는지 기록한다."""
            try:
                clock[0] = 31
                await asyncio.Event().wait()
            finally:
                stopped.set()
        progress = []
        def handle(request):
            """두 번째 진행 보고에서 실행 권한을 거부한다."""
            if request.url.path.endswith("progress"):
                progress.append(True)
                return httpx.Response(204 if len(progress) == 1 else status)
            if request.method == "GET":
                return httpx.Response(200, json=claim_data())
            return httpx.Response(204)
        client = AgentClient(SERVER, TOKEN, transport=httpx.MockTransport(handle))
        runner = AgentRunner(client, AgentConfig(SERVER, tmp_path, TOKEN), tmp_path / "config.json",
            handlers={"scan": handler}, clock=lambda: clock[0])
        try:
            if status == 401:
                with pytest.raises(APIError):
                    await runner.run(once=True)
            else:
                await runner.run(once=True)
            assert stopped.is_set()
            assert runner.pending_path.exists() == (status == 401)
        finally:
            await client.close()
    asyncio.run(scenario())


def test_user_cancellation_preserves_report_and_cleans_up_handler(tmp_path):
    """Ctrl+C에 대응하는 취소 시 로컬 핸들러를 정리하고 보류 보고를 남긴다."""
    async def scenario():
        """종료 이벤트가 도착한 장기 작업을 안전하게 중단한다."""
        started, cleaned = asyncio.Event(), asyncio.Event()
        async def handler(payload, root):
            """취소 뒤 자원 정리 구간이 실행되는지 기록한다."""
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        def handle(request):
            """수신과 진행 확인만 응답한다."""
            return httpx.Response(200, json=claim_data()) if request.method == "GET" else httpx.Response(204)
        client = AgentClient(SERVER, TOKEN, transport=httpx.MockTransport(handle))
        runner = AgentRunner(client, AgentConfig(SERVER, tmp_path, TOKEN), tmp_path / "config.json", handlers={"scan": handler})
        task = asyncio.create_task(runner.run(once=True))
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert cleaned.is_set()
            assert runner.load_pending()["report"]["status"] == "failed"
        finally:
            task.cancel()
            await client.close()
    asyncio.run(scenario())


def test_idle_backoff_is_bounded_and_heartbeat_is_separate(tmp_path):
    """빈 큐 대기는 늘어나지만 heartbeat 주기에 맞춰 대기를 끊는다."""
    async def scenario():
        """실제 대기 없이 모의 시각으로 폴링 간격을 확인한다."""
        clock, sleeps, heartbeats = [0.0], [], []
        def handle(request):
            """각 heartbeat의 모의 시각을 수집한다."""
            if request.url.path.endswith("heartbeat"):
                heartbeats.append(clock[0])
                return httpx.Response(204)
            return httpx.Response(200, content="null", headers={"Content-Type": "application/json"})
        client = AgentClient(SERVER, TOKEN, transport=httpx.MockTransport(handle))
        runner = AgentRunner(client, AgentConfig(SERVER, tmp_path, TOKEN), tmp_path / "config.json",
            clock=lambda: clock[0], jitter=lambda low, high: (low + high) / 2)
        async def wait(delay):
            """가짜 대기로 시각을 진행시키고 반복 횟수를 제한한다."""
            sleeps.append(delay)
            clock[0] += delay
            if len(sleeps) == 8:
                runner.stop.set()
        runner.wait = wait
        try:
            await runner.run()
            assert sleeps[:4] == [2, 4, 8, 16]
            assert all(0 <= delay <= 30 for delay in sleeps)
            assert len(heartbeats) >= 2 and heartbeats[1] == 30
        finally:
            await client.close()
    asyncio.run(scenario())


def test_stale_connection_pending_is_not_sent_with_new_token(tmp_path):
    """재연결 시 이전 토큰에 속한 결과를 새 기기 인증으로 전송하지 않는다."""
    path = tmp_path / "config.json"
    old = AgentRunner(None, AgentConfig(SERVER, tmp_path, TOKEN), path)
    old.start_pending(ClaimedJob.parse(claim_data()))
    fresh = AgentRunner(None, AgentConfig(SERVER, tmp_path, "n" * 43), path)
    assert fresh.load_pending() is None
    assert not fresh.pending_path.exists()


def test_corrupt_config_and_invalid_job_never_expose_values(tmp_path):
    """손상된 비밀 설정과 잘못된 작업 경로를 실행 전에 거부한다."""
    path = tmp_path / "config.json"
    write_json(path, {"agent_token": TOKEN, "unexpected": True})
    with pytest.raises(AgentError) as error:
        load_config(path)
    assert TOKEN not in str(error.value)
    with pytest.raises(AgentError):
        ClaimedJob.parse({**claim_data(), "job_id": "../../private"})
