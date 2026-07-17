from datetime import timedelta
from unittest.mock import Mock

from sqlmodel import Session, select

from app.models import (
    AlertRule,
    ExecutionLog,
    ExecutionStatus,
    MailLog,
    SendMode,
    SqlDataSource,
    TriggerType,
    utc_now,
)


def _create_rule(session):
    source = SqlDataSource(
        name="log-service-source",
        host="db.example.com",
        database="erp",
        username="readonly",
        encrypted_password="encrypted",
    )
    session.add(source)
    session.commit()
    session.refresh(source)
    rule = AlertRule(
        name="log-service-rule",
        data_source_id=source.id,
        sql_text="select 1",
        cron_expression="0 9 * * *",
        recipients="ops@example.com",
        subject_template="subject",
        body_template="body",
        send_mode=SendMode.SUMMARY,
    )
    session.add(rule)
    session.commit()
    session.refresh(rule)
    return rule


def test_log_services_paginate_and_normalize_bounds_independently(session):
    from app.log_service import LogFilters, list_execution_logs, list_mail_logs

    rule = _create_rule(session)
    session.add_all(
        [
            ExecutionLog(
                rule_id=rule.id,
                trigger_type=TriggerType.MANUAL,
                error_message=f"execution-{index}",
                started_at=utc_now() + timedelta(seconds=index),
            )
            for index in range(61)
        ]
    )
    session.commit()
    execution_logs = session.exec(select(ExecutionLog)).all()
    session.add_all(
        [
            MailLog(
                execution_log_id=execution_logs[0].id,
                recipients="ops@example.com",
                subject=f"mail-{index}",
                status="success",
                sent_at=utc_now() + timedelta(seconds=index),
            )
            for index in range(61)
        ]
    )
    session.commit()

    execution_page = list_execution_logs(
        session, LogFilters(), page=99, page_size=1
    )
    mail_page = list_mail_logs(session, LogFilters(), page=3, page_size=999)

    assert execution_page.page == 7
    assert execution_page.page_size == 10
    assert execution_page.total == 61
    assert execution_page.total_pages == 7
    assert len(execution_page.items) == 1
    assert execution_page.has_previous is True
    assert execution_page.has_next is False
    assert mail_page.page == 1
    assert mail_page.page_size == 200
    assert mail_page.total == 61
    assert mail_page.total_pages == 1
    assert len(mail_page.items) == 61


def test_log_services_normalize_page_below_one_and_keep_filters(session):
    from app.log_service import LogFilters, list_execution_logs

    rule = _create_rule(session)
    session.add_all(
        [
            ExecutionLog(
                rule_id=rule.id,
                trigger_type=TriggerType.MANUAL,
                status="failed" if index % 2 else "success",
                error_message="needle" if index % 2 else "other",
            )
            for index in range(4)
        ]
    )
    session.commit()

    page = list_execution_logs(
        session,
        LogFilters(execution_status="failed", keyword="needle"),
        page=0,
        page_size=50,
    )

    assert page.page == 1
    assert page.page_size == 50
    assert page.total == 2
    assert page.total_pages == 1
    assert [log.error_message for log in page.items] == ["needle", "needle"]


def test_cleanup_expired_logs_deletes_only_completed_logs_before_cutoff_and_their_mail_logs(
    session, engine
):
    from app.log_service import cleanup_expired_logs

    rule = _create_rule(session)
    cutoff_now = utc_now()
    expired = ExecutionLog(
        rule_id=rule.id,
        trigger_type=TriggerType.MANUAL,
        status=ExecutionStatus.SUCCESS,
        finished_at=cutoff_now - timedelta(days=181),
    )
    at_cutoff = ExecutionLog(
        rule_id=rule.id,
        trigger_type=TriggerType.MANUAL,
        status=ExecutionStatus.FAILED,
        finished_at=cutoff_now - timedelta(days=180),
    )
    running = ExecutionLog(
        rule_id=rule.id,
        trigger_type=TriggerType.MANUAL,
        status=ExecutionStatus.RUNNING,
        finished_at=cutoff_now - timedelta(days=181),
    )
    session.add_all([expired, at_cutoff, running])
    session.commit()
    session.refresh(expired)
    session.refresh(at_cutoff)
    session.refresh(running)
    expired_id = expired.id
    at_cutoff_id = at_cutoff.id
    running_id = running.id
    session.add_all(
        [
            MailLog(
                execution_log_id=expired.id,
                recipients="ops@example.com",
                subject="expired",
                status="success",
            ),
            MailLog(
                execution_log_id=at_cutoff.id,
                recipients="ops@example.com",
                subject="at cutoff",
                status="success",
            ),
        ]
    )
    session.commit()

    deleted = cleanup_expired_logs(
        lambda: Session(engine), retention_days=180, now=cutoff_now
    )

    assert deleted == 1
    with Session(engine) as verification_session:
        assert verification_session.get(ExecutionLog, expired_id) is None
        assert verification_session.get(ExecutionLog, at_cutoff_id) is not None
        assert verification_session.get(ExecutionLog, running_id) is not None
        assert verification_session.exec(
            select(MailLog).where(MailLog.execution_log_id == expired_id)
        ).all() == []
        assert len(
            verification_session.exec(
                select(MailLog).where(MailLog.execution_log_id == at_cutoff_id)
            ).all()
        ) == 1


def test_cleanup_expired_logs_commits_each_batch_and_can_retry_a_failed_batch(session, engine):
    from app.log_service import cleanup_expired_logs

    rule = _create_rule(session)
    cutoff_now = utc_now()
    session.add_all(
        [
            ExecutionLog(
                rule_id=rule.id,
                trigger_type=TriggerType.MANUAL,
                status=ExecutionStatus.SUCCESS,
                finished_at=cutoff_now - timedelta(days=181),
            )
            for _ in range(5)
        ]
    )
    session.commit()

    first_session = Session(engine)
    first_session.commit = Mock(wraps=first_session.commit)
    failing_session = Session(engine)
    failing_session.commit = Mock(side_effect=RuntimeError("second batch failed"))
    failing_session.rollback = Mock(wraps=failing_session.rollback)
    sessions = iter([first_session, failing_session])

    try:
        cleanup_expired_logs(
            lambda: next(sessions),
            retention_days=180,
            now=cutoff_now,
            batch_size=2,
        )
    except RuntimeError as exc:
        assert str(exc) == "second batch failed"
    else:
        raise AssertionError("the failed batch must be re-raised")

    first_session.commit.assert_called_once()
    failing_session.rollback.assert_called_once()
    with Session(engine) as verification_session:
        assert len(verification_session.exec(select(ExecutionLog)).all()) == 3

    assert cleanup_expired_logs(
        lambda: Session(engine),
        retention_days=180,
        now=cutoff_now,
        batch_size=2,
    ) == 3
    with Session(engine) as verification_session:
        assert verification_session.exec(select(ExecutionLog)).all() == []


def test_cleanup_expired_logs_uses_utc_now_when_now_is_omitted(session, engine, monkeypatch):
    from app import log_service

    cleanup_now = utc_now() + timedelta(days=365)
    monkeypatch.setattr(log_service, "utc_now", lambda: cleanup_now)
    rule = _create_rule(session)
    execution_log = ExecutionLog(
        rule_id=rule.id,
        trigger_type=TriggerType.MANUAL,
        status=ExecutionStatus.SUCCESS,
        finished_at=cleanup_now - timedelta(days=181),
    )
    session.add(execution_log)
    session.commit()

    assert log_service.cleanup_expired_logs(
        lambda: Session(engine), retention_days=180
    ) == 1
