from collections.abc import Callable, Iterable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.models import AlertRule


def valid_scheduled_rules(rules: Iterable[AlertRule]) -> list[AlertRule]:
    valid_rules = []
    for rule in rules:
        if not rule.enabled or rule.id is None:
            continue
        try:
            CronTrigger.from_crontab(rule.cron_expression)
        except ValueError:
            continue
        valid_rules.append(rule)
    return valid_rules


def build_scheduler(
    rules: Iterable[AlertRule],
    execute_rule: Callable[[int], None],
) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    for rule in valid_scheduled_rules(rules):
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
