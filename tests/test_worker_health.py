from datetime import UTC, datetime

import pytest

from app.models import WorkerHeartbeat
from app.worker_health import record_worker_start, record_worker_sync, summarize_worker_error


def test_record_worker_start_creates_singleton_heartbeat(session):
    started_at = datetime(2026, 7, 17, 9, 30)

    record_worker_start(session, "worker-a", now=started_at)

    heartbeat = session.get(WorkerHeartbeat, 1)
    assert heartbeat is not None
    assert heartbeat.id == 1
    assert heartbeat.worker_id == "worker-a"
    assert heartbeat.started_at == started_at
    assert heartbeat.last_seen_at == started_at
    assert heartbeat.last_sync_ok is True
    assert heartbeat.last_error == ""


def test_record_worker_sync_success_replaces_previous_failure(session):
    started_at = datetime(2026, 7, 17, 9, 30)
    failed_at = datetime(2026, 7, 17, 9, 31)
    recovered_at = datetime(2026, 7, 17, 9, 32)
    record_worker_start(session, "worker-a", now=started_at)
    record_worker_sync(session, "worker-a", ok=False, error="RuntimeError: worker sync failed", now=failed_at)

    record_worker_sync(session, "worker-a", ok=True, now=recovered_at)

    heartbeat = session.get(WorkerHeartbeat, 1)
    assert heartbeat is not None
    assert heartbeat.started_at == started_at
    assert heartbeat.last_seen_at == recovered_at
    assert heartbeat.last_sync_ok is True
    assert heartbeat.last_error == ""


def test_record_worker_sync_persists_sanitized_failure_summary(session):
    secret = "postgresql://operator:super-secret@db.internal/alerts"
    summary = summarize_worker_error(RuntimeError(f"unable to connect: {secret}"))

    record_worker_sync(
        session,
        "worker-a",
        ok=False,
        error=summary,
        now=datetime(2026, 7, 17, 9, 31),
    )

    heartbeat = session.get(WorkerHeartbeat, 1)
    assert heartbeat is not None
    assert heartbeat.id == 1
    assert heartbeat.last_sync_ok is False
    assert heartbeat.last_error == "Worker 同步失败，请检查数据库连接和调度配置"
    assert secret not in heartbeat.last_error
    assert len(heartbeat.last_error) <= 300


def test_summarize_worker_error_is_bounded_and_does_not_include_exception_message():
    secret = "x" * 1_000

    summary = summarize_worker_error(ValueError(secret))

    assert summary == "Worker 同步失败，请检查数据库连接和调度配置"
    assert secret not in summary
    assert len(summary) <= 300


def test_worker_health_rolls_back_after_commit_failure(session, monkeypatch):
    def fail_commit():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(session, "commit", fail_commit)

    with pytest.raises(RuntimeError, match="database unavailable"):
        record_worker_start(session, "worker-a")

    assert session.get(WorkerHeartbeat, 1) is None


def test_record_worker_start_converts_aware_timestamp_to_naive_utc(session):
    aware_time = datetime(2026, 7, 17, 17, 30, tzinfo=UTC)

    record_worker_start(session, "worker-a", now=aware_time)

    heartbeat = session.get(WorkerHeartbeat, 1)
    assert heartbeat is not None
    assert heartbeat.started_at == datetime(2026, 7, 17, 17, 30)
    assert heartbeat.started_at.tzinfo is None
