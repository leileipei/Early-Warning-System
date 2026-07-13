import importlib
import re

import pytest
from cryptography.fernet import Fernet
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.auth import require_admin
from app.crypto import SecretCipher
from app.db import get_session
from app.models import AdminUser
from app.security import hash_password, verify_password

CSRF_PATTERN = re.compile(r'name="_csrf_token" value="([^"]+)"')


def _set_required_settings(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode())


def _load_create_app():
    from app.settings import get_settings

    get_settings.cache_clear()
    main = importlib.import_module("app.main")
    return main.create_app, get_settings


@pytest.fixture()
def auth_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture()
def auth_app(auth_engine, monkeypatch):
    _set_required_settings(monkeypatch)
    create_app, get_settings = _load_create_app()
    app = create_app()

    def override_get_session():
        with Session(auth_engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session

    @app.get("/protected-test-route")
    def protected_test_route(admin: AdminUser = Depends(require_admin)):
        return {"username": admin.username}

    try:
        yield app
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def _create_admin_user(engine, username: str = "admin", password: str = "correct-password") -> int:
    with Session(engine) as session:
        user = AdminUser(username=username, password_hash=hash_password(password))
        session.add(user)
        session.commit()
        session.refresh(user)
        assert user.id is not None
        return user.id


def _delete_admin_user(engine, user_id: int) -> None:
    with Session(engine) as session:
        user = session.get(AdminUser, user_id)
        assert user is not None
        session.delete(user)
        session.commit()


def _csrf_token(client: TestClient) -> str:
    response = client.get("/login")
    match = CSRF_PATTERN.search(response.text)
    assert match is not None
    return match.group(1)


def _csrf_post(client: TestClient, path: str, *, data=None, **kwargs):
    payload = dict(data or {})
    payload["_csrf_token"] = _csrf_token(client)
    return client.post(path, data=payload, **kwargs)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def make_test_limiter(clock):
    from app.web_security import LoginRateLimiter

    return LoginRateLimiter(
        max_failures=5,
        failure_window_seconds=900,
        lockout_seconds=900,
        clock=clock,
    )


def test_password_hash_round_trip():
    password_hash = hash_password("CorrectHorseBatteryStaple")

    assert password_hash != "CorrectHorseBatteryStaple"
    assert verify_password("CorrectHorseBatteryStaple", password_hash)
    assert not verify_password("wrong", password_hash)


def test_secret_cipher_round_trip():
    cipher = SecretCipher.from_key_material(Fernet.generate_key().decode())

    encrypted = cipher.encrypt("smtp-password")

    assert encrypted != "smtp-password"
    assert cipher.decrypt(encrypted) == "smtp-password"


def test_login_success_sets_admin_session(auth_app, auth_engine):
    _create_admin_user(auth_engine)
    client = TestClient(auth_app)

    response = _csrf_post(
        client,
        "/login",
        data={"username": "admin", "password": "correct-password"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    protected_response = client.get("/protected-test-route")
    assert protected_response.status_code == 200
    assert protected_response.json() == {"username": "admin"}


def test_login_with_wrong_password_returns_400(auth_app, auth_engine):
    _create_admin_user(auth_engine)
    client = TestClient(auth_app)

    response = _csrf_post(
        client,
        "/login",
        data={"username": "admin", "password": "wrong-password"},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "用户名或密码错误"}


def test_logout_clears_admin_session(auth_app, auth_engine):
    _create_admin_user(auth_engine)
    client = TestClient(auth_app)
    login_response = _csrf_post(
        client,
        "/login",
        data={"username": "admin", "password": "correct-password"},
        follow_redirects=False,
    )
    assert login_response.status_code == 303

    logout_response = _csrf_post(client, "/logout", follow_redirects=False)

    assert logout_response.status_code == 303
    assert logout_response.headers["location"] == "/login"
    protected_response = client.get("/protected-test-route")
    assert protected_response.status_code == 401


def test_require_admin_rejects_session_for_missing_user(auth_app, auth_engine):
    user_id = _create_admin_user(auth_engine)
    client = TestClient(auth_app)
    login_response = _csrf_post(
        client,
        "/login",
        data={"username": "admin", "password": "correct-password"},
        follow_redirects=False,
    )
    assert login_response.status_code == 303
    _delete_admin_user(auth_engine, user_id)

    response = client.get("/protected-test-route")

    assert response.status_code == 401


def test_login_rejects_missing_csrf_token(auth_app):
    client = TestClient(auth_app)

    response = client.post("/login", data={"username": "admin", "password": "password"})

    assert response.status_code == 403
    assert response.json() == {"detail": "请求安全校验失败"}


def test_login_rotates_csrf_token(auth_app, auth_engine):
    _create_admin_user(auth_engine)
    client = TestClient(auth_app)
    old_token = _csrf_token(client)

    response = client.post(
        "/login",
        data={
            "username": "admin",
            "password": "correct-password",
            "_csrf_token": old_token,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert client.post("/logout", data={"_csrf_token": old_token}).status_code == 403


def test_login_locks_on_fifth_failure(auth_app, auth_engine):
    from app.web_security import LoginRateLimiter

    _create_admin_user(auth_engine)
    clock = FakeClock()
    auth_app.state.login_rate_limiter = LoginRateLimiter(
        max_failures=5,
        failure_window_seconds=900,
        lockout_seconds=900,
        clock=clock,
    )
    client = TestClient(auth_app)

    responses = [
        _csrf_post(client, "/login", data={"username": "admin", "password": "wrong"})
        for _ in range(5)
    ]

    assert [response.status_code for response in responses] == [400, 400, 400, 400, 429]
    assert responses[-1].headers["retry-after"] == "900"
    assert responses[-1].json() == {"detail": "登录尝试过多，请稍后重试"}


def test_locked_login_skips_password_verification(auth_app, auth_engine, monkeypatch):
    _create_admin_user(auth_engine)
    client = TestClient(auth_app)
    for _ in range(5):
        _csrf_post(client, "/login", data={"username": "admin", "password": "wrong"})

    monkeypatch.setattr("app.auth.verify_password", lambda *args: pytest.fail("must not verify"))

    response = _csrf_post(
        client,
        "/login",
        data={"username": "admin", "password": "correct-password"},
    )

    assert response.status_code == 429


def test_login_lock_expires(auth_app, auth_engine):
    _create_admin_user(auth_engine)
    clock = FakeClock()
    auth_app.state.login_rate_limiter = make_test_limiter(clock)
    client = TestClient(auth_app)
    for _ in range(5):
        _csrf_post(client, "/login", data={"username": "admin", "password": "wrong"})

    clock.advance(901)
    response = _csrf_post(
        client,
        "/login",
        data={"username": "admin", "password": "correct-password"},
        follow_redirects=False,
    )

    assert response.status_code == 303


def test_unknown_user_and_wrong_password_share_limit_behavior(auth_app, auth_engine):
    _create_admin_user(auth_engine)
    unknown_client = TestClient(auth_app, client=("10.0.0.1", 50000))
    wrong_client = TestClient(auth_app, client=("10.0.0.2", 50000))

    unknown = [
        _csrf_post(unknown_client, "/login", data={"username": "missing", "password": "wrong"})
        for _ in range(5)
    ]
    wrong = [
        _csrf_post(wrong_client, "/login", data={"username": "admin", "password": "wrong"})
        for _ in range(5)
    ]

    assert [response.status_code for response in unknown] == [400, 400, 400, 400, 429]
    assert [response.status_code for response in wrong] == [400, 400, 400, 400, 429]


def test_successful_login_clears_previous_failures(auth_app, auth_engine):
    _create_admin_user(auth_engine)
    client = TestClient(auth_app)
    for _ in range(4):
        response = _csrf_post(
            client,
            "/login",
            data={"username": "admin", "password": "wrong"},
        )
        assert response.status_code == 400

    login_response = _csrf_post(
        client,
        "/login",
        data={"username": "admin", "password": "correct-password"},
        follow_redirects=False,
    )
    assert login_response.status_code == 303

    logout_response = _csrf_post(client, "/logout", follow_redirects=False)
    assert logout_response.status_code == 303

    responses = [
        _csrf_post(client, "/login", data={"username": "admin", "password": "wrong"})
        for _ in range(4)
    ]
    assert [response.status_code for response in responses] == [400, 400, 400, 400]
