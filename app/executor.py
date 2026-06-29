from dataclasses import dataclass

from app.mailer import EmailMessage
from app.models import AlertRule, ExecutionStatus, SendMode, TriggerType
from app.sql_validator import validate_select_sql
from app.template_renderer import render_per_row, render_summary


@dataclass(frozen=True)
class ExecutionResult:
    status: ExecutionStatus
    row_count: int
    email_count: int
    error_message: str = ""


class RuleExecutor:
    def __init__(self, sql_client, mailer):
        self.sql_client = sql_client
        self.mailer = mailer

    def execute(self, rule: AlertRule, trigger_type: TriggerType) -> ExecutionResult:
        try:
            validate_select_sql(rule.sql_text)
            query_result = self.sql_client.query(
                rule.sql_text,
                timeout_seconds=rule.query_timeout_seconds,
                max_rows=rule.max_rows,
            )
            rows = query_result.rows
            if not rows:
                return ExecutionResult(
                    status=ExecutionStatus.SUCCESS,
                    row_count=0,
                    email_count=0,
                )

            messages = self._build_messages(rule, rows)
            failures = 0
            for message in messages:
                send_result = self.mailer.send(message)
                if not send_result.success:
                    failures += 1

            if failures == len(messages):
                status = ExecutionStatus.FAILED
            elif failures:
                status = ExecutionStatus.PARTIAL_FAILED
            else:
                status = ExecutionStatus.SUCCESS

            return ExecutionResult(status=status, row_count=len(rows), email_count=len(messages))
        except Exception as exc:
            return ExecutionResult(
                status=ExecutionStatus.FAILED,
                row_count=0,
                email_count=0,
                error_message=str(exc),
            )

    def _build_messages(self, rule: AlertRule, rows: list[dict]) -> list[EmailMessage]:
        recipients = [email.strip() for email in rule.recipients.split(",") if email.strip()]
        cc = [email.strip() for email in rule.cc_recipients.split(",") if email.strip()]
        context = {"rule_name": rule.name}
        if rule.send_mode == SendMode.SUMMARY:
            rendered = render_summary(rule.subject_template, rule.body_template, rows, context)
            return [EmailMessage(recipients, cc, rendered.subject, rendered.html_body)]

        messages = []
        for row in rows:
            rendered = render_per_row(rule.subject_template, rule.body_template, row, context)
            messages.append(EmailMessage(recipients, cc, rendered.subject, rendered.html_body))
        return messages
