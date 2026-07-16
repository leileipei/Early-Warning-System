from datetime import datetime, timedelta

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.execution_lock import RuleExecutionInProgressError, rule_execution_lease
from app.models import AlertRule, RuleExecutionLease, SendMode, SqlDataSource


def _raise_release_commit_error(_connection):
    raise RuntimeError("release commit failed")


@pytest.fixture
def lease_engine(tmp_path):
    from app.db import create_db_engine, init_db

    engine = create_db_engine(f"sqlite:///{tmp_path / 'execution-leases.sqlite3'}")
    init_db(engine)
    yield engine
    engine.dispose()


def test_lease_sessions_use_distinct_dbapi_connections(lease_engine):
    with Session(lease_engine) as first, Session(lease_engine) as second:
        first_connection = first.connection().connection.driver_connection
        second_connection = second.connection().connection.driver_connection

        assert first_connection is not second_connection


@pytest.fixture
def persisted_rule_id(lease_engine):
    with Session(lease_engine) as session:
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


def test_rule_execution_lease_is_released_after_context(lease_engine, persisted_rule_id):
    with Session(lease_engine) as session:
        with rule_execution_lease(session, persisted_rule_id, lease_seconds=60):
            assert session.get(RuleExecutionLease, persisted_rule_id) is not None
        assert session.get(RuleExecutionLease, persisted_rule_id) is None


def test_rule_execution_lease_rejects_second_session(lease_engine, persisted_rule_id):
    with Session(lease_engine) as first, Session(lease_engine) as second:
        with rule_execution_lease(first, persisted_rule_id, lease_seconds=60):
            first_connection = first.connection().connection.driver_connection
            second_connection = second.connection().connection.driver_connection
            assert first_connection is not second_connection

            with pytest.raises(RuleExecutionInProgressError):
                with rule_execution_lease(second, persisted_rule_id, lease_seconds=60):
                    pytest.fail("second execution must not enter")

            rule = second.exec(
                select(AlertRule).where(AlertRule.id == persisted_rule_id)
            ).one()
            assert rule.id == persisted_rule_id


def test_expired_rule_execution_lease_can_be_replaced(lease_engine, persisted_rule_id):
    now = datetime(2026, 7, 16, 0, 0, 0)
    with Session(lease_engine) as session:
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


def test_rule_execution_lease_releases_after_exception(lease_engine, persisted_rule_id):
    with Session(lease_engine) as session:
        with pytest.raises(RuntimeError, match="boom"):
            with rule_execution_lease(session, persisted_rule_id, lease_seconds=60):
                raise RuntimeError("boom")
        assert session.get(RuleExecutionLease, persisted_rule_id) is None


def test_integrity_error_releases_lease_and_session_remains_usable(
    lease_engine,
    persisted_rule_id,
):
    with Session(lease_engine) as session:
        with pytest.raises(IntegrityError):
            with rule_execution_lease(session, persisted_rule_id, lease_seconds=60):
                session.add(
                    SqlDataSource(
                        name="lease-source",
                        host="duplicate.example.com",
                        database="erp",
                        username="readonly",
                        encrypted_password="encrypted",
                    )
                )
                session.commit()

        assert session.get(RuleExecutionLease, persisted_rule_id) is None
        rule = session.exec(select(AlertRule).where(AlertRule.id == persisted_rule_id)).one()
        assert rule.id == persisted_rule_id


def test_release_failure_is_raised_and_session_is_rolled_back(lease_engine, persisted_rule_id):
    with Session(lease_engine) as session:
        lease = rule_execution_lease(session, persisted_rule_id, lease_seconds=60)
        lease.__enter__()
        event.listen(lease_engine, "commit", _raise_release_commit_error, once=True)

        with pytest.raises(RuntimeError, match="release commit failed"):
            lease.__exit__(None, None, None)

        assert session.execute(text("SELECT 1")).scalar_one() == 1


def test_release_failure_does_not_replace_body_error(lease_engine, persisted_rule_id):
    with Session(lease_engine) as session:
        with pytest.raises(RuntimeError, match="body failed"):
            with rule_execution_lease(session, persisted_rule_id, lease_seconds=60):
                event.listen(lease_engine, "commit", _raise_release_commit_error, once=True)
                raise RuntimeError("body failed")

        assert session.execute(text("SELECT 1")).scalar_one() == 1


def test_expired_owner_cannot_release_replacement_lease(lease_engine, persisted_rule_id):
    current = [datetime(2026, 7, 16, 0, 0, 0)]
    first_session = Session(lease_engine)
    second_session = Session(lease_engine)
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
        first_connection = first_session.connection().connection.driver_connection
        second_connection = second_session.connection().connection.driver_connection
        assert first_connection is not second_connection

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
