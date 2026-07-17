import csv
import io
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import ceil
from typing import Generic, TypeVar

from sqlalchemy import and_, delete, func, or_
from sqlmodel import Session, select

from app.models import (
    ExecutionLog,
    ExecutionStatus,
    MailLog,
    MailStatus,
    TriggerType,
    utc_now,
)

T = TypeVar("T")

DEFAULT_PAGE_SIZE = 50
MIN_PAGE_SIZE = 10
MAX_PAGE_SIZE = 200
DEFAULT_CLEANUP_BATCH_SIZE = 500
CSV_EXPORT_BATCH_SIZE = 500


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


def csv_safe_cell(value: object) -> object:
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def stream_csv(
    headers: Sequence[str], row_batches: Iterable[Iterable[Sequence[object]]]
) -> Iterator[str]:
    yield "\ufeff"
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(headers)
    yield buffer.getvalue()
    for batch in row_batches:
        for row in batch:
            buffer.seek(0)
            buffer.truncate(0)
            writer.writerow([csv_safe_cell(cell) for cell in row])
            yield buffer.getvalue()


def iter_execution_log_batches(
    session_factory: Callable[[], Session],
) -> Iterator[list[ExecutionLog]]:
    yield from _iter_log_batches(
        session_factory,
        select(ExecutionLog),
        ExecutionLog.started_at.desc(),
        ExecutionLog.started_at,
        ExecutionLog.id,
    )


def iter_mail_log_batches(session_factory: Callable[[], Session]) -> Iterator[list[MailLog]]:
    yield from _iter_log_batches(
        session_factory,
        select(MailLog),
        MailLog.sent_at.desc(),
        MailLog.sent_at,
        MailLog.id,
    )


def _iter_log_batches(
    session_factory: Callable[[], Session],
    statement,
    order_by,
    timestamp_column,
    id_column,
) -> Iterator[list]:
    with session_factory() as session:
        max_id = next(iter(session.exec(select(func.max(id_column)))), None)
    if max_id is None:
        return

    cursor_timestamp = None
    cursor_id = None
    while True:
        batch_statement = statement.where(id_column <= max_id)
        if cursor_timestamp is not None:
            batch_statement = batch_statement.where(
                or_(
                    timestamp_column < cursor_timestamp,
                    and_(timestamp_column == cursor_timestamp, id_column < cursor_id),
                )
            )
        with session_factory() as session:
            batch = list(
                session.exec(
                    batch_statement.order_by(order_by, id_column.desc()).limit(CSV_EXPORT_BATCH_SIZE)
                )
            )
        if not batch:
            return
        yield batch
        cursor_timestamp = getattr(batch[-1], timestamp_column.key)
        cursor_id = batch[-1].id


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


def cleanup_expired_logs(
    session_factory: Callable[[], Session],
    *,
    retention_days: int,
    now: datetime | None = None,
    batch_size: int = DEFAULT_CLEANUP_BATCH_SIZE,
) -> int:
    if batch_size <= 0:
        raise ValueError("batch_size must be greater than zero")

    effective_batch_size = min(batch_size, DEFAULT_CLEANUP_BATCH_SIZE)
    cutoff = (now or utc_now()) - timedelta(days=retention_days)
    deleted_count = 0

    while True:
        with session_factory() as session:
            try:
                execution_ids = session.exec(
                    select(ExecutionLog.id)
                    .where(
                        ExecutionLog.finished_at < cutoff,
                        ExecutionLog.status != ExecutionStatus.RUNNING,
                    )
                    .order_by(ExecutionLog.id)
                    .limit(effective_batch_size)
                ).all()
                if not execution_ids:
                    return deleted_count

                session.exec(
                    delete(MailLog).where(MailLog.execution_log_id.in_(execution_ids))
                )
                session.exec(delete(ExecutionLog).where(ExecutionLog.id.in_(execution_ids)))
                session.commit()
                deleted_count += len(execution_ids)
            except Exception:
                session.rollback()
                raise


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
