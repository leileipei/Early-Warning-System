import logging
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


def _job_id(rule_id: int) -> str:
    return f"rule-{rule_id}"


def _add_rule_job(
    scheduler,
    rule: AlertRule,
    execute_rule: Callable[[int], None],
) -> None:
    scheduler.add_job(
        execute_rule,
        trigger=CronTrigger.from_crontab(rule.cron_expression),
        args=[rule.id],
        id=_job_id(rule.id),
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )


class RuleScheduleSynchronizer:
    def __init__(self, scheduler, execute_rule: Callable[[int], None], logger=None):
        self.scheduler = scheduler
        self.execute_rule = execute_rule
        self.logger = logger or logging.getLogger(__name__)
        self.known_cron_by_rule_id: dict[int, str] = {}

    def sync(self, rules: Iterable[AlertRule]) -> None:
        desired = {rule.id: rule for rule in valid_scheduled_rules(rules)}

        for rule_id in set(self.known_cron_by_rule_id) - set(desired):
            try:
                if self.scheduler.get_job(_job_id(rule_id)) is not None:
                    self.scheduler.remove_job(_job_id(rule_id))
            except Exception:
                self.logger.exception("移除规则调度任务失败: rule_id=%s", rule_id)
            else:
                self.known_cron_by_rule_id.pop(rule_id, None)

        for rule_id, rule in desired.items():
            unchanged = self.known_cron_by_rule_id.get(rule_id) == rule.cron_expression
            if unchanged and self.scheduler.get_job(_job_id(rule_id)) is not None:
                continue
            try:
                if self.scheduler.get_job(_job_id(rule_id)) is not None:
                    self.scheduler.remove_job(_job_id(rule_id))
                _add_rule_job(self.scheduler, rule, self.execute_rule)
            except Exception:
                self.logger.exception("同步规则调度任务失败: rule_id=%s", rule_id)
            else:
                self.known_cron_by_rule_id[rule_id] = rule.cron_expression


def build_scheduler(
    rules: Iterable[AlertRule],
    execute_rule: Callable[[int], None],
) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    for rule in valid_scheduled_rules(rules):
        _add_rule_job(scheduler, rule, execute_rule)
    return scheduler
