"""기기 생존과 작업 수신을 유지하고 실행 결과를 재시작 후에도 보고한다."""
import asyncio
import json
import random
import re
import time

from galtalka_agent.client import APIError
from galtalka_agent.config import AgentError, read_json, valid_token, write_json


class AgentRunner:
    def __init__(self, client, config, config_path, handlers=None, stop=None, clock=time.monotonic,
                 jitter=random.uniform):
        """서버가 아닌 로컬 코드에서 등록한 핸들러만 실행하도록 구성한다."""
        self.client = client
        self.config = config
        self.pending_path = config_path.with_suffix(".pending.json")
        self.handlers = handlers or {}
        self.stop = stop if stop is not None else asyncio.Event()
        self.clock = clock
        self.jitter = jitter
        self._next_heartbeat = 0

    def clear_pending(self):
        """현재 설정에 대응하는 보류 기록 한 파일만 정리한다."""
        if self.pending_path.is_symlink():
            raise AgentError("보류 기록 경로가 올바르지 않습니다.")
        self.pending_path.unlink(missing_ok=True)

    def load_pending(self):
        """이전 실행의 보고를 복구하고 다른 연결의 기록은 전송하지 않는다."""
        if not self.pending_path.exists() and not self.pending_path.is_symlink():
            return None
        state = read_json(self.pending_path)
        if (not isinstance(state, dict) or set(state) != {"identity", "job_id", "claim_token", "report"}
            or not isinstance(state["job_id"], str) or not re.fullmatch(r"job_[a-f0-9]{32}", state["job_id"])
            or not valid_token(state["claim_token"])):
            raise AgentError("보류 작업 기록이 올바르지 않습니다.")
        if state["identity"] != self.config.identity:
            self.clear_pending()
            return None
        if state["report"] is None:
            state["report"] = {"claim_token": state["claim_token"], "status": "failed",
                "message": "연결 프로그램이 중단됐습니다. 실행 상태를 확인한 뒤 다시 요청해 주세요."}
            write_json(self.pending_path, state)
        report = state["report"]
        if (not isinstance(report, dict) or report.get("claim_token") != state["claim_token"]
            or report.get("status") not in {"done", "failed"}
            or set(report) - {"claim_token", "status", "result", "message"}):
            raise AgentError("보류 결과 기록이 올바르지 않습니다.")
        return state

    def start_pending(self, job):
        """작업 실행 전에 인수 정보를 저장해 재시작 시 중복 실행을 막는다."""
        write_json(self.pending_path, {"identity": self.config.identity, "job_id": job.job_id,
            "claim_token": job.claim_token, "report": None})

    def save_report(self, job, status, result=None, message=None):
        """결과를 네트워크 전송 전에 저장해 같은 본문으로 재전송한다."""
        report = {"claim_token": job.claim_token, "status": status, "result": result, "message": message}
        write_json(self.pending_path, {"identity": self.config.identity, "job_id": job.job_id,
            "claim_token": job.claim_token, "report": report})

    async def flush_pending(self):
        """확인되지 않은 보고를 먼저 보내고 이미 만료된 인수는 정리한다."""
        state = self.load_pending()
        if state is None:
            return
        try:
            await self.client.report(state["job_id"], state["report"])
        except APIError as error:
            if error.status not in {404, 409}:
                raise
        self.clear_pending()

    async def cancel_handler(self, task):
        """로컬 핸들러에 취소를 전달하고 정리가 끝난 뒤 다음 작업으로 넘어간다."""
        if task is not None and not task.done():
            task.cancel()
        if task is not None:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def execute(self, job):
        """진행 보고가 실패하면 핸들러를 중단하고 실행을 반복하지 않는다."""
        self.start_pending(job)
        handler = self.handlers.get(job.kind)
        if handler is None:
            self.save_report(job, "failed", message="이 연결 프로그램 버전에서 지원하지 않는 작업입니다.")
            await self.flush_pending()
            return
        task = None
        try:
            await self.client.progress(job)
            task = asyncio.create_task(handler(job.payload, self.config.project_root))
            next_progress = self.clock() + 30
            while not task.done():
                await asyncio.wait({task}, timeout=0.25)
                if self.stop.is_set():
                    raise asyncio.CancelledError
                if self.clock() >= next_progress and not task.done():
                    await self.client.heartbeat()
                    self._next_heartbeat = self.clock() + 30
                    await self.client.progress(job)
                    next_progress = self.clock() + 30
            result = task.result()
            if not isinstance(result, dict) or len(json.dumps(result, allow_nan=False).encode()) > 256 * 1024:
                raise ValueError
            self.save_report(job, "done", result=result)
        except asyncio.CancelledError:
            await self.cancel_handler(task)
            self.save_report(job, "failed", message="사용자가 연결 프로그램을 중단했습니다. 실행 상태를 확인해 주세요.")
            raise
        except APIError as error:
            await self.cancel_handler(task)
            if error.status in {404, 409}:
                self.clear_pending()
                return
            self.save_report(job, "failed", message="작업 진행을 확인하지 못해 실행을 중단했습니다.")
            raise
        except Exception:
            await self.cancel_handler(task)
            self.save_report(job, "failed", message="작업 처리에 실패했습니다. 연결 프로그램 상태를 확인해 주세요.")
        await self.flush_pending()

    async def tick(self):
        """생존 확인과 보류 보고를 우선하고 승인된 작업 하나만 인수한다."""
        if self.clock() >= self._next_heartbeat:
            await self.client.heartbeat()
            self._next_heartbeat = self.clock() + 30
        if self.load_pending() is not None:
            await self.flush_pending()
            return True
        job = await self.client.claim()
        if job is None:
            return False
        await self.execute(job)
        return True

    async def wait(self, delay):
        """종료 요청이 오면 폴링 대기를 즉시 끝낸다."""
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=delay)
        except TimeoutError:
            pass

    async def run(self, once=False):
        """빈 큐와 일시 오류에는 backoff를 적용하고 인증 해제 시 종료한다."""
        delay = 2
        while not self.stop.is_set():
            failed = False
            try:
                active = await self.tick()
            except APIError as error:
                if not error.transient or once:
                    raise
                failed, active = True, False
            if once:
                return
            base = 2 if active else delay
            sleep = min(30, self.jitter(base * 0.8, base * 1.2))
            delay = 2 if active else min(base * 2, 30)
            if not failed:
                sleep = min(sleep, max(0, self._next_heartbeat - self.clock()))
            await self.wait(sleep)
