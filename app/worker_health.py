from datetime import UTC, datetime

from sqlmodel import Session

from app.models import WorkerHeartbeat, utc_now

WORKER_SYNC_FAILURE = "Worker 同步失败，请检查数据库连接和调度配置"


def summarize_worker_error(exc: BaseException) -> str:
    _ = exc
    return WORKER_SYNC_FAILURE


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
    _ = error
    return WORKER_SYNC_FAILURE


def _commit(session: Session) -> None:
    try:
        session.commit()
    except BaseException:
        session.rollback()
        raise
