from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class RunResult:
    status: str
    exit_code: int | None
    stdout: str
    stderr: str


def run_codex_once(
    command: list[str],
    *,
    cwd: Path,
    prompt: str,
    stdout_path: Path,
    timeout: int,
    max_output_bytes: int = 2 * 1024 * 1024,
) -> RunResult:
    stdout_path.unlink(missing_ok=True)
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path = stdout_path.with_suffix(".stderr")
    stderr_path.unlink(missing_ok=True)
    with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
        proc = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE,
            stdout=stdout_file, stderr=stderr_file)
        try:
            proc.stdin.write(prompt.encode("utf-8"))
            proc.stdin.close()
            started = time.monotonic()
            while proc.poll() is None:
                if stdout_path.stat().st_size + stderr_path.stat().st_size > max_output_bytes:
                    proc.kill()
                    proc.wait()
                    return RunResult("output_limit", proc.returncode, "", "")
                if time.monotonic() - started > timeout:
                    proc.kill()
                    proc.wait()
                    stdout_path.unlink(missing_ok=True)
                    return RunResult("timeout", None, "", "")
                time.sleep(0.02)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
    if stdout_path.stat().st_size + stderr_path.stat().st_size > max_output_bytes:
        return RunResult("output_limit", proc.returncode, "", "")
    return RunResult(
        "completed" if proc.returncode == 0 else "failed",
        proc.returncode,
        stdout_path.read_text(encoding="utf-8", errors="replace"),
        stderr_path.read_text(encoding="utf-8", errors="replace"),
    )
