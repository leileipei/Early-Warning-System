from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app.db import create_db_engine
from app.models import utc_now
from app.worker_health import record_worker_start, record_worker_sync

VALID_FERNET_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


@pytest.fixture()
def client(monkeypatch, engine):
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret-with-32-bytes")
    monkeypatch.setenv("SECRET_KEY", VALID_FERNET_KEY)

    from app.settings import get_settings

    get_settings.cache_clear()
    import app.main as main

    monkeypatch.setattr(main, "get_engine", lambda: engine, raising=False)
    try:
        yield TestClient(main.create_app())
    finally:
        get_settings.cache_clear()


def test_health_is_fixed_liveness_check(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_is_unavailable_without_worker_heartbeat(client):
    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "components": {"database": "ready", "worker": "missing"},
    }


def test_readiness_is_available_after_fresh_successful_worker_sync(client, session):
    record_worker_start(session, "worker-a", now=utc_now())
    record_worker_sync(session, "worker-a", ok=True, now=utc_now())

    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "components": {"database": "ready", "worker": "ready"},
    }


def test_readiness_is_unavailable_for_stale_worker(client, session):
    record_worker_start(session, "worker-a", now=utc_now() - timedelta(seconds=61))

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["components"] == {"database": "ready", "worker": "stale"}


def test_readiness_is_unavailable_after_failed_worker_sync(client, session):
    record_worker_start(session, "worker-a", now=utc_now())
    record_worker_sync(
        session,
        "worker-a",
        ok=False,
        error="RuntimeError: worker synchronization failed",
        now=utc_now(),
    )

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["components"] == {"database": "ready", "worker": "sync_failed"}


def test_readiness_is_unavailable_when_required_tables_are_missing(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret-with-32-bytes")
    monkeypatch.setenv("SECRET_KEY", VALID_FERNET_KEY)
    from app.settings import get_settings

    get_settings.cache_clear()
    import app.main as main

    monkeypatch.setattr(
        main, "get_engine", lambda: create_db_engine("sqlite:///:memory:"), raising=False
    )
    try:
        response = TestClient(main.create_app()).get("/health/ready")
    finally:
        get_settings.cache_clear()

    assert response.status_code == 503
    assert response.json()["components"] == {
        "database": "schema_incomplete",
        "worker": "unknown",
    }


def test_readiness_hides_database_exception_details(client, monkeypatch, engine):
    database_url = str(engine.url)

    def fail_connection():
        raise RuntimeError(f"database unavailable at {database_url} /private/secret")

    import app.main as main

    monkeypatch.setattr(main, "get_engine", lambda: create_engine("sqlite://", creator=fail_connection))

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "components": {"database": "unavailable", "worker": "unknown"},
    }
    assert database_url not in response.text
    assert "/private/secret" not in response.text
    assert "RuntimeError" not in response.text


def test_readiness_hides_engine_creation_exception_details(monkeypatch):
    database_url = "postgresql://operator:super-secret@db.internal/alerts"
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret-with-32-bytes")
    monkeypatch.setenv("SECRET_KEY", VALID_FERNET_KEY)
    from app.settings import get_settings

    get_settings.cache_clear()
    import app.main as main

    def fail_get_engine():
        raise RuntimeError(f"unable to create engine for {database_url} /private/secret")

    monkeypatch.setattr(main, "get_engine", fail_get_engine)
    try:
        response = TestClient(main.create_app(), raise_server_exceptions=False).get("/health/ready")
    finally:
        get_settings.cache_clear()

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "components": {"database": "unavailable", "worker": "unknown"},
    }
    assert database_url not in response.text
    assert "/private/secret" not in response.text
    assert "RuntimeError" not in response.text
