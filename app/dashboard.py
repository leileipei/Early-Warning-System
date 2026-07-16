from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_
from sqlmodel import Session, select

from app.models import (
    AlertRule,
    ExecutionLog,
    ExecutionStatus,
    MailLog,
    MailStatus,
    utc_now,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _shanghai_day_bounds_utc(now: datetime) -> tuple[datetime, datetime]:
    aware_utc = now.replace(tzinfo=UTC)
    local_now = aware_utc.astimezone(SHANGHAI)
    local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    local_end = local_start + timedelta(days=1)
    return (
        local_start.astimezone(UTC).replace(tzinfo=None),
        local_end.astimezone(UTC).replace(tzinfo=None),
    )


def build_dashboard_context(session: Session, *, now: datetime | None = None) -> dict:
    current = now or utc_now()
    today_start, today_end = _shanghai_day_bounds_utc(current)
    recent_start = current - timedelta(hours=24)

    enabled_rule_count = session.exec(
        select(func.count()).select_from(AlertRule).where(
            AlertRule.archived_at.is_(None),
            AlertRule.enabled == True,  # noqa: E712
        )
    ).one()
    today_execution_count = session.exec(
        select(func.count()).select_from(ExecutionLog).where(
            ExecutionLog.started_at >= today_start,
            ExecutionLog.started_at < today_end,
        )
    ).one()
    recent_failure_count = session.exec(
        select(func.count()).select_from(ExecutionLog).where(
            ExecutionLog.started_at >= recent_start,
            or_(
                ExecutionLog.status == ExecutionStatus.FAILED,
                ExecutionLog.status == ExecutionStatus.PARTIAL_FAILED,
            ),
        )
    ).one()
    recent_rows = session.exec(
        select(ExecutionLog, AlertRule.name)
        .join(AlertRule, AlertRule.id == ExecutionLog.rule_id)
        .order_by(ExecutionLog.started_at.desc())
        .limit(5)
    ).all()

    def mail_count(status: MailStatus) -> int:
        return session.exec(
            select(func.count()).select_from(MailLog).where(
                MailLog.sent_at >= recent_start,
                MailLog.status == status,
            )
        ).one()

    return {
        "enabled_rule_count": enabled_rule_count,
        "today_execution_count": today_execution_count,
        "recent_failure_count": recent_failure_count,
        "recent_executions": [
            {"log": log, "rule_name": rule_name} for log, rule_name in recent_rows
        ],
        "mail_success_count": mail_count(MailStatus.SUCCESS),
        "mail_failure_count": mail_count(MailStatus.FAILED),
    }
