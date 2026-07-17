from datetime import timedelta

from sqlmodel import select

from app.models import AlertRule, ExecutionLog, MailLog, SendMode, SqlDataSource, TriggerType, utc_now


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
