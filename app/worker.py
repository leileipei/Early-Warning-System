import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4

from sqlmodel import Session, select

from app.db import get_engine, init_db
from app.error_reporting import log_exception_safely, public_error_summary
from app.execution_lock import RuleExecutionInProgressError
from app.execution_service import execute_rule_by_id
from app.log_service import cleanup_expired_logs
from app.models import AlertRule, TriggerType
from app.scheduler import RuleScheduleSynchronizer, build_scheduler
from app.settings import get_settings
from app.worker_health import record_worker_start, record_worker_sync


logger = logging.getLogger(__name__)
WORKER_SYNC_FAILURE = "Worker 同步失败，请检查数据库连接和调度配置"


@dataclass(frozen=True)
class RuleSyncResult:
    ok: bool
    error: str = ""


def build_execute_rule_callback(
    session_factory: Callable[[], Session] | type[Session] | None = None,
    execute_rule_by_id_fn: Callable[[Session, int, TriggerType], object] = execute_rule_by_id,
) -> Callable[[int], None]:
    def execute_rule(rule_id: int) -> None:
        factory = session_factory or (lambda: Session(get_engine()))
        with factory() as session:
            try:
                execute_rule_by_id_fn(session, rule_id, TriggerType.SCHEDULED)
            except RuleExecutionInProgressError:
                logger.warning("规则正在执行，跳过本次调度: rule_id=%s", rule_id)

    return execute_rule


def sync_rules_once(
    synchronizer,
    session_factory: Callable[[], Session] | None = None,
    logger=None,
) -> RuleSyncResult:
    factory = session_factory or (lambda: Session(get_engine()))
    active_logger = logger or globals()["logger"]
    try:
        with factory() as session:
            rules = session.exec(select(AlertRule).where(AlertRule.archived_at.is_(None))).all()
    except Exception as exc:
        log_exception_safely(active_logger, "读取预警规则失败，保留当前调度任务", exc)
        return RuleSyncResult(ok=False, error=public_error_summary(exc, fallback=WORKER_SYNC_FAILURE))

    try:
        synchronizer.sync(rules)
    except Exception as exc:
        log_exception_safely(active_logger, "同步预警规则失败，保留当前调度任务", exc)
        return RuleSyncResult(ok=False, error=public_error_summary(exc, fallback=WORKER_SYNC_FAILURE))
    return RuleSyncResult(ok=True)


def _record_sync_heartbeat(
    session_factory: Callable[[], Session],
    record_sync: Callable[..., None],
    worker_id: str,
    result: RuleSyncResult,
) -> None:
    try:
        with session_factory() as session:
            record_sync(session, worker_id, ok=result.ok, error=result.error)
    except Exception as exc:
        log_exception_safely(logger, "记录 Worker 心跳失败", exc)


def _record_start_heartbeat(
    session_factory: Callable[[], Session], worker_id: str
) -> None:
    try:
        with session_factory() as session:
            record_worker_start(session, worker_id)
    except Exception as exc:
        log_exception_safely(logger, "记录 Worker 启动心跳失败", exc)


def _cleanup_logs_once(cleanup_logs: Callable[[], object]) -> None:
    try:
        cleanup_logs()
    except Exception as exc:
        log_exception_safely(logger, "清理过期日志失败", exc)


def run_sync_loop(
    scheduler,
    synchronizer,
    *,
    interval_seconds: float,
    worker_id: str | None = None,
    session_factory: Callable[[], Session] | None = None,
    sync_once=sync_rules_once,
    record_sync: Callable[..., None] = record_worker_sync,
    cleanup_logs: Callable[[], object] | None = None,
    cleanup_interval_seconds: float | None = None,
    sleep_fn=time.sleep,
    monotonic_fn=time.monotonic,
) -> None:
    heartbeat_session_factory = session_factory or (lambda: Session(get_engine()))
    active_cleanup_logs = cleanup_logs or (lambda: None)
    active_cleanup_interval = cleanup_interval_seconds or float("inf")
    scheduler_started = False
    try:
        _cleanup_logs_once(active_cleanup_logs)
        next_cleanup_at = monotonic_fn() + active_cleanup_interval
        result = sync_once(synchronizer)
        if worker_id is not None:
            _record_sync_heartbeat(heartbeat_session_factory, record_sync, worker_id, result)
        scheduler.start()
        scheduler_started = True
        while True:
            sleep_fn(interval_seconds)
            now = monotonic_fn()
            if now >= next_cleanup_at:
                _cleanup_logs_once(active_cleanup_logs)
                next_cleanup_at = now + active_cleanup_interval
            result = sync_once(synchronizer)
            if worker_id is not None:
                _record_sync_heartbeat(heartbeat_session_factory, record_sync, worker_id, result)
    except KeyboardInterrupt:
        if scheduler_started:
            scheduler.shutdown()


def main() -> None:
    init_db()
    settings = get_settings()
    worker_id = uuid4().hex

    def session_factory() -> Session:
        return Session(get_engine())

    def cleanup_logs() -> int:
        return cleanup_expired_logs(
            session_factory,
            retention_days=settings.log_retention_days,
        )

    _record_start_heartbeat(session_factory, worker_id)
    execute_rule = build_execute_rule_callback()
    scheduler = build_scheduler(
        [],
        execute_rule,
        misfire_grace_seconds=settings.scheduler_misfire_grace_seconds,
    )
    synchronizer = RuleScheduleSynchronizer(
        scheduler,
        execute_rule,
        misfire_grace_seconds=settings.scheduler_misfire_grace_seconds,
    )
    run_sync_loop(
        scheduler,
        synchronizer,
        interval_seconds=settings.scheduler_sync_interval_seconds,
        worker_id=worker_id,
        session_factory=session_factory,
        cleanup_logs=cleanup_logs,
        cleanup_interval_seconds=settings.log_cleanup_interval_seconds,
    )


if __name__ == "__main__":
    main()
