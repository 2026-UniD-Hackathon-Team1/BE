"""연결 설정과 보류 보고를 원자적으로 저장하며 비밀값 출력을 막는다."""
import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


class AgentError(Exception):
    """사용자에게 원문 비밀값 없이 표시할 수 있는 실행 오류."""


def normalize_server(value):
    """HTTPS 주소와 로컬 개발용 HTTP 주소만 허용하고 인증 정보는 거부한다."""
    try:
        parsed = urlsplit(value)
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (not parsed.hostname or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
            or not (parsed.scheme == "https" or parsed.scheme == "http" and local)):
            raise ValueError
        parsed.port
    except ValueError:
        raise AgentError("서버는 HTTPS 주소로 지정해 주세요. 로컬 개발에서만 HTTP를 사용할 수 있습니다.") from None
    return value.rstrip("/")


def approved_root(value):
    """사용자가 명시한 기존 프로젝트 폴더만 승인 루트로 저장한다."""
    try:
        root = Path(value).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError
        return root
    except (OSError, ValueError, TypeError):
        raise AgentError("기존 프로젝트 폴더를 --root로 지정해 주세요.") from None


def valid_token(value):
    """서버가 발급한 URL 안전 토큰 형식을 확인한다."""
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{32,128}", value) is not None


@dataclass(frozen=True)
class AgentConfig:
    server_url: str
    project_root: Path
    agent_token: str = field(repr=False)

    @property
    def identity(self):
        """재연결 후 이전 연결의 보류 작업을 보내지 않도록 식별 해시를 만든다."""
        content = json.dumps([self.server_url, str(self.project_root), self.agent_token])
        return hashlib.sha256(content.encode()).hexdigest()


def default_config_path():
    """사용자가 명령을 실행할 때만 접근할 기본 설정 경로를 반환한다."""
    return Path.home() / ".galtalka" / "config.json"


def read_json(path):
    """크기와 링크 여부를 검사하고 비밀값을 오류 메시지에 포함하지 않는다."""
    try:
        if path.is_symlink() or path.stat().st_size > 512 * 1024:
            raise ValueError
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        raise AgentError("로컬 연결 설정 또는 보류 기록을 읽을 수 없습니다.") from None


def write_json(path, value):
    """비밀값 파일을 제한된 권한의 임시 파일로 쓴 뒤 원자적으로 교체한다."""
    temporary = None
    try:
        if path.is_symlink():
            raise ValueError
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > 512 * 1024:
            raise ValueError
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".galtalka-")
        with os.fdopen(descriptor, "wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        temporary = None
    except (OSError, ValueError, TypeError, RecursionError):
        raise AgentError("로컬 연결 정보를 안전하게 저장할 수 없습니다. 저장 경로를 확인해 주세요.") from None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def load_config(path):
    """현재 연결 설정과 승인 루트를 검사하고 손상된 설정은 사용하지 않는다."""
    data = read_json(path)
    if (not isinstance(data, dict) or set(data) != {"version", "server_url", "project_root", "agent_token"}
        or data["version"] != 1 or not isinstance(data["server_url"], str)
        or not valid_token(data["agent_token"])):
        raise AgentError("연결 설정이 올바르지 않습니다. connect로 다시 연결해 주세요.")
    return AgentConfig(normalize_server(data["server_url"]), approved_root(data["project_root"]), data["agent_token"])


def save_config(path, config):
    """원문 토큰을 출력하지 않고 연결 설정 파일에만 저장한다."""
    write_json(path, {"version": 1, "server_url": config.server_url,
        "project_root": str(config.project_root), "agent_token": config.agent_token})


@contextmanager
def runtime_lock(path):
    """운영체제 파일 잠금으로 같은 설정의 중복 실행과 동시 재연결을 막는다."""
    lock_path = path.with_suffix(".lock")
    stream = None
    locked = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if lock_path.is_symlink():
            raise OSError
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        stream = os.fdopen(descriptor, "r+b", buffering=0)
        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(b"0")
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = True
    except OSError:
        if stream is not None:
            stream.close()
        raise AgentError("같은 설정의 연결 프로그램이 이미 실행 중이거나 잠금 파일을 열 수 없습니다.") from None
    try:
        yield
    finally:
        if stream is not None:
            if locked:
                if os.name == "nt":
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            stream.close()
