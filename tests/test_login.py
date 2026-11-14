import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.main import create_app
from backend.models import UserAccount

LOCAL = {"X-Local-Token": "test-local-token"}
PROFILE = {"role": "developer", "primary_language": "python", "task_types": ["code_review"]}
ALICE = {"username": "alice", "password": "correct-horse-1"}


@pytest.fixture
def client(database):
    with TestClient(create_app(database_url=database[0].url.render_as_string(hide_password=False), local_token="test-local-token")) as value:
        yield value


def bearer(response):
    return {"Authorization": "Bearer " + response.json()["token"]}


def test_signup_login_and_data_belongs_to_account(client, database):
    """가입하면 바로 로그인되고, 다른 기기에서 로그인해도 같은 계정 데이터를 쓴다."""
    signup = client.post("/api/auth/signup", json=ALICE)
    assert signup.status_code == 201 and signup.json()["username"] == "alice"
    a = bearer(signup)
    assert client.put("/api/profile", headers=a, json=PROFILE).status_code == 200
    # 다른 기기에서 로그인
    login = client.post("/api/auth/login", json=ALICE)
    assert login.status_code == 200 and login.json()["user_id"] == signup.json()["user_id"]
    assert client.get("/api/profile", headers=bearer(login)).json()["profile_id"] == signup.json()["user_id"]
    assert client.get("/api/auth/me", headers=bearer(login)).json()["username"] == "alice"
    # 다른 사용자, 로컬 토큰 사용자와는 데이터가 따로다
    bob = bearer(client.post("/api/auth/signup", json={"username": "bob", "password": "another-pass-2"}))
    assert client.get("/api/profile", headers=bob).status_code == 404
    assert client.get("/api/profile", headers=LOCAL).status_code == 404
    # 비밀번호 원문은 저장하지 않는다
    with database[1]() as session:
        stored = session.scalar(select(UserAccount.password_hash).where(UserAccount.username == "alice"))
    assert ALICE["password"] not in stored and stored.startswith("scrypt$")


def test_signup_and_login_errors(client):
    assert client.post("/api/auth/signup", json=ALICE).status_code == 201
    assert client.post("/api/auth/signup", json=ALICE).json()["error"]["code"] == "username_taken"
    assert client.post("/api/auth/signup", json={"username": "Bad Name", "password": "long-enough"}).status_code == 422
    assert client.post("/api/auth/signup", json={"username": "short_pw", "password": "123"}).status_code == 422
    wrong = client.post("/api/auth/login", json={"username": "alice", "password": "wrong-password"})
    assert wrong.status_code == 401 and wrong.json()["error"]["code"] == "invalid_credentials"
    nobody = client.post("/api/auth/login", json={"username": "nobody", "password": "whatever-1"})
    assert nobody.status_code == 401 and nobody.json()["error"]["code"] == "invalid_credentials"


def test_login_attempts_are_limited(client):
    client.post("/api/auth/signup", json=ALICE)
    for _ in range(10):
        client.post("/api/auth/login", json={"username": "alice", "password": "wrong-password"})
    blocked = client.post("/api/auth/login", json=ALICE)
    assert blocked.status_code == 429 and blocked.json()["error"]["code"] == "login_rate_limited"


def test_logout_and_bad_session(client):
    a = bearer(client.post("/api/auth/signup", json=ALICE))
    assert client.post("/api/auth/logout", headers=a).status_code == 204
    assert client.get("/api/auth/me", headers=a).status_code == 401
    assert client.get("/api/profile", headers={"Authorization": "Bearer nope"}).json()["error"]["code"] == "invalid_session"
    assert client.get("/api/auth/me", headers=LOCAL).json()["via"] == "local"
