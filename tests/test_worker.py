from types import SimpleNamespace
from unittest.mock import Mock

from sqlmodel import Session

from app.models import AlertRule, SendMode, SqlDataSource, WorkerHeartbeat, utc_now
import app.worker as worker


def _persist_rule(session, data_source_id, *, name, archived_at=None):
    rule = AlertRule(
        name=name,
        data_source_id=data_source_id,
        sql_text="select id from orders",
        cron_expression="0 9 * * *",
        recipients="ops@example.com",
        subject_template="预警",
        body_template="{{table}}",
        send_mode=SendMode.SUMMARY,
        archived_at=archived_at,
    )
    session.add(rule)
    session.commit()
    session.refresh(rule)
    return rule


def test_sync_rules_once_passes_only_active_rules_to_synchronizer(engine):
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
        active_rule = _persist_rule(session, data_source.id, name="active")
        active_rule_id = active_rule.id
        _persist_rule(session, data_source.id, name="archived", archived_at=utc_now())

    synchronizer = Mock()

    result = worker.sync_rules_once(synchronizer, session_factory=lambda: Session(engine))

    assert result == worker.RuleSyncResult(ok=True)
    synced_rules = synchronizer.sync.call_args.args[0]
    assert [rule.id for rule in synced_rules] == [active_rule_id]


def test_sync_rules_once_returns_failure_when_rule_reading_fails():
    session_factory = Mock(side_effect=RuntimeError("database unavailable"))

    result = worker.sync_rules_once(Mock(), session_factory=session_factory)

    assert result == worker.RuleSyncResult(
        ok=False, error="RuntimeError: worker synchronization failed"
    )


def test_sync_rules_once_returns_failure_when_scheduler_sync_raises(engine):
    synchronizer = Mock()
    synchronizer.sync.side_effect = ValueError("invalid cron")

    result = worker.sync_rules_once(synchronizer, session_factory=lambda: Session(engine))

    assert result == worker.RuleSyncResult(
        ok=False, error="ValueError: worker synchronization failed"
    )


def test_run_sync_loop_records_successful_sync_heartbeat(engine):
    scheduler = Mock()

    def stop_after_initial_sync(_interval_seconds):
        raise KeyboardInterrupt

    worker.run_sync_loop(
        scheduler,
        Mock(),
        interval_seconds=10.0,
        worker_id="worker-a",
        session_factory=lambda: Session(engine),
        sync_once=Mock(return_value=worker.RuleSyncResult(ok=True)),
        sleep_fn=stop_after_initial_sync,
    )

    with Session(engine) as session:
        heartbeat = session.get(WorkerHeartbeat, 1)

    assert heartbeat is not None
    assert heartbeat.worker_id == "worker-a"
    assert heartbeat.last_sync_ok is True
    scheduler.start.assert_called_once()
    scheduler.shutdown.assert_called_once()


def test_run_sync_loop_records_failed_sync_heartbeat(engine):
    scheduler = Mock()

    def stop_after_initial_sync(_interval_seconds):
        raise KeyboardInterrupt

    worker.run_sync_loop(
        scheduler,
        Mock(),
        interval_seconds=10.0,
        worker_id="worker-a",
        session_factory=lambda: Session(engine),
        sync_once=Mock(
            return_value=worker.RuleSyncResult(
                ok=False, error="RuntimeError: worker synchronization failed"
            )
        ),
        sleep_fn=stop_after_initial_sync,
    )

    with Session(engine) as session:
        heartbeat = session.get(WorkerHeartbeat, 1)

    assert heartbeat is not None
    assert heartbeat.last_sync_ok is False
    assert heartbeat.last_error == "RuntimeError: worker synchronization failed"
    scheduler.start.assert_called_once()


def test_heartbeat_write_failure_does_not_stop_scheduler(caplog):
    scheduler = Mock()

    def stop_after_initial_sync(_interval_seconds):
        raise KeyboardInterrupt

    with caplog.at_level("ERROR"):
        worker.run_sync_loop(
            scheduler,
            Mock(),
            interval_seconds=10.0,
            worker_id="worker-a",
            session_factory=Mock(),
            sync_once=Mock(return_value=worker.RuleSyncResult(ok=True)),
            record_sync=Mock(side_effect=RuntimeError("database unavailable")),
            sleep_fn=stop_after_initial_sync,
        )

    scheduler.start.assert_called_once()
    scheduler.shutdown.assert_called_once()
    assert "记录 Worker 心跳失败" in caplog.text


def test_worker_skips_rule_when_execution_lease_is_busy(caplog):
    from app.execution_lock import RuleExecutionInProgressError
    from app.worker import build_execute_rule_callback

    session_context = Mock()
    session_context.__enter__ = Mock(return_value=session_context)
    session_context.__exit__ = Mock(return_value=False)
    execute_rule = Mock(side_effect=RuleExecutionInProgressError("busy"))
    callback = build_execute_rule_callback(
        session_factory=lambda: session_context,
        execute_rule_by_id_fn=execute_rule,
    )

    with caplog.at_level("WARNING"):
        callback(42)

    execute_rule.assert_called_once()
    assert "rule_id=42" in caplog.text


def test_worker_main_passes_misfire_grace_seconds_to_scheduler_and_synchronizer(
    engine, monkeypatch,
):
    import app.worker as worker

    scheduler = object()
    execute_rule = object()
    settings = SimpleNamespace(
        scheduler_misfire_grace_seconds=45,
        scheduler_sync_interval_seconds=10.0,
    )
    build_scheduler = Mock(return_value=scheduler)
    synchronizer = Mock()

    monkeypatch.setattr(worker, "init_db", Mock())
    monkeypatch.setattr(worker, "get_engine", Mock(return_value=engine))
    monkeypatch.setattr(worker, "uuid4", Mock(return_value=SimpleNamespace(hex="worker-a")))
    monkeypatch.setattr(worker, "get_settings", Mock(return_value=settings))
    monkeypatch.setattr(worker, "build_execute_rule_callback", Mock(return_value=execute_rule))
    monkeypatch.setattr(worker, "build_scheduler", build_scheduler)
    monkeypatch.setattr(worker, "RuleScheduleSynchronizer", Mock(return_value=synchronizer))
    run_sync_loop = Mock()
    monkeypatch.setattr(worker, "run_sync_loop", run_sync_loop)

    worker.main()

    build_scheduler.assert_called_once_with(
        [], execute_rule, misfire_grace_seconds=45
    )
    worker.RuleScheduleSynchronizer.assert_called_once_with(
        scheduler, execute_rule, misfire_grace_seconds=45
    )
    run_sync_loop.assert_called_once()
    assert run_sync_loop.call_args.args == (scheduler, synchronizer)
    assert run_sync_loop.call_args.kwargs["interval_seconds"] == 10.0
    assert run_sync_loop.call_args.kwargs["worker_id"] == "worker-a"
    assert callable(run_sync_loop.call_args.kwargs["session_factory"])

    with Session(engine) as session:
        heartbeat = session.get(WorkerHeartbeat, 1)

    assert heartbeat is not None
    assert heartbeat.worker_id == "worker-a"
