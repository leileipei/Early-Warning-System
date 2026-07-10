import importlib
from unittest.mock import Mock

from app.models import AlertRule, SendMode
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


def test_scheduler_adds_enabled_rule_job():
    scheduler = build_scheduler([make_rule()], execute_rule=lambda rule_id: None)

    jobs = scheduler.get_jobs()

    assert len(jobs) == 1
    assert jobs[0].id == "rule-7"
    assert list(jobs[0].args) == [7]


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


def test_rule_synchronizer_replaces_job_when_cron_changes():
    scheduler = build_scheduler([], execute_rule=lambda rule_id: None)
    synchronizer = RuleScheduleSynchronizer(scheduler, execute_rule=lambda rule_id: None)

    synchronizer.sync([make_rule(id=7, cron_expression="0 9 * * *")])
    first_trigger = str(scheduler.get_job("rule-7").trigger)
    synchronizer.sync([make_rule(id=7, cron_expression="30 10 * * *")])

    assert str(scheduler.get_job("rule-7").trigger) != first_trigger
    assert "10" in str(scheduler.get_job("rule-7").trigger)
    assert "30" in str(scheduler.get_job("rule-7").trigger)


def test_rule_synchronizer_removes_disabled_or_deleted_rules():
    scheduler = build_scheduler([], execute_rule=lambda rule_id: None)
    synchronizer = RuleScheduleSynchronizer(scheduler, execute_rule=lambda rule_id: None)
    synchronizer.sync([make_rule(id=7), make_rule(id=8)])

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
