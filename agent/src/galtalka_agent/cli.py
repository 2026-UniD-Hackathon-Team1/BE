"""명시적 연결과 수동 실행만 제공하는 연결 프로그램 명령."""
import argparse
import asyncio
import platform
import re
import sys
from pathlib import Path

from galtalka_agent import __version__
from galtalka_agent.client import AgentClient
from galtalka_agent.config import (AgentConfig, AgentError, approved_root, default_config_path,
    load_config, normalize_server, runtime_lock, save_config)
from galtalka_agent.worker import AgentRunner


async def connect(args, transport=None):
    """코드와 승인 루트를 확인하고 서버가 발급한 기기 토큰을 로컬에 저장한다."""
    if not re.fullmatch(r"[A-Za-z2-9]{4}-?[A-Za-z2-9]{4}", args.code):
        raise AgentError("8자리 연결 코드를 확인해 주세요.")
    server, root = normalize_server(args.server), approved_root(args.root)
    device = args.device_name or platform.node() or "local-device"
    if not device.strip() or len(device) > 100:
        raise AgentError("기기 이름은 1자부터 100자까지 입력해 주세요.")
    with runtime_lock(args.config):
        client = AgentClient(server, transport=transport)
        try:
            token = await client.pair(args.code, device, __version__)
            save_config(args.config, AgentConfig(server, root, token))
        finally:
            await client.close()


async def run(args, transport=None):
    """저장된 연결만 사용하며 같은 설정으로 두 프로그램을 실행하지 못하게 한다."""
    with runtime_lock(args.config):
        config = load_config(args.config)
        client = AgentClient(config.server_url, config.agent_token, transport=transport)
        try:
            await AgentRunner(client, config, args.config).run(once=args.once)
        finally:
            await client.close()


def main(argv=None, *, transport=None):
    """CLI 오류는 비밀값과 traceback 없이 안내하고 종료 코드를 반환한다."""
    parser = argparse.ArgumentParser(prog="galtalka", description="갈아탈까 PC 연결 프로그램")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    pairing = commands.add_parser("connect", help="연결 코드로 이 PC 등록")
    pairing.add_argument("code")
    pairing.add_argument("--server", required=True, help="Railway HTTPS 서버 주소")
    pairing.add_argument("--root", type=Path, required=True, help="사용자가 승인한 프로젝트 폴더")
    pairing.add_argument("--device-name")
    pairing.add_argument("--config", type=Path, default=default_config_path())
    worker = commands.add_parser("run", help="heartbeat와 작업 수신 시작")
    worker.add_argument("--config", type=Path, default=default_config_path())
    worker.add_argument("--once", action="store_true", help="한 번 확인한 뒤 종료")
    args = parser.parse_args(argv)
    try:
        if args.command == "connect":
            asyncio.run(connect(args, transport))
            print("기기를 연결했습니다. galtalka run으로 연결 프로그램을 시작하세요.")
        else:
            print("연결 프로그램을 시작합니다. 종료하려면 Ctrl+C를 누르세요.")
            asyncio.run(run(args, transport))
        return 0
    except KeyboardInterrupt:
        print("연결 프로그램을 종료했습니다.")
        return 130
    except AgentError as error:
        print(str(error), file=sys.stderr)
        return 1
    except OSError:
        print("로컬 연결 파일에 접근할 수 없습니다. 경로와 권한을 확인해 주세요.", file=sys.stderr)
        return 1
