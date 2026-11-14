"""로그인 원문 보존과 계정 격리 및 공개 인증의 자원 제한을 검증한다."""
import base64
import hashlib
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

import backend.login as login
from backend.auth import token_hash
from backend.main import create_app
from backend.models import UserAccount, UserSession

OPERATOR = {"X-Local-Token": "test-operator-token"}


def app(database):
    """실제 테스트 PostgreSQL을 사용하는 인증 앱을 만든다."""
    return create_app(database_url=database[2].attributes["database_url"], local_token="test-operator-token")


def test_password_spaces_token_cache_and_session_expiry(database):
    """공백을 포함한 비밀번호와 세션 만료를 원문 그대로 처리한다."""
    with TestClient(app(database)) as api:
        issued = api.post("/api/auth/signup", json={"username": "space-user", "password": " padded-password "})
        assert issued.status_code == 201
        assert issued.headers["Cache-Control"] == "no-store"
        assert datetime.fromisoformat(issued.json()["expires_at"]) > datetime.now(timezone.utc)
        assert api.post("/api/auth/login", json={"username": "space-user", "password": "padded-password"}).status_code == 401
        assert api.post("/api/auth/login", json={"username": "space-user", "password": " padded-password "}).status_code == 200
        token = issued.json()["token"]
        with database[1].begin() as session:
            stored = session.get(UserSession, token_hash(token))
            assert stored.token_hash != token
            stored.expires_at = "2000-01-01T00:00:00.000000Z"
        assert api.get("/api/auth/me", headers={"Authorization": "Bearer " + token}).status_code == 401


def test_normal_accounts_cannot_operate_global_catalog(database):
    """가입한 일반 사용자가 전역 수집과 예약 및 운영 통계를 제어하지 못한다."""
    with TestClient(app(database)) as api:
        token = api.post("/api/auth/signup", json={"username": "member", "password": "member-password"}).json()["token"]
        user = {"Authorization": "Bearer " + token}
        assert api.get("/api/auth/me", headers=user).status_code == 200
        assert api.get("/api/catalog/refresh", headers=user).status_code == 403
        assert api.put("/api/catalog/refresh/schedule", headers=user, json={"enabled": False}).status_code == 403
        assert api.post("/api/catalog/sync", headers={**user, "Idempotency-Key": "unauthorized"},
                        json={"discovery_queries": ["review"]}).status_code == 403
        assert api.put("/api/catalog/refresh/schedule", headers=OPERATOR, json={"enabled": False}).status_code == 200


def test_signup_limit_survives_app_restart(database):
    """가입 요청도 DB에 제한을 남겨 재시작 뒤 계속 우회할 수 없다."""
    with TestClient(app(database)) as api:
        for index in range(5):
            assert api.post("/api/auth/signup", json={"username": f"member-{index}", "password": "safe-test-password"}).status_code == 201
    with TestClient(app(database)) as api:
        limited = api.post("/api/auth/signup", json={"username": "member-next", "password": "safe-test-password"})
        assert limited.status_code == 429 and limited.headers["Retry-After"]


def test_rotating_usernames_do_not_bypass_ip_login_limit(database, monkeypatch):
    """아이디를 계속 바꿔도 같은 접속 주소의 해시 시도량을 제한한다."""
    checked = []

    def verify(password, stored):
        """이 테스트에서는 해시 비용 대신 검증에 진입한 요청 수만 기록한다."""
        checked.append(1)
        return False

    monkeypatch.setattr(login, "verify_password", verify)
    with TestClient(app(database)) as api:
        for index in range(30):
            assert api.post("/api/auth/login", json={"username": f"unknown-{index}", "password": "safe-test-password"}).status_code == 401
        assert api.post("/api/auth/login", json={"username": "unknown-next", "password": "safe-test-password"}).status_code == 429
    assert len(checked) == 30


def test_legacy_hash_is_upgraded_after_successful_login(database):
    """기존 비용의 해시는 성공 로그인 시 새 형식으로 갱신한다."""
    salt = b"s" * 16
    digest = hashlib.scrypt(b"legacy-password", salt=salt, n=2**14, r=8, p=1, dklen=32)
    old = "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()
    with database[1].begin() as session:
        session.add(UserAccount(id="legacy-user", username="legacy", password_hash=old))
    with TestClient(app(database)) as api:
        assert api.post("/api/auth/login", json={"username": "legacy", "password": "legacy-password"}).status_code == 200
    with database[1]() as session:
        stored = session.scalar(select(UserAccount.password_hash).where(UserAccount.id == "legacy-user"))
        assert stored.startswith("scrypt$16384$8$5$") and login.verify_password("legacy-password", stored)


def test_hash_concurrency_is_bounded():
    """동시에 사용할 해시 슬롯이 없으면 추가 메모리를 할당하지 않고 거부한다."""
    assert login.HASH_SLOTS.acquire(blocking=False)
    assert login.HASH_SLOTS.acquire(blocking=False)
    try:
        with pytest.raises(HTTPException) as error:
            login.hash_password("safe-test-password")
        assert error.value.status_code == 503
    finally:
        login.HASH_SLOTS.release()
        login.HASH_SLOTS.release()


def test_server_input_manifest_is_owned_by_profile(tmp_path):
    """파일 입력 id를 알아도 다른 계정의 원본을 비교에 사용할 수 없다."""
    from backend.input_store import register_input, load_input
    root = tmp_path / "project"
    root.mkdir()
    (root / "example.py").write_text("print('test')", encoding="utf-8")
    data_dir = tmp_path / "inputs"
    manifest = register_input(root, ["example.py"], data_dir, "code_review", profile_id="alice")
    assert load_input(manifest["input_id"], data_dir, profile_id="alice") == manifest
    with pytest.raises(ValueError, match="not owned"):
        load_input(manifest["input_id"], data_dir, profile_id="bob")
    with pytest.raises(ValueError, match="not owned"):
        load_input(manifest["input_id"], data_dir)
