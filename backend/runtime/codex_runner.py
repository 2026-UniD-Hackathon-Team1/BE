from __future__ import annotations

import subprocess
import os
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class RunResult:
    status: str
    exit_code: int | None
    stdout: str
    stderr: str


def stop_process(proc):
    """Windows 실행기가 만든 자식 프로세스까지 종료하고 파일 핸들을 반환한다."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    if proc.poll() is None:
        proc.kill()
    proc.wait()


def run_codex_once(
    command: list[str],
    *,
    cwd: Path,
    prompt: str,
    stdout_path: Path,
    timeout: int,
    max_output_bytes: int = 2 * 1024 * 1024,
) -> RunResult:
    """제한 시간과 출력 크기를 지키며 실행하고 시간 초과 결과 파일을 정리한다."""
    stdout_path.unlink(missing_ok=True)
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path = stdout_path.with_suffix(".stderr")
    stderr_path.unlink(missing_ok=True)
    timed_out = False
    with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
        proc = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE,
            stdout=stdout_file, stderr=stderr_file)
        try:
            proc.stdin.write(prompt.encode("utf-8"))
            proc.stdin.close()
            started = time.monotonic()
            while proc.poll() is None:
                if stdout_path.stat().st_size + stderr_path.stat().st_size > max_output_bytes:
                    stop_process(proc)
                    return RunResult("output_limit", proc.returncode, "", "")
                if time.monotonic() - started > timeout:
                    stop_process(proc)
                    timed_out = True
                    break
                time.sleep(0.02)
        finally:
            if proc.poll() is None:
                stop_process(proc)
    if timed_out:
        stdout_path.unlink(missing_ok=True)
        return RunResult("timeout", None, "", "")
    if stdout_path.stat().st_size + stderr_path.stat().st_size > max_output_bytes:
        return RunResult("output_limit", proc.returncode, "", "")
    return RunResult(
        "completed" if proc.returncode == 0 else "failed",
        proc.returncode,
        stdout_path.read_text(encoding="utf-8", errors="replace"),
        stderr_path.read_text(encoding="utf-8", errors="replace"),
    )
