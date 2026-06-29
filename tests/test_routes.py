from fastapi.testclient import TestClient

from app.main import create_app


def test_health_endpoint_returns_ok():
    client = TestClient(create_app())

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_app_title_uses_configured_app_name(monkeypatch):
    monkeypatch.setenv("APP_NAME", "测试预警系统")

    from app.settings import get_settings

    get_settings.cache_clear()
    try:
        app = create_app()

        assert app.title == "测试预警系统"
    finally:
        get_settings.cache_clear()
