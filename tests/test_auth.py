import importlib
import json
import re
import time
from base64 import b64decode
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from threading import Barrier, Lock

import pytest
from cryptography.fernet import Fernet
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import app.auth as auth
from app.auth import require_admin
from app.admin_cli import upsert_admin_user
from app.crypto import SecretCipher
from app.db import get_session
from app.models import AdminUser
from app.security import hash_password, verify_password

CSRF_PATTERN = re.compile(r'name="_csrf_token" value="([^"]+)"')


def _set_required_settings(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret-with-32-bytes")
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


def _session_data(client: TestClient) -> dict[str, object]:
    encoded = client.cookies.get("session")
    assert encoded is not None
    payload = encoded.split(".", 1)[0]
    padding = "=" * (-len(payload) % 4)
    return json.loads(b64decode(payload + padding))


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


def test_password_verification_supports_existing_bcrypt_hashes():
    password_hash = "$2b$12$GSm13057BqwXHr3/6MpZLeV9aTcFcj2VpuH5UBb1Q.OZE0DbRIoa."

    assert verify_password("legacy-password", password_hash)


def test_password_verification_returns_false_for_invalid_hash():
    assert not verify_password("password", "not-a-bcrypt-hash")


def test_new_password_hashes_use_bcrypt_and_can_be_verified():
    password_hash = hash_password("new-password")

    assert password_hash.startswith("$2b$")
    assert verify_password("new-password", password_hash)


def test_login_page_uses_auth_shell(auth_app):
    client = TestClient(auth_app)

    response = client.get("/login")

    assert response.status_code == 200
    assert 'class="auth-shell"' in response.text


def test_secret_cipher_round_trip():
    cipher = SecretCipher.from_key_material(Fernet.generate_key().decode())

    encrypted = cipher.encrypt("smtp-password")

    assert encrypted != "smtp-password"
    assert cipher.decrypt(encrypted) == "smtp-password"


def test_login_success_sets_admin_session(auth_app, auth_engine):
    user_id = _create_admin_user(auth_engine)
    client = TestClient(auth_app)

    response = _csrf_post(
        client,
        "/login",
        data={"username": "admin", "password": "correct-password"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    session_data = _session_data(client)
    assert session_data["admin_user_id"] == user_id
    assert isinstance(session_data["authenticated_at"], int)
    assert session_data["last_activity_at"] == session_data["authenticated_at"]
    assert session_data["admin_session_version"] == 1
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
    assert "session=null" in response.headers["set-cookie"]


def test_password_update_invalidates_existing_admin_session(auth_app, auth_engine):
    _create_admin_user(auth_engine)
    client = TestClient(auth_app)
    login_response = _csrf_post(
        client,
        "/login",
        data={"username": "admin", "password": "correct-password"},
        follow_redirects=False,
    )
    assert login_response.status_code == 303
    with Session(auth_engine) as session:
        upsert_admin_user(session, "admin", "replacement-password")

    response = client.get("/protected-test-route")

    assert response.status_code == 401
    assert "session=null" in response.headers["set-cookie"]


def test_session_cookie_max_age_matches_session_absolute_timeout(auth_app):
    client = TestClient(auth_app)

    response = client.get("/login")

    assert "Max-Age=28800" in response.headers["set-cookie"]


def test_validate_admin_session_refreshes_last_activity_at():
    user = AdminUser(id=7, username="admin", password_hash="hash", session_version=3)
    authenticated_at = datetime(2026, 1, 1, tzinfo=UTC)
    request = SimpleNamespace(
        session={
            "admin_user_id": 7,
            "authenticated_at": int(authenticated_at.timestamp()),
            "last_activity_at": int(authenticated_at.timestamp()),
            "admin_session_version": 3,
        }
    )
    now = authenticated_at + timedelta(minutes=10)

    assert auth.validate_admin_session(
        request,
        user,
        max_age_seconds=28_800,
        idle_timeout_seconds=1_800,
        now=now,
    )
    assert request.session["last_activity_at"] == int(now.timestamp())


@pytest.mark.parametrize(
    "session_update, now_offset",
    [
        ({"admin_user_id": "7"}, 0),
        ({"authenticated_at": "invalid"}, 0),
        ({"last_activity_at": True}, 0),
        ({"admin_session_version": True}, 0),
        ({"authenticated_at": 1_001, "last_activity_at": 1_001}, 0),
        ({"last_activity_at": 999}, 0),
        ({}, 28_800),
        ({}, 1_800),
        ({"admin_user_id": 8}, 0),
        ({"admin_session_version": 4}, 0),
    ],
    ids=[
        "invalid-user-id-type",
        "invalid-time-type",
        "boolean-time-type",
        "boolean-session-version-type",
        "future-time",
        "backward-activity-time",
        "absolute-timeout",
        "idle-timeout",
        "different-user",
        "different-session-version",
    ],
)
def test_validate_admin_session_clears_invalid_or_expired_session(session_update, now_offset):
    user = AdminUser(id=7, username="admin", password_hash="hash", session_version=3)
    request = SimpleNamespace(
        session={
            "admin_user_id": 7,
            "authenticated_at": 1_000,
            "last_activity_at": 1_000,
            "admin_session_version": 3,
        }
    )
    request.session.update(session_update)

    assert not auth.validate_admin_session(
        request,
        user,
        max_age_seconds=28_800,
        idle_timeout_seconds=1_800,
        now=datetime.fromtimestamp(1_000 + now_offset, UTC),
    )
    assert request.session == {}


def test_validate_admin_session_clears_missing_session_field():
    user = AdminUser(id=7, username="admin", password_hash="hash", session_version=3)
    request = SimpleNamespace(
        session={
            "admin_user_id": 7,
            "authenticated_at": 1_000,
            "last_activity_at": 1_000,
        }
    )

    assert not auth.validate_admin_session(
        request,
        user,
        max_age_seconds=28_800,
        idle_timeout_seconds=1_800,
        now=datetime.fromtimestamp(1_001, UTC),
    )
    assert request.session == {}


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


def test_concurrent_logins_for_one_key_stop_verifying_after_lockout(
    auth_app,
    auth_engine,
    monkeypatch,
):
    _create_admin_user(auth_engine)
    clients = [TestClient(auth_app) for _ in range(10)]
    tokens = [_csrf_token(client) for client in clients]
    start = Barrier(len(clients))
    call_lock = Lock()
    verification_calls = 0

    def reject_password(*_args):
        nonlocal verification_calls
        with call_lock:
            verification_calls += 1
        time.sleep(0.05)
        return False

    monkeypatch.setattr("app.auth.verify_password", reject_password)

    def attempt_login(item):
        client, token = item
        start.wait()
        return client.post(
            "/login",
            data={"username": "admin", "password": "wrong", "_csrf_token": token},
        ).status_code

    try:
        with ThreadPoolExecutor(max_workers=len(clients)) as executor:
            statuses = list(executor.map(attempt_login, zip(clients, tokens, strict=True)))
    finally:
        for client in clients:
            client.close()

    assert verification_calls == 5
    assert sorted(statuses) == [400] * 4 + [429] * 6


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


def test_limiter_capacity_overflow_skips_password_verification(auth_app, monkeypatch):
    from app.web_security import LoginRateLimiter

    client_host = "10.0.0.8"
    auth_app.state.login_rate_limiter = LoginRateLimiter(
        max_failures=5,
        failure_window_seconds=900,
        lockout_seconds=900,
        max_states=1,
        cleanup_interval_seconds=30,
    )
    auth_app.state.login_rate_limiter.record_failure(client_host, "occupied")
    monkeypatch.setattr("app.auth.verify_password", lambda *_args: pytest.fail("must not verify"))
    client = TestClient(auth_app, client=(client_host, 50000))

    response = _csrf_post(
        client,
        "/login",
        data={"username": "new-key", "password": "password"},
    )

    assert response.status_code == 429
    assert response.headers["retry-after"] == "30"


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


def test_unknown_user_verifies_password_against_fixed_dummy_hash(
    auth_app,
    monkeypatch,
):
    calls = []

    def reject_password(password, password_hash):
        calls.append((password, password_hash))
        return False

    monkeypatch.setattr("app.auth.verify_password", reject_password)
    client = TestClient(auth_app)

    responses = [
        _csrf_post(client, "/login", data={"username": "missing", "password": "submitted"})
        for _ in range(5)
    ]

    assert len(calls) == 5
    auth_module = importlib.import_module("app.auth")
    assert calls == [("submitted", auth_module.DUMMY_PASSWORD_HASH)] * 5
    assert auth_module.DUMMY_PASSWORD_HASH.startswith("$2b$12$")
    assert [response.status_code for response in responses] == [400, 400, 400, 400, 429]


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
