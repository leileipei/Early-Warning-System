import re
from datetime import UTC, datetime

from sqlmodel import Session

from app.models import WorkerHeartbeat, utc_now

_ERROR_DESCRIPTION = "worker synchronization failed"
_ERROR_TYPE_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")


def summarize_worker_error(exc: BaseException) -> str:
    return _format_error_summary(type(exc).__name__)


def record_worker_start(
    session: Session,
    worker_id: str,
    *,
    now: datetime | None = None,
) -> None:
    timestamp = _naive_utc(now)
    heartbeat = session.get(WorkerHeartbeat, 1)
    if heartbeat is None:
        heartbeat = WorkerHeartbeat(
            id=1,
            worker_id=worker_id,
            started_at=timestamp,
            last_seen_at=timestamp,
        )
        session.add(heartbeat)
    else:
        heartbeat.worker_id = worker_id
        heartbeat.started_at = timestamp
        heartbeat.last_seen_at = timestamp
        heartbeat.last_sync_ok = True
        heartbeat.last_error = ""
    _commit(session)


def record_worker_sync(
    session: Session,
    worker_id: str,
    *,
    ok: bool,
    error: str = "",
    now: datetime | None = None,
) -> None:
    timestamp = _naive_utc(now)
    heartbeat = session.get(WorkerHeartbeat, 1)
    if heartbeat is None:
        heartbeat = WorkerHeartbeat(
            id=1,
            worker_id=worker_id,
            started_at=timestamp,
            last_seen_at=timestamp,
        )
        session.add(heartbeat)
    else:
        heartbeat.worker_id = worker_id
        heartbeat.last_seen_at = timestamp

    heartbeat.last_sync_ok = ok
    heartbeat.last_error = "" if ok else _summarize_error_text(error)
    _commit(session)


def _naive_utc(now: datetime | None) -> datetime:
    if now is None:
        return utc_now()
    if now.tzinfo is None:
        return now
    return now.astimezone(UTC).replace(tzinfo=None)


def _summarize_error_text(error: str) -> str:
    error_type, _, _ = error.partition(":")
    if _ERROR_TYPE_PATTERN.fullmatch(error_type.strip()):
        return _format_error_summary(error_type.strip())
    return _format_error_summary("WorkerSyncError")


def _format_error_summary(error_type: str) -> str:
    suffix = f": {_ERROR_DESCRIPTION}"
    return f"{error_type[: 300 - len(suffix)]}{suffix}"


def _commit(session: Session) -> None:
    try:
        session.commit()
    except BaseException:
        session.rollback()
        raise
