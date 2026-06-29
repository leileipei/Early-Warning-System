import time
from collections.abc import Callable

from sqlmodel import Session, select

from app.db import get_engine, init_db
from app.execution_service import execute_rule_by_id
from app.models import AlertRule, TriggerType
from app.scheduler import build_scheduler


def build_execute_rule_callback(
    session_factory: Callable[[], Session] | type[Session] | None = None,
    execute_rule_by_id_fn: Callable[[Session, int, TriggerType], object] = execute_rule_by_id,
) -> Callable[[int], None]:
    def execute_rule(rule_id: int) -> None:
        factory = session_factory or (lambda: Session(get_engine()))
        with factory() as session:
            execute_rule_by_id_fn(session, rule_id, TriggerType.SCHEDULED)

    return execute_rule


def main() -> None:
    init_db()
    with Session(get_engine()) as session:
        rules = session.exec(select(AlertRule).where(AlertRule.enabled == True)).all()  # noqa: E712

    scheduler = build_scheduler(rules, build_execute_rule_callback())
    scheduler.start()
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        scheduler.shutdown()


if __name__ == "__main__":
    main()
