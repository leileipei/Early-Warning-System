import importlib

import pytest

from app.models import AlertRule, SendMode
from app.scheduler import build_scheduler


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


def test_scheduler_raises_for_invalid_cron_expression():
    with pytest.raises(ValueError):
        build_scheduler([make_rule(cron_expression="not a cron")], execute_rule=lambda rule_id: None)


def test_worker_import_does_not_block():
    worker = importlib.import_module("app.worker")

    assert callable(worker.main)
