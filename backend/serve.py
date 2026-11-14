"""배포 플랫폼의 PORT를 적용하는 API 실행 명령."""
import os
import uvicorn

from backend.settings import validate_deployment


def main():
    """배포 설정을 검사하고 플랫폼 포트에 API를 실행한다."""
    validate_deployment()
    port = int(os.environ.get("PORT", "8765"))
    if not 1 <= port <= 65535:
        raise ValueError("PORT must be between 1 and 65535")
    uvicorn.run("backend.main:app", host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
