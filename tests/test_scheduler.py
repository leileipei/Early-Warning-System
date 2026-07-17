import importlib
from unittest.mock import Mock

import pytest
from apscheduler.triggers.cron import CronTrigger

from app import scheduler as scheduler_module
from app.models import AlertRule, SendMode, utc_now
from app.scheduler import RuleScheduleSynchronizer, build_scheduler


def make_rule(**overrides):
    data = {
        "id": 7,
        "name": "daily",
        "data_source_id": 1,
        "sql_text": "select id from orders",
        "cron_expression": "0 9 * * *",
        "recipients": "ops@example.com",
        "subject_template": "预警",
        "body_template": "{{table}}",
        "send_mode": SendMode.SUMMARY,
        "enabled": True,
    }
    data.update(overrides)
    return AlertRule(**data)


class RecordingScheduler:
    def __init__(self, **kwargs):
        self.added_jobs = []

    def add_job(self, *args, **kwargs):
        self.added_jobs.append((args, kwargs))

    def get_jobs(self):
        return []

    def get_job(self, job_id):
        return None


def assert_job_options(kwargs, misfire_grace_seconds):
    assert kwargs["misfire_grace_time"] == misfire_grace_seconds
    assert kwargs["coalesce"] is True
    assert kwargs["max_instances"] == 1


def test_scheduler_adds_enabled_rule_job():
    scheduler = build_scheduler([make_rule()], execute_rule=lambda rule_id: None)

    jobs = scheduler.get_jobs()

    assert len(jobs) == 1
    assert jobs[0].id == "rule-7"
    assert list(jobs[0].args) == [7]


def test_scheduler_passes_misfire_options_when_building_initial_jobs(monkeypatch):
    monkeypatch.setattr(scheduler_module, "BackgroundScheduler", RecordingScheduler)
    scheduler = build_scheduler(
        [make_rule()],
        execute_rule=lambda rule_id: None,
        misfire_grace_seconds=45,
    )

    assert_job_options(scheduler.added_jobs[0][1], 45)


def test_scheduler_uses_default_misfire_options_when_building_initial_jobs(monkeypatch):
    monkeypatch.setattr(scheduler_module, "BackgroundScheduler", RecordingScheduler)
    scheduler = build_scheduler([make_rule()], execute_rule=lambda rule_id: None)

    assert_job_options(scheduler.added_jobs[0][1], 300)


def test_rule_synchronizer_passes_misfire_options_when_adding_dynamic_job():
    scheduler = RecordingScheduler()
    synchronizer = RuleScheduleSynchronizer(
        scheduler,
        execute_rule=lambda rule_id: None,
        misfire_grace_seconds=45,
    )

    synchronizer.sync([make_rule()])

    assert_job_options(scheduler.added_jobs[0][1], 45)


def test_rule_synchronizer_uses_default_misfire_options_when_adding_dynamic_job():
    scheduler = RecordingScheduler()
    synchronizer = RuleScheduleSynchronizer(
        scheduler,
        execute_rule=lambda rule_id: None,
    )

    synchronizer.sync([make_rule()])

    assert_job_options(scheduler.added_jobs[0][1], 300)


def test_scheduler_skips_disabled_and_unsaved_rules():
    scheduler = build_scheduler(
        [
            make_rule(id=1, enabled=False),
            make_rule(id=None, enabled=True),
            make_rule(id=2, enabled=True),
        ],
        execute_rule=lambda rule_id: None,
    )

    jobs = scheduler.get_jobs()

    assert [job.id for job in jobs] == ["rule-2"]


def test_scheduler_skips_archived_enabled_rules():
    scheduler = build_scheduler(
        [
            make_rule(id=1),
            make_rule(id=2, archived_at=utc_now()),
        ],
        execute_rule=lambda rule_id: None,
    )

    assert [job.id for job in scheduler.get_jobs()] == ["rule-1"]


def test_scheduler_adds_multiple_rules_with_stable_ids():
    scheduler = build_scheduler(
        [
            make_rule(id=3, cron_expression="0 8 * * *"),
            make_rule(id=9, cron_expression="30 17 * * 1-5"),
        ],
        execute_rule=lambda rule_id: None,
    )

    jobs = sorted(scheduler.get_jobs(), key=lambda job: job.id)

    assert [job.id for job in jobs] == ["rule-3", "rule-9"]
    assert [list(job.args) for job in jobs] == [[3], [9]]


def test_scheduler_skips_invalid_cron_expression():
    scheduler = build_scheduler(
        [
            make_rule(id=1, cron_expression="not a cron"),
            make_rule(id=2, cron_expression="0 9 * * *"),
        ],
        execute_rule=lambda rule_id: None,
    )

    assert [job.id for job in scheduler.get_jobs()] == ["rule-2"]


def test_rule_synchronizer_adds_new_rule_without_rescheduling_unchanged_rule():
    scheduler = build_scheduler([], execute_rule=lambda rule_id: None)
    add_job = Mock(wraps=scheduler.add_job)
    scheduler.add_job = add_job
    synchronizer = RuleScheduleSynchronizer(scheduler, execute_rule=lambda rule_id: None)

    synchronizer.sync([make_rule(id=7)])
    synchronizer.sync([make_rule(id=7)])

    assert add_job.call_count == 1
    assert scheduler.get_job("rule-7") is not None


def test_rule_synchronizer_adopts_existing_unchanged_job_without_rescheduling():
    def execute_rule(rule_id):
        return None

    scheduler = build_scheduler([make_rule(id=7)], execute_rule=execute_rule)
    add_job = Mock(wraps=scheduler.add_job)
    scheduler.add_job = add_job
    synchronizer = RuleScheduleSynchronizer(scheduler, execute_rule=execute_rule)

    synchronizer.sync([make_rule(id=7)])

    assert add_job.call_count == 0
    assert scheduler.get_job("rule-7") is not None


def test_rule_synchronizer_replaces_job_when_cron_changes():
    scheduler = build_scheduler([], execute_rule=lambda rule_id: None)
    synchronizer = RuleScheduleSynchronizer(scheduler, execute_rule=lambda rule_id: None)

    synchronizer.sync([make_rule(id=7, cron_expression="0 9 * * *")])
    first_trigger = str(scheduler.get_job("rule-7").trigger)
    synchronizer.sync([make_rule(id=7, cron_expression="30 10 * * *")])

    assert str(scheduler.get_job("rule-7").trigger) != first_trigger
    assert "10" in str(scheduler.get_job("rule-7").trigger)
    assert "30" in str(scheduler.get_job("rule-7").trigger)


@pytest.mark.parametrize("start_paused", [False, True], ids=["stopped", "started-paused"])
def test_rule_synchronizer_preserves_existing_job_when_cron_replacement_fails(start_paused):
    def execute_rule(rule_id):
        return None

    scheduler = build_scheduler(
        [make_rule(id=7, cron_expression="0 9 * * *")],
        execute_rule=execute_rule,
    )
    if start_paused:
        scheduler.start(paused=True)
    original_trigger = str(scheduler.get_job("rule-7").trigger)
    replacement_trigger = str(CronTrigger.from_crontab("30 10 * * *"))
    real_add_job = scheduler.add_job
    real_reschedule_job = scheduler.reschedule_job
    attempts = 0

    def fail_first_replacement(operation, *args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("scheduler unavailable")
        return operation(*args, **kwargs)

    scheduler.add_job = lambda *args, **kwargs: fail_first_replacement(
        real_add_job, *args, **kwargs
    )
    scheduler.reschedule_job = lambda *args, **kwargs: fail_first_replacement(
        real_reschedule_job, *args, **kwargs
    )
    synchronizer = RuleScheduleSynchronizer(scheduler, execute_rule=execute_rule)

    try:
        synchronizer.sync([make_rule(id=7, cron_expression="30 10 * * *")])

        retained_job = scheduler.get_job("rule-7")
        assert retained_job is not None
        assert retained_job.id == "rule-7"
        assert retained_job.func is execute_rule
        assert list(retained_job.args) == [7]
        assert retained_job.max_instances == 1
        assert retained_job.coalesce is True
        assert str(retained_job.trigger) == original_trigger

        synchronizer.sync([make_rule(id=7, cron_expression="30 10 * * *")])

        replacement_job = scheduler.get_job("rule-7")
        assert attempts == 2
        assert replacement_job is not None
        assert replacement_job.id == "rule-7"
        assert replacement_job.func is execute_rule
        assert list(replacement_job.args) == [7]
        assert replacement_job.max_instances == 1
        assert replacement_job.coalesce is True
        assert str(replacement_job.trigger) == replacement_trigger
    finally:
        if start_paused:
            scheduler.shutdown(wait=False)


def test_rule_synchronizer_removes_disabled_or_deleted_rules():
    scheduler = build_scheduler([], execute_rule=lambda rule_id: None)
    synchronizer = RuleScheduleSynchronizer(scheduler, execute_rule=lambda rule_id: None)
    synchronizer.sync([make_rule(id=7), make_rule(id=8)])

    synchronizer.sync([make_rule(id=7, enabled=False)])

    assert scheduler.get_job("rule-7") is None
    assert scheduler.get_job("rule-8") is None


def test_rule_synchronizer_removes_archived_rule():
    scheduler = build_scheduler([make_rule(id=7)], execute_rule=lambda rule_id: None)
    synchronizer = RuleScheduleSynchronizer(scheduler, execute_rule=lambda rule_id: None)

    synchronizer.sync([make_rule(id=7, archived_at=utc_now())])

    assert scheduler.get_job("rule-7") is None


def test_rule_synchronizer_removes_preexisting_disabled_or_deleted_rules():
    def execute_rule(rule_id):
        return None

    scheduler = build_scheduler(
        [make_rule(id=7), make_rule(id=8)],
        execute_rule=execute_rule,
    )
    synchronizer = RuleScheduleSynchronizer(scheduler, execute_rule=execute_rule)

    synchronizer.sync([make_rule(id=7, enabled=False)])

    assert scheduler.get_job("rule-7") is None
    assert scheduler.get_job("rule-8") is None


def test_rule_synchronizer_restores_missing_job():
    scheduler = build_scheduler([], execute_rule=lambda rule_id: None)
    synchronizer = RuleScheduleSynchronizer(scheduler, execute_rule=lambda rule_id: None)
    synchronizer.sync([make_rule(id=7)])
    scheduler.remove_job("rule-7")

    synchronizer.sync([make_rule(id=7)])

    assert scheduler.get_job("rule-7") is not None


def test_rule_synchronizer_retries_failed_rule_without_blocking_other_rules():
    scheduler = build_scheduler([], execute_rule=lambda rule_id: None)
    real_add_job = scheduler.add_job
    failed_once = {"rule-1": False}

    def flaky_add_job(*args, **kwargs):
        if kwargs["id"] == "rule-1" and not failed_once["rule-1"]:
            failed_once["rule-1"] = True
            raise RuntimeError("scheduler unavailable")
        return real_add_job(*args, **kwargs)

    scheduler.add_job = flaky_add_job
    synchronizer = RuleScheduleSynchronizer(scheduler, execute_rule=lambda rule_id: None)

    synchronizer.sync([make_rule(id=1), make_rule(id=2)])
    assert scheduler.get_job("rule-1") is None
    assert scheduler.get_job("rule-2") is not None

    synchronizer.sync([make_rule(id=1), make_rule(id=2)])
    assert scheduler.get_job("rule-1") is not None


def test_rule_synchronizer_isolates_get_job_failure_and_retries_rule():
    scheduler = build_scheduler([], execute_rule=lambda rule_id: None)
    logger = Mock()
    synchronizer = RuleScheduleSynchronizer(
        scheduler,
        execute_rule=lambda rule_id: None,
        logger=logger,
    )
    synchronizer.sync([make_rule(id=1)])
    real_get_job = scheduler.get_job
    attempts = {"rule-1": 0}

    def flaky_get_job(job_id):
        if job_id == "rule-1":
            attempts[job_id] += 1
            if attempts[job_id] == 1:
                raise RuntimeError("scheduler unavailable")
        return real_get_job(job_id)

    scheduler.get_job = flaky_get_job

    synchronizer.sync([make_rule(id=1), make_rule(id=2)])
    assert real_get_job("rule-2") is not None

    synchronizer.sync([make_rule(id=1), make_rule(id=2)])
    assert attempts["rule-1"] == 2
    logger.exception.assert_called_once_with("同步规则调度任务失败: rule_id=%s", 1)


def test_worker_import_does_not_block():
    worker = importlib.import_module("app.worker")

    assert callable(worker.main)


def test_worker_execute_rule_callback_opens_session_and_uses_scheduled_trigger():
    worker = importlib.import_module("app.worker")
    calls = []
    sessions = []

    class FakeSession:
        def __enter__(self):
            sessions.append("opened")
            return "session"

        def __exit__(self, exc_type, exc, traceback):
            sessions.append("closed")

    def execute_rule_by_id(session, rule_id, trigger_type):
        calls.append((session, rule_id, trigger_type))

    callback = worker.build_execute_rule_callback(
        session_factory=FakeSession,
        execute_rule_by_id_fn=execute_rule_by_id,
    )

    callback(7)

    assert sessions == ["opened", "closed"]
    assert calls == [("session", 7, worker.TriggerType.SCHEDULED)]


def test_worker_sync_rules_once_loads_all_rules_and_calls_synchronizer():
    worker = importlib.import_module("app.worker")
    rules = [make_rule(id=1), make_rule(id=2, enabled=False)]
    synchronizer = Mock()

    class QueryResult:
        def all(self):
            return rules

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return None

        def exec(self, statement):
            return QueryResult()

    result = worker.sync_rules_once(synchronizer, session_factory=FakeSession)

    assert result is True
    synchronizer.sync.assert_called_once_with(rules)


def test_worker_sync_rules_once_preserves_scheduler_when_database_read_fails():
    worker = importlib.import_module("app.worker")
    synchronizer = Mock()

    class FailingSession:
        def __enter__(self):
            raise RuntimeError("database is locked")

        def __exit__(self, exc_type, exc, traceback):
            return None

    result = worker.sync_rules_once(synchronizer, session_factory=FailingSession)

    assert result is False
    synchronizer.sync.assert_not_called()


def test_worker_run_loop_syncs_immediately_then_uses_configured_interval():
    worker = importlib.import_module("app.worker")
    scheduler = Mock()
    synchronizer = Mock()
    sync_calls = []
    sleeps = []

    def sync_once(target):
        sync_calls.append(target)
        if len(sync_calls) == 2:
            raise KeyboardInterrupt
        return True

    def sleep_fn(seconds):
        sleeps.append(seconds)

    worker.run_sync_loop(
        scheduler,
        synchronizer,
        interval_seconds=2.5,
        sync_once=sync_once,
        sleep_fn=sleep_fn,
    )

    assert sync_calls == [synchronizer, synchronizer]
    assert sleeps == [2.5]
    scheduler.start.assert_called_once_with()
    scheduler.shutdown.assert_called_once_with()


def test_worker_run_loop_does_not_shutdown_when_initial_sync_is_interrupted():
    worker = importlib.import_module("app.worker")
    scheduler = Mock()
    synchronizer = Mock()

    def interrupted_sync(target):
        raise KeyboardInterrupt

    worker.run_sync_loop(
        scheduler,
        synchronizer,
        interval_seconds=2.5,
        sync_once=interrupted_sync,
    )

    scheduler.start.assert_not_called()
    scheduler.shutdown.assert_not_called()
