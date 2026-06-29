import importlib

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError


def _set_required_settings(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")


def _load_create_app():
    from app.settings import get_settings

    get_settings.cache_clear()
    main = importlib.import_module("app.main")
    return main.create_app, get_settings


def test_health_endpoint_returns_ok(monkeypatch):
    _set_required_settings(monkeypatch)
    create_app, get_settings = _load_create_app()
    try:
        client = TestClient(create_app())

        response = client.get("/health")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
    finally:
        get_settings.cache_clear()


def test_app_title_uses_configured_app_name(monkeypatch):
    _set_required_settings(monkeypatch)
    monkeypatch.setenv("APP_NAME", "测试预警系统")
    create_app, get_settings = _load_create_app()

    get_settings.cache_clear()
    try:
        app = create_app()

        assert app.title == "测试预警系统"
    finally:
        get_settings.cache_clear()


def test_settings_require_secret_values(tmp_path, monkeypatch):
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.chdir(tmp_path)

    from app.settings import Settings

    with pytest.raises(ValidationError) as exc_info:
        Settings()

    error_fields = {error["loc"][0] for error in exc_info.value.errors()}
    assert {"session_secret", "secret_key"} <= error_fields


@pytest.mark.parametrize(
    ("field_name", "settings_values"),
    [
        (
            "session_secret",
            {
                "session_secret": "REPLACE_ME_WITH_RANDOM_SESSION_SECRET",
                "secret_key": "valid-secret-key",
            },
        ),
        (
            "secret_key",
            {
                "session_secret": "valid-session-secret",
                "secret_key": "REPLACE_ME_WITH_32_BYTE_URL_SAFE_FERNET_KEY",
            },
        ),
    ],
)
def test_settings_reject_replace_me_secret_placeholders(field_name, settings_values):
    from app.settings import Settings

    with pytest.raises(ValidationError) as exc_info:
        Settings(**settings_values)

    error_fields = {error["loc"][0] for error in exc_info.value.errors()}
    assert field_name in error_fields


def test_settings_reads_dotenv_file(tmp_path, monkeypatch):
    monkeypatch.delenv("APP_NAME", raising=False)
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    tmp_path.joinpath(".env").write_text(
        "\n".join(
            [
                "APP_NAME=Env 文件预警系统",
                "SESSION_SECRET=dotenv-session-secret",
                "SECRET_KEY=dotenv-secret-key",
            ]
        ),
        encoding="utf-8",
    )

    from app.settings import get_settings

    get_settings.cache_clear()
    try:
        settings = get_settings()

        assert settings.app_name == "Env 文件预警系统"
        assert settings.session_secret == "dotenv-session-secret"
        assert settings.secret_key == "dotenv-secret-key"
    finally:
        get_settings.cache_clear()
