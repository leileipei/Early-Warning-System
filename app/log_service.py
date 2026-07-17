from dataclasses import dataclass
from math import ceil
from typing import Generic, TypeVar

from sqlalchemy import func, or_
from sqlmodel import Session, select

from app.models import ExecutionLog, ExecutionStatus, MailLog, MailStatus, TriggerType

T = TypeVar("T")

DEFAULT_PAGE_SIZE = 50
MIN_PAGE_SIZE = 10
MAX_PAGE_SIZE = 200


@dataclass(frozen=True)
class LogFilters:
    execution_status: str = ""
    trigger_type: str = ""
    rule_id: str = ""
    mail_status: str = ""
    keyword: str = ""


@dataclass(frozen=True)
class Page(Generic[T]):
    items: list[T]
    page: int
    page_size: int
    total: int
    total_pages: int
    has_previous: bool
    has_next: bool


def list_execution_logs(
    session: Session,
    filters: LogFilters,
    *,
    page: int,
    page_size: int,
) -> Page[ExecutionLog]:
    statement = select(ExecutionLog)
    if filters.execution_status in {status.value for status in ExecutionStatus}:
        statement = statement.where(ExecutionLog.status == filters.execution_status)
    if filters.trigger_type in {trigger.value for trigger in TriggerType}:
        statement = statement.where(ExecutionLog.trigger_type == filters.trigger_type)
    if filters.rule_id:
        try:
            statement = statement.where(ExecutionLog.rule_id == int(filters.rule_id))
        except ValueError:
            statement = statement.where(ExecutionLog.rule_id == -1)
    if filters.keyword:
        statement = statement.where(
            or_(
                ExecutionLog.error_type.contains(filters.keyword),
                ExecutionLog.error_message.contains(filters.keyword),
            )
        )
    return _paginate(
        session,
        statement,
        ExecutionLog.started_at.desc(),
        page=page,
        page_size=page_size,
    )


def list_mail_logs(
    session: Session,
    filters: LogFilters,
    *,
    page: int,
    page_size: int,
) -> Page[MailLog]:
    statement = select(MailLog)
    if filters.mail_status in {status.value for status in MailStatus}:
        statement = statement.where(MailLog.status == filters.mail_status)
    if filters.keyword:
        statement = statement.where(
            or_(
                MailLog.recipients.contains(filters.keyword),
                MailLog.cc_recipients.contains(filters.keyword),
                MailLog.subject.contains(filters.keyword),
                MailLog.error_message.contains(filters.keyword),
            )
        )
    return _paginate(
        session,
        statement,
        MailLog.sent_at.desc(),
        page=page,
        page_size=page_size,
    )


def _paginate(session: Session, statement, order_by, *, page: int, page_size: int) -> Page:
    total = session.exec(select(func.count()).select_from(statement.subquery())).one()
    normalized_page_size = min(max(page_size, MIN_PAGE_SIZE), MAX_PAGE_SIZE)
    total_pages = max(1, ceil(total / normalized_page_size))
    normalized_page = min(max(page, 1), total_pages)
    items = session.exec(
        statement.order_by(order_by)
        .offset((normalized_page - 1) * normalized_page_size)
        .limit(normalized_page_size)
    ).all()
    return Page(
        items=items,
        page=normalized_page,
        page_size=normalized_page_size,
        total=total,
        total_pages=total_pages,
        has_previous=normalized_page > 1,
        has_next=normalized_page < total_pages,
    )
