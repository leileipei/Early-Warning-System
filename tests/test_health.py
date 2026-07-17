from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlmodel import Session

from app.db import create_db_engine
from app.models import AlertRule, SendMode, SqlDataSource, WorkerHeartbeat, utc_now
from app.scheduler import RuleScheduleSynchronizer, build_scheduler
from app.worker import WORKER_SYNC_FAILURE, sync_rules_once
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


def test_partial_rule_schedule_failure_records_failed_heartbeat_and_readiness(
    client, engine
):
    with Session(engine) as session:
        data_source = SqlDataSource(
            name="prod",
            host="db.example.com",
            database="erp",
            username="readonly",
            encrypted_password="encrypted",
        )
        session.add(data_source)
        session.commit()
        session.refresh(data_source)
        for rule_id in (1, 2):
            session.add(
                AlertRule(
                    id=rule_id,
                    name=f"rule-{rule_id}",
                    data_source_id=data_source.id,
                    sql_text="select 1",
                    cron_expression="0 9 * * *",
                    recipients="ops@example.com",
                    subject_template="预警",
                    body_template="{{table}}",
                    send_mode=SendMode.SUMMARY,
                )
            )
        session.commit()

    scheduler = build_scheduler([], execute_rule=lambda rule_id: None)
    real_add_job = scheduler.add_job

    def fail_one_rule(*args, **kwargs):
        if kwargs["id"] == "rule-1":
            raise RuntimeError("password=must-not-reach-heartbeat")
        return real_add_job(*args, **kwargs)

    scheduler.add_job = fail_one_rule
    synchronizer = RuleScheduleSynchronizer(
        scheduler, execute_rule=lambda rule_id: None
    )

    result = sync_rules_once(
        synchronizer, session_factory=lambda: Session(engine)
    )
    with Session(engine) as session:
        record_worker_start(session, "worker-a", now=utc_now())
        record_worker_sync(
            session,
            "worker-a",
            ok=result.ok,
            error=result.error,
            now=utc_now(),
        )
        heartbeat = session.get(WorkerHeartbeat, 1)

    response = client.get("/health/ready")

    assert result.ok is False
    assert result.error == WORKER_SYNC_FAILURE
    assert heartbeat is not None
    assert heartbeat.last_sync_ok is False
    assert heartbeat.last_error == WORKER_SYNC_FAILURE
    assert "must-not-reach-heartbeat" not in heartbeat.last_error
    assert scheduler.get_job("rule-2") is not None
    assert response.status_code == 503
    assert response.json()["components"] == {
        "database": "ready",
        "worker": "sync_failed",
    }


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
