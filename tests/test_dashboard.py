from datetime import datetime, timedelta

from app.dashboard import build_dashboard_context
from app.models import (
    AlertRule,
    ExecutionLog,
    ExecutionStatus,
    MailLog,
    MailStatus,
    SendMode,
    SqlDataSource,
    TriggerType,
)


def _create_source(session) -> SqlDataSource:
    source = SqlDataSource(
        name="dashboard-source",
        host="db.example.com",
        database="erp",
        username="readonly",
        encrypted_password="encrypted",
    )
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


def _create_rule(
    session,
    source: SqlDataSource,
    name: str,
    *,
    enabled: bool = True,
    archived_at: datetime | None = None,
) -> AlertRule:
    rule = AlertRule(
        name=name,
        data_source_id=source.id,
        sql_text="select 1 as warning",
        cron_expression="0 9 * * *",
        recipients="ops@example.com",
        subject_template="预警",
        body_template="{{ table }}",
        send_mode=SendMode.SUMMARY,
        enabled=enabled,
        archived_at=archived_at,
    )
    session.add(rule)
    session.commit()
    session.refresh(rule)
    return rule


def _create_execution(
    session,
    rule: AlertRule,
    *,
    started_at: datetime,
    status: ExecutionStatus,
    trigger_type: TriggerType = TriggerType.SCHEDULED,
    row_count: int = 1,
    email_count: int = 1,
) -> ExecutionLog:
    execution = ExecutionLog(
        rule_id=rule.id,
        trigger_type=trigger_type,
        status=status,
        started_at=started_at,
        finished_at=started_at + timedelta(seconds=1),
        row_count=row_count,
        email_count=email_count,
    )
    session.add(execution)
    session.commit()
    session.refresh(execution)
    return execution


def _create_mail(
    session,
    execution: ExecutionLog,
    *,
    status: MailStatus,
    sent_at: datetime,
) -> None:
    session.add(
        MailLog(
            execution_log_id=execution.id,
            recipients="ops@example.com",
            subject="预警",
            status=status,
            sent_at=sent_at,
        )
    )
    session.commit()


def test_dashboard_context_uses_real_counts_and_recent_rows(session):
    now = datetime(2026, 7, 16, 2, 0, 0)  # 10:00 Asia/Shanghai
    source = _create_source(session)
    latest_rule = _create_rule(session, source, "最新规则")
    second_rule = _create_rule(session, source, "第二规则")
    _create_rule(session, source, "停用规则", enabled=False)
    _create_rule(session, source, "已归档规则", archived_at=now - timedelta(days=1))

    latest = _create_execution(
        session,
        latest_rule,
        started_at=now - timedelta(hours=1),
        status=ExecutionStatus.FAILED,
    )
    second = _create_execution(
        session,
        second_rule,
        started_at=now - timedelta(hours=2),
        status=ExecutionStatus.PARTIAL_FAILED,
    )
    _create_execution(
        session,
        latest_rule,
        started_at=now - timedelta(hours=3),
        status=ExecutionStatus.SUCCESS,
    )
    old = _create_execution(
        session,
        latest_rule,
        started_at=now - timedelta(hours=25),
        status=ExecutionStatus.FAILED,
    )
    for execution in (latest, second, latest, latest):
        _create_mail(
            session,
            execution,
            status=MailStatus.SUCCESS,
            sent_at=now - timedelta(minutes=30),
        )
    _create_mail(
        session,
        latest,
        status=MailStatus.FAILED,
        sent_at=now - timedelta(minutes=20),
    )
    _create_mail(
        session,
        old,
        status=MailStatus.FAILED,
        sent_at=now - timedelta(hours=25),
    )

    context = build_dashboard_context(session, now=now)

    assert context["enabled_rule_count"] == 2
    assert context["today_execution_count"] == 3
    assert context["recent_failure_count"] == 2
    assert context["mail_success_count"] == 4
    assert context["mail_failure_count"] == 1
    assert len(context["recent_executions"]) <= 5
    assert context["recent_executions"][0]["rule_name"] == "最新规则"


def test_dashboard_today_count_uses_shanghai_midnight(session):
    now = datetime(2026, 7, 15, 16, 30, 0)
    source = _create_source(session)
    rule = _create_rule(session, source, "边界规则")
    _create_execution(
        session,
        rule,
        started_at=datetime(2026, 7, 15, 16, 0, 0),
        status=ExecutionStatus.SUCCESS,
    )
    _create_execution(
        session,
        rule,
        started_at=datetime(2026, 7, 15, 15, 59, 59),
        status=ExecutionStatus.SUCCESS,
    )
    _create_execution(
        session,
        rule,
        started_at=datetime(2026, 7, 16, 16, 0, 0),
        status=ExecutionStatus.SUCCESS,
    )

    context = build_dashboard_context(session, now=now)

    assert context["today_execution_count"] == 1


def test_dashboard_recent_executions_returns_latest_five(session):
    now = datetime(2026, 7, 16, 2, 0, 0)
    source = _create_source(session)
    rule = _create_rule(session, source, "排序规则")
    for minutes_ago in range(6):
        _create_execution(
            session,
            rule,
            started_at=now - timedelta(minutes=minutes_ago),
            status=ExecutionStatus.SUCCESS,
        )

    context = build_dashboard_context(session, now=now)

    returned_times = [item["log"].started_at for item in context["recent_executions"]]
    assert returned_times == [now - timedelta(minutes=value) for value in range(5)]


def test_dashboard_empty_database_returns_real_zeroes(session):
    context = build_dashboard_context(
        session,
        now=datetime(2026, 7, 16, 2, 0, 0),
    )

    assert context == {
        "enabled_rule_count": 0,
        "today_execution_count": 0,
        "recent_failure_count": 0,
        "recent_executions": [],
        "mail_success_count": 0,
        "mail_failure_count": 0,
    }
