import logging
import time
from collections.abc import Callable

from sqlmodel import Session, select

from app.db import get_engine, init_db
from app.execution_service import execute_rule_by_id
from app.models import AlertRule, TriggerType
from app.scheduler import RuleScheduleSynchronizer, build_scheduler
from app.settings import get_settings


logger = logging.getLogger(__name__)


def build_execute_rule_callback(
    session_factory: Callable[[], Session] | type[Session] | None = None,
    execute_rule_by_id_fn: Callable[[Session, int, TriggerType], object] = execute_rule_by_id,
) -> Callable[[int], None]:
    def execute_rule(rule_id: int) -> None:
        factory = session_factory or (lambda: Session(get_engine()))
        with factory() as session:
            execute_rule_by_id_fn(session, rule_id, TriggerType.SCHEDULED)

    return execute_rule


def sync_rules_once(synchronizer, session_factory=None, logger=None) -> bool:
    factory = session_factory or (lambda: Session(get_engine()))
    active_logger = logger or globals()["logger"]
    try:
        with factory() as session:
            rules = session.exec(select(AlertRule)).all()
    except Exception:
        active_logger.exception("读取预警规则失败，保留当前调度任务")
        return False

    synchronizer.sync(rules)
    return True


def run_sync_loop(
    scheduler,
    synchronizer,
    *,
    interval_seconds: float,
    sync_once=sync_rules_once,
    sleep_fn=time.sleep,
) -> None:
    scheduler_started = False
    try:
        sync_once(synchronizer)
        scheduler.start()
        scheduler_started = True
        while True:
            sleep_fn(interval_seconds)
            sync_once(synchronizer)
    except KeyboardInterrupt:
        if scheduler_started:
            scheduler.shutdown()


def main() -> None:
    init_db()
    settings = get_settings()
    execute_rule = build_execute_rule_callback()
    scheduler = build_scheduler([], execute_rule)
    synchronizer = RuleScheduleSynchronizer(scheduler, execute_rule)
    run_sync_loop(
        scheduler,
        synchronizer,
        interval_seconds=settings.scheduler_sync_interval_seconds,
    )


if __name__ == "__main__":
    main()
