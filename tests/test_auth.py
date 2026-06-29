import importlib

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

    response = client.post(
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

    response = client.post(
        "/login",
        data={"username": "admin", "password": "wrong-password"},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "用户名或密码错误"}


def test_logout_clears_admin_session(auth_app, auth_engine):
    _create_admin_user(auth_engine)
    client = TestClient(auth_app)
    login_response = client.post(
        "/login",
        data={"username": "admin", "password": "correct-password"},
        follow_redirects=False,
    )
    assert login_response.status_code == 303

    logout_response = client.post("/logout", follow_redirects=False)

    assert logout_response.status_code == 303
    assert logout_response.headers["location"] == "/login"
    protected_response = client.get("/protected-test-route")
    assert protected_response.status_code == 401


def test_require_admin_rejects_session_for_missing_user(auth_app, auth_engine):
    user_id = _create_admin_user(auth_engine)
    client = TestClient(auth_app)
    login_response = client.post(
        "/login",
        data={"username": "admin", "password": "correct-password"},
        follow_redirects=False,
    )
    assert login_response.status_code == 303
    _delete_admin_user(auth_engine, user_id)

    response = client.get("/protected-test-route")

    assert response.status_code == 401
