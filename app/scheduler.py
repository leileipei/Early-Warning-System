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


def _rule_id_from_job_id(job_id: str) -> int | None:
    if not job_id.startswith("rule-"):
        return None
    try:
        return int(job_id.removeprefix("rule-"))
    except ValueError:
        return None


def _cron_signature(cron_expression: str) -> str:
    return str(CronTrigger.from_crontab(cron_expression))


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
        for job in scheduler.get_jobs():
            rule_id = _rule_id_from_job_id(job.id)
            if rule_id is not None:
                self.known_cron_by_rule_id[rule_id] = str(job.trigger)

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
            try:
                job = self.scheduler.get_job(_job_id(rule_id))
                cron_signature = _cron_signature(rule.cron_expression)
                unchanged = self.known_cron_by_rule_id.get(rule_id) == cron_signature
                if unchanged and job is not None:
                    continue
                if job is not None:
                    self.scheduler.remove_job(_job_id(rule_id))
                _add_rule_job(self.scheduler, rule, self.execute_rule)
            except Exception:
                self.logger.exception("同步规则调度任务失败: rule_id=%s", rule_id)
            else:
                self.known_cron_by_rule_id[rule_id] = cron_signature


def build_scheduler(
    rules: Iterable[AlertRule],
    execute_rule: Callable[[int], None],
) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    for rule in valid_scheduled_rules(rules):
        _add_rule_job(scheduler, rule, execute_rule)
    return scheduler
