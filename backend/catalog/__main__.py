"""명시적인 네트워크 실행 옵션이 있어야 동작하는 수집 명령."""
import argparse
import json
from pathlib import Path

from backend.catalog.config import load_config
from backend.catalog.github_client import GithubClient
from backend.catalog.service import CatalogCollector
from backend.db import configured_engine, session_factory


def main():
    """설정을 먼저 검증하고 사용자가 요청한 경우에만 공개 GitHub API를 호출한다."""
    parser = argparse.ArgumentParser(description="GitHub 공개 후보 발견 및 수집")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--execute-network", action="store_true", help="실제 공개 GitHub API 호출 허용")
    args = parser.parse_args()
    config = load_config(args.config)
    if not args.execute_network:
        print(json.dumps({"status": "configuration_valid", "network_executed": False,
                          "config": config.model_dump()}, ensure_ascii=False))
        return
    engine = configured_engine()
    client = GithubClient()
    try:
        result = CatalogCollector(session_factory(engine), client).run(config)
        print(json.dumps(result, ensure_ascii=False))
    finally:
        client.close()
        engine.dispose()


if __name__ == "__main__":
    main()
