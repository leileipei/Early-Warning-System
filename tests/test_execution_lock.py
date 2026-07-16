from datetime import datetime, timedelta

import pytest
from sqlmodel import Session

from app.execution_lock import RuleExecutionInProgressError, rule_execution_lease
from app.models import AlertRule, RuleExecutionLease, SendMode, SqlDataSource


@pytest.fixture
def persisted_rule_id(engine):
    with Session(engine) as session:
        source = SqlDataSource(
            name="lease-source",
            host="db.example.com",
            database="erp",
            username="readonly",
            encrypted_password="encrypted",
        )
        session.add(source)
        session.commit()
        session.refresh(source)
        rule = AlertRule(
            name="lease-rule",
            data_source_id=source.id,
            sql_text="select 1 as ok",
            cron_expression="0 9 * * *",
            recipients="ops@example.com",
            subject_template="预警",
            body_template="{{table}}",
            send_mode=SendMode.SUMMARY,
        )
        session.add(rule)
        session.commit()
        session.refresh(rule)
        return rule.id


def test_rule_execution_lease_is_released_after_context(engine, persisted_rule_id):
    with Session(engine) as session:
        with rule_execution_lease(session, persisted_rule_id, lease_seconds=60):
            assert session.get(RuleExecutionLease, persisted_rule_id) is not None
        assert session.get(RuleExecutionLease, persisted_rule_id) is None


def test_rule_execution_lease_rejects_second_session(engine, persisted_rule_id):
    with Session(engine) as first, Session(engine) as second:
        with rule_execution_lease(first, persisted_rule_id, lease_seconds=60):
            with pytest.raises(RuleExecutionInProgressError):
                with rule_execution_lease(second, persisted_rule_id, lease_seconds=60):
                    pytest.fail("second execution must not enter")


def test_expired_rule_execution_lease_can_be_replaced(engine, persisted_rule_id):
    now = datetime(2026, 7, 16, 0, 0, 0)
    with Session(engine) as session:
        session.add(
            RuleExecutionLease(
                rule_id=persisted_rule_id,
                owner_token="stale-owner",
                acquired_at=now - timedelta(minutes=2),
                expires_at=now - timedelta(minutes=1),
            )
        )
        session.commit()
        with rule_execution_lease(
            session,
            persisted_rule_id,
            lease_seconds=60,
            now_fn=lambda: now,
        ):
            assert session.get(RuleExecutionLease, persisted_rule_id).owner_token != "stale-owner"


def test_rule_execution_lease_releases_after_exception(engine, persisted_rule_id):
    with Session(engine) as session:
        with pytest.raises(RuntimeError, match="boom"):
            with rule_execution_lease(session, persisted_rule_id, lease_seconds=60):
                raise RuntimeError("boom")
        assert session.get(RuleExecutionLease, persisted_rule_id) is None


def test_expired_owner_cannot_release_replacement_lease(engine, persisted_rule_id):
    current = [datetime(2026, 7, 16, 0, 0, 0)]
    first_session = Session(engine)
    second_session = Session(engine)
    first = rule_execution_lease(
        first_session,
        persisted_rule_id,
        lease_seconds=10,
        now_fn=lambda: current[0],
    )
    second = rule_execution_lease(
        second_session,
        persisted_rule_id,
        lease_seconds=10,
        now_fn=lambda: current[0],
    )
    first_entered = False
    second_entered = False
    try:
        first.__enter__()
        first_entered = True
        current[0] += timedelta(seconds=11)
        second.__enter__()
        second_entered = True
        replacement_token = second_session.get(
            RuleExecutionLease,
            persisted_rule_id,
        ).owner_token

        first.__exit__(None, None, None)
        first_entered = False

        second_session.expire_all()
        lease = second_session.get(RuleExecutionLease, persisted_rule_id)
        assert lease is not None
        assert lease.owner_token == replacement_token
    finally:
        if second_entered:
            second.__exit__(None, None, None)
        if first_entered:
            first.__exit__(None, None, None)
        first_session.close()
        second_session.close()
