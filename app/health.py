from dataclasses import dataclass

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlmodel import Session

from app.models import WorkerHeartbeat, utc_now

_REQUIRED_TABLES = {
    "adminuser",
    "alertrule",
    "executionlog",
    "maillog",
    "workerheartbeat",
}


@dataclass(frozen=True)
class ReadinessResult:
    ready: bool
    components: dict[str, str]


def check_readiness(engine: Engine, *, heartbeat_timeout_seconds: int) -> ReadinessResult:
    try:
        with Session(engine) as session:
            session.exec(text("SELECT 1")).one()
            tables = set(inspect(engine).get_table_names())
            if not _REQUIRED_TABLES.issubset(tables):
                return ReadinessResult(
                    False, {"database": "schema_incomplete", "worker": "unknown"}
                )
            heartbeat = session.get(WorkerHeartbeat, 1)
    except Exception:
        return ReadinessResult(False, {"database": "unavailable", "worker": "unknown"})

    if heartbeat is None:
        return ReadinessResult(False, {"database": "ready", "worker": "missing"})
    if (utc_now() - heartbeat.last_seen_at).total_seconds() > heartbeat_timeout_seconds:
        return ReadinessResult(False, {"database": "ready", "worker": "stale"})
    if not heartbeat.last_sync_ok:
        return ReadinessResult(False, {"database": "ready", "worker": "sync_failed"})
    return ReadinessResult(True, {"database": "ready", "worker": "ready"})
