"""승인 루트의 고정된 설정과 manifest에서 환경 메타데이터만 읽는다."""
import asyncio
import hashlib
import json
import os
import re
import stat
import threading
import tomllib
from pathlib import Path

from galtalka_agent.failures import JobFailure

FILES = {"pyproject.toml": "Python 프로젝트 설정", "requirements.txt": "Python 패키지 목록",
    "package.json": "JavaScript 프로젝트 설정", "Cargo.toml": "Rust 프로젝트 설정",
    "go.mod": "Go 프로젝트 설정", ".codex/config.toml": "Codex 프로젝트 설정"}
FRAMEWORKS = {"fastapi", "django", "flask", "react", "next", "vue", "express"}
REASONS = {"link": "링크 경로는 읽지 않습니다.", "large": "파일 크기 제한을 초과했습니다.",
    "unreadable": "파일을 읽을 수 없습니다.", "changed": "스캔 중 파일이 바뀌었습니다.",
    "limit": "스캔 범위 제한을 초과했습니다.", "name": "허용하지 않는 이름입니다."}


def is_link(info):
    """심볼릭 링크와 Windows junction 및 reparse point를 함께 검사한다."""
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


class ScanReader:
    def __init__(self, root, stop):
        """고정된 승인 루트와 전체 읽기 예산을 준비한다."""
        self.root, self.stop = root, stop
        self.remaining = 512 * 1024
        self.excluded = []

    def exclude(self, path, reason):
        """제외한 상대 경로와 고정 안내만 보고하고 원문 오류를 숨긴다."""
        if len(self.excluded) < 100:
            self.excluded.append({"path": path, "reason": REASONS[reason]})

    def checked(self, relative):
        """승인 루트와 각 구성 요소의 링크 여부를 읽기 전에 검사한다."""
        if self.stop.is_set():
            raise JobFailure("SCAN_CANCELLED", "환경 스캔을 중단했습니다.")
        if self.root.resolve(strict=True) != self.root or is_link(self.root.lstat()):
            raise JobFailure("ROOT_NOT_APPROVED", "승인한 프로젝트 경로가 바뀌었습니다. 다시 연결해 주세요.")
        path = self.root
        for part in Path(relative).parts:
            if part in {"..", "."} or Path(part).is_absolute():
                raise ValueError
            path = path / part
            if is_link(path.lstat()):
                self.exclude(relative, "link")
                return None
        if path.resolve(strict=True) != self.root / relative:
            self.exclude(relative, "link")
            return None
        return path

    def read(self, relative):
        """일반 파일만 제한된 크기로 읽고 열린 파일의 동일성을 확인한다."""
        try:
            path = self.checked(relative)
            if path is None:
                return None
            before = path.lstat()
            if not stat.S_ISREG(before.st_mode):
                self.exclude(relative, "unreadable")
                return None
            limit = min(128 * 1024 if relative.endswith("SKILL.md") else 64 * 1024, self.remaining)
            if before.st_size > limit:
                self.exclude(relative, "large" if self.remaining else "limit")
                return None
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            with os.fdopen(descriptor, "rb") as source:
                opened = os.fstat(source.fileno())
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino) or not stat.S_ISREG(opened.st_mode):
                    self.exclude(relative, "changed")
                    return None
                verified = self.checked(relative)
                if verified is None:
                    return None
                current = verified.lstat()
                if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                    self.exclude(relative, "changed")
                    return None
                content = source.read(limit + 1)
                after = os.fstat(source.fileno())
            if len(content) > limit:
                self.exclude(relative, "large")
                return None
            if (after.st_size, after.st_mtime_ns) != (opened.st_size, opened.st_mtime_ns):
                self.exclude(relative, "changed")
                return None
            self.remaining -= len(content)
            return content
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            self.exclude(relative, "unreadable")
            return None


def scan_request(payload, root):
    """서버 요청으로 승인 폴더나 읽기 권한을 넓히지 못하도록 검사한다."""
    if (not isinstance(payload, dict) or set(payload) != {"schema_version", "root", "scopes"}
        or payload["schema_version"] != 1 or not isinstance(payload["root"], str)
        or not isinstance(payload["scopes"], list) or not payload["scopes"]
        or any(not isinstance(scope, str) or scope not in {"config", "skills", "code"} for scope in payload["scopes"])
        or "config" not in payload["scopes"]):
        raise JobFailure("INVALID_SCAN_REQUEST", "환경 스캔의 읽기 범위를 확인해 주세요.")
    requested = payload["root"]
    if requested != ".":
        requested = os.path.normcase(os.path.normpath(os.path.expanduser(requested)))
        if requested != os.path.normcase(str(root)):
            raise JobFailure("ROOT_NOT_APPROVED", "connect에서 승인한 프로젝트만 읽을 수 있습니다.")
    try:
        if not root.is_dir() or is_link(root.lstat()) or root.resolve(strict=True) != root:
            raise OSError
    except OSError:
        raise JobFailure("ROOT_NOT_READABLE", "승인한 프로젝트를 읽을 수 없습니다.") from None


def package_metadata(path, text):
    """허용된 패키지 이름만 분류하며 버전과 URL 및 원문은 반환하지 않는다."""
    names, languages = set(), set()
    if path in {"pyproject.toml", "requirements.txt"}:
        languages.add("python")
        requirements = tomllib.loads(text).get("project", {}).get("dependencies", []) if path.endswith("toml") else text.splitlines()
        if not isinstance(requirements, list):
            raise ValueError
        for item in requirements:
            match = re.match(r"\s*([a-zA-Z0-9_.-]+)(?:\s|[<>=!~;\[]|$)", item) if isinstance(item, str) else None
            if match:
                names.add(match.group(1).lower())
    elif path == "package.json":
        data = json.loads(text)
        for key in ("dependencies", "devDependencies"):
            dependencies = data.get(key, {})
            if not isinstance(dependencies, dict):
                raise ValueError
            names.update(dependencies.keys())
        languages.add("typescript" if "typescript" in names else "javascript")
    elif path == "Cargo.toml":
        tomllib.loads(text)
        languages.add("rust")
    elif path == "go.mod":
        if not re.search(r"(?m)^module\s+\S+", text):
            raise ValueError
        languages.add("go")
    return languages, {"nextjs" if name == "next" else name for name in names & FRAMEWORKS}


def collect_scan(payload, root, stop):
    """작은 고정 파일 집합과 한 단계 스킬 폴더만 읽어 환경 정보를 만든다."""
    scan_request(payload, root)
    reader = ScanReader(root, stop)
    settings, skills, languages, frameworks, warnings = [], [], set(), set(), []
    runner, model = "unknown", "unknown"
    for path, summary in FILES.items():
        excluded_before = len(reader.excluded)
        content = reader.read(path)
        if content is None:
            if len(reader.excluded) > excluded_before:
                settings.append({"path": path, "summary": summary, "readable": False, "excluded": True})
            continue
        readable = True
        try:
            text = content.decode("utf-8-sig")
            if path == ".codex/config.toml":
                data = tomllib.loads(text)
                runner = "codex_cli"
                value = data.get("model", "unknown")
                if (isinstance(value, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:/-]{0,79}", value)
                    and not value.lower().startswith(("sk-", "sk_", "ghp_", "github_pat_", "bearer"))):
                    model = value
            else:
                found_languages, found_frameworks = package_metadata(path, text)
                languages.update(found_languages)
                frameworks.update(found_frameworks)
        except (ValueError, TypeError, AttributeError, RecursionError):
            readable = False
        settings.append({"path": path, "summary": summary, "readable": readable, "excluded": False})
    if "skills" in payload["scopes"]:
        try:
            folder = reader.checked(".agents/skills")
            entries = []
            if folder is not None:
                with os.scandir(folder) as source:
                    for entry in source:
                        if len(entries) == 50:
                            warnings.append("skill_limit")
                            break
                        entries.append(entry.name)
            for name in sorted(entries):
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name) or name.lower().startswith(("secret", "sk-", "sk_")):
                    reader.exclude(".agents/skills/[invalid-name]", "name")
                    continue
                relative = f".agents/skills/{name}/SKILL.md"
                content = reader.read(relative)
                if content is not None:
                    skills.append({"name": name, "version": "unknown", "source": relative,
                        "manifest_sha256": hashlib.sha256(content).hexdigest(), "excluded": False})
        except FileNotFoundError:
            pass
        except OSError:
            reader.exclude(".agents/skills", "unreadable")
    return {"schema_version": 1, "scan_root": str(root), "scopes": sorted(set(payload["scopes"])),
        "runner": runner, "model_id": model, "installed_skills": skills, "settings_files": settings,
        "languages": sorted(languages), "frameworks": sorted(frameworks), "excluded_paths": reader.excluded,
        "warnings": warnings}


async def scan(payload, root):
    """로컬 읽기를 별도 스레드에서 수행하고 취소 후 추가 파일 탐색을 멈춘다."""
    stop = threading.Event()
    try:
        return await asyncio.to_thread(collect_scan, payload, root, stop)
    finally:
        stop.set()
