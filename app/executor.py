from dataclasses import dataclass, field
from re import split
from typing import Protocol

from app.mailer import EmailMessage, MailSendResult, SmtpMailer
from app.models import AlertRule, ExecutionStatus, SendMode
from app.sql_client import SqlClient
from app.template_renderer import render_per_row, render_summary

try:
    from app.sql_validator import validate_select_only_sql
except ImportError:
    from app.sql_validator import validate_select_sql as validate_select_only_sql


class CompatibleMailer(Protocol):
    def send(self, message: EmailMessage) -> MailSendResult:
        raise NotImplementedError


@dataclass(frozen=True)
class ExecutionMailResult:
    message: EmailMessage
    result: MailSendResult


@dataclass(frozen=True)
class ExecutionResult:
    status: ExecutionStatus
    row_count: int = 0
    mail_count: int = 0
    error_message: str | None = None
    mail_results: list[ExecutionMailResult] = field(default_factory=list)


class RuleExecutor:
    def __init__(
        self,
        sql_client: SqlClient,
        mailer: SmtpMailer | CompatibleMailer,
        max_rows: int = 500,
    ):
        self.sql_client = sql_client
        self.mailer = mailer
        self.max_rows = max_rows

    def execute(self, rule: AlertRule, trigger_type=None) -> ExecutionResult:
        sql = _rule_sql(rule)
        try:
            validate_select_only_sql(sql)
        except Exception as exc:
            return ExecutionResult(status=ExecutionStatus.FAILED, error_message=str(exc))

        try:
            query_result = self.sql_client.query(
                sql,
                timeout_seconds=rule.query_timeout_seconds,
                max_rows=self.max_rows,
            )
        except Exception as exc:
            return ExecutionResult(status=ExecutionStatus.FAILED, error_message=str(exc))

        rows = query_result.rows
        if not rows:
            return ExecutionResult(status=ExecutionStatus.SUCCESS, row_count=0)

        try:
            messages = self._build_messages(rule, rows)
        except Exception as exc:
            return ExecutionResult(
                status=ExecutionStatus.FAILED,
                row_count=len(rows),
                error_message=str(exc),
            )

        mail_results = [self._send_message(message) for message in messages]
        successful_count = sum(1 for mail_result in mail_results if mail_result.result.success)
        status = self._status_for_mail_results(mail_results)
        error_message = self._combined_error_message(mail_results)

        return ExecutionResult(
            status=status,
            row_count=len(rows),
            mail_count=successful_count,
            error_message=error_message,
            mail_results=mail_results,
        )

    def _build_messages(self, rule: AlertRule, rows: list[dict]) -> list[EmailMessage]:
        recipients = _parse_recipients(rule.recipients)
        if not recipients:
            raise ValueError("recipients must not be empty")

        cc_recipients = _parse_recipients(rule.cc_recipients)
        context = {"rule_name": rule.name, "rows": rows, "row_count": len(rows)}

        if rule.send_mode == SendMode.SUMMARY:
            rendered = render_summary(rule.subject_template, rule.body_template, rows, context)
            return [
                EmailMessage(
                    recipients=recipients,
                    cc_recipients=cc_recipients,
                    subject=rendered.subject,
                    html_body=rendered.html_body,
                )
            ]

        messages = []
        for row in rows:
            rendered = render_per_row(rule.subject_template, rule.body_template, row, context)
            messages.append(
                EmailMessage(
                    recipients=recipients,
                    cc_recipients=cc_recipients,
                    subject=rendered.subject,
                    html_body=rendered.html_body,
                )
            )
        return messages

    def _send_message(self, message: EmailMessage) -> ExecutionMailResult:
        try:
            result = self.mailer.send(message)
        except Exception as exc:
            result = MailSendResult(success=False, error_message=str(exc))
        return ExecutionMailResult(message=message, result=result)

    def _status_for_mail_results(self, mail_results: list[ExecutionMailResult]) -> ExecutionStatus:
        successful_count = sum(1 for mail_result in mail_results if mail_result.result.success)
        if successful_count == len(mail_results):
            return ExecutionStatus.SUCCESS
        if successful_count == 0:
            return ExecutionStatus.FAILED
        return ExecutionStatus.PARTIAL_FAILED

    def _combined_error_message(self, mail_results: list[ExecutionMailResult]) -> str | None:
        errors = [
            mail_result.result.error_message
            for mail_result in mail_results
            if not mail_result.result.success and mail_result.result.error_message
        ]
        return "; ".join(errors) if errors else None


def _parse_recipients(value: str) -> list[str]:
    return [recipient.strip() for recipient in split(r"[,;]", value or "") if recipient.strip()]


def _rule_sql(rule: AlertRule) -> str:
    return getattr(rule, "sql_query", rule.sql_text)
