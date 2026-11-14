"""배포 플랫폼의 PORT를 적용하는 API 실행 명령."""
import os
from pathlib import Path
import uvicorn

from backend.settings import validate_deployment


def load_local_env():
    """Load simple KEY=VALUE settings from the project .env without overriding shell values."""
    env_path = Path.cwd() / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip("\"'")
        if key and key.replace("_", "").isalnum():
            os.environ.setdefault(key, value)


def main():
    """배포 설정을 검사하고 플랫폼 포트에 API를 실행한다."""
    load_local_env()
    validate_deployment()
    port = int(os.environ.get("PORT", "8765"))
    if not 1 <= port <= 65535:
        raise ValueError("PORT must be between 1 and 65535")
    uvicorn.run("backend.main:app", host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
