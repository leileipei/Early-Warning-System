from collections.abc import Callable, Iterable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.models import AlertRule


def build_scheduler(
    rules: Iterable[AlertRule],
    execute_rule: Callable[[int], None],
) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    for rule in rules:
        if not rule.enabled or rule.id is None:
            continue
        trigger = CronTrigger.from_crontab(rule.cron_expression)
        scheduler.add_job(
            execute_rule,
            trigger=trigger,
            args=[rule.id],
            id=f"rule-{rule.id}",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    return scheduler
