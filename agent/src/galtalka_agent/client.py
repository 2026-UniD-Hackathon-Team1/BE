"""리디렉션 없이 인증된 서버와만 통신하는 연결 프로그램 HTTP 클라이언트."""
import json
from dataclasses import dataclass, field
from datetime import datetime
import re

import httpx

from galtalka_agent.config import AgentError, normalize_server, valid_token


class APIError(AgentError):
    def __init__(self, status=None):
        """서버 본문과 토큰 없이 상태 코드만 보관하는 오류를 만든다."""
        self.status = status
        self.transient = status is None or status == 429 or status >= 500
        message = "서버에 연결할 수 없습니다. 연결 상태를 확인해 주세요."
        if status == 401:
            message = "기기 인증이 해제됐습니다. 새 연결 코드로 connect를 실행해 주세요."
        elif status is not None:
            message = f"서버 요청에 실패했습니다(HTTP {status})."
        super().__init__(message)


@dataclass(frozen=True)
class ClaimedJob:
    job_id: str
    kind: str
    attempt: int
    lease_expires_at: str
    claim_token: str = field(repr=False)
    payload: dict = field(repr=False)

    @classmethod
    def parse(cls, value):
        """서버 응답의 식별자와 인수 정보를 검사해 경로 및 명령 주입을 막는다."""
        try:
            if (not isinstance(value, dict) or not re.fullmatch(r"job_[a-f0-9]{32}", value["job_id"])
                or not isinstance(value["kind"], str) or not 1 <= len(value["kind"]) <= 80
                or not valid_token(value["claim_token"]) or not isinstance(value["payload"], dict)
                or not isinstance(value["attempt"], int) or value["attempt"] < 1
                or len(json.dumps(value["payload"], allow_nan=False).encode()) > 128 * 1024):
                raise ValueError
            if datetime.fromisoformat(value["lease_expires_at"]).utcoffset() is None:
                raise ValueError
            return cls(**{key: value[key] for key in cls.__dataclass_fields__})
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            raise AgentError("서버의 작업 응답이 올바르지 않습니다.") from None


class AgentClient:
    def __init__(self, server_url, agent_token=None, transport=None):
        """HTTPS와 제한 시간을 적용하며 토큰이 다른 서버로 전달되지 않게 한다."""
        self._token = agent_token
        self._http = httpx.AsyncClient(base_url=normalize_server(server_url), transport=transport,
            timeout=httpx.Timeout(10, connect=5), follow_redirects=False)

    async def close(self):
        """프로그램 종료 시 HTTP 연결을 반환한다."""
        await self._http.aclose()

    async def request(self, method, path, body=None):
        """응답 크기를 제한하고 서버 오류 원문이나 인증 헤더를 출력하지 않는다."""
        headers = {"Authorization": "Bearer " + self._token} if self._token else {}
        try:
            async with self._http.stream(method, path, json=body, headers=headers) as response:
                if not 200 <= response.status_code < 300:
                    raise APIError(response.status_code)
                if response.status_code == 204:
                    return None
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > 512 * 1024:
                        raise AgentError("서버 응답 크기가 제한을 초과했습니다.")
                return json.loads(content)
        except httpx.RequestError:
            raise APIError() from None
        except (ValueError, RecursionError):
            raise AgentError("서버 응답을 읽을 수 없습니다.") from None

    async def pair(self, code, device_name, agent_version):
        """일회용 연결 코드를 교환하고 응답 토큰 형식을 검사한다."""
        value = await self.request("POST", "/api/agent/pair", {
            "code": code, "device_name": device_name, "agent_version": agent_version})
        if not isinstance(value, dict) or not valid_token(value.get("agent_token")):
            raise AgentError("연결 토큰을 받지 못했습니다. 새 코드를 발급해 주세요.")
        return value["agent_token"]

    async def heartbeat(self):
        """아직 검증하지 않은 Codex 준비 상태를 완료로 주장하지 않고 생존을 보고한다."""
        await self.request("POST", "/api/agent/heartbeat", {"codex_version": None, "codex_ready": False})

    async def claim(self):
        """빈 큐는 None으로 반환하고 작업 응답은 검증한 뒤 전달한다."""
        value = await self.request("GET", "/api/agent/jobs")
        return None if value is None else ClaimedJob.parse(value)

    async def progress(self, job):
        """기기 토큰과 인수 토큰을 구분해 진행 상태와 실행 기한을 갱신한다."""
        await self.request("POST", f"/api/agent/jobs/{job.job_id}/progress",
            {"claim_token": job.claim_token, "step": "running"})

    async def report(self, job_id, body):
        """보류 중인 같은 결과 본문을 멱등적인 결과 API로 전송한다."""
        await self.request("POST", f"/api/agent/jobs/{job_id}/result", body)
