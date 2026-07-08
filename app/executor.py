from collections.abc import Callable
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
    error_type: str = ""
    error_message: str | None = None
    mail_results: list[ExecutionMailResult] = field(default_factory=list)

    @property
    def email_count(self) -> int:
        return self.mail_count


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

    def execute(
        self,
        rule: AlertRule,
        trigger_type=None,
        row_filter: Callable[[list[dict]], list[dict]] | None = None,
    ) -> ExecutionResult:
        sql = _rule_sql(rule)
        try:
            validate_select_only_sql(sql)
        except Exception as exc:
            return ExecutionResult(
                status=ExecutionStatus.FAILED,
                error_type=type(exc).__name__,
                error_message=_exception_message(exc),
            )

        try:
            query_result = self.sql_client.query(
                sql,
                timeout_seconds=rule.query_timeout_seconds,
                max_rows=_rule_max_rows(rule, self.max_rows),
            )
        except Exception as exc:
            return ExecutionResult(
                status=ExecutionStatus.FAILED,
                error_type=type(exc).__name__,
                error_message=_exception_message(exc),
            )

        rows = query_result.rows
        row_count = len(rows)
        if row_filter is not None:
            rows = row_filter(rows)
        if not rows:
            return ExecutionResult(status=ExecutionStatus.SUCCESS, row_count=row_count)

        try:
            messages = self._build_messages(rule, rows)
        except Exception as exc:
            return ExecutionResult(
                status=ExecutionStatus.FAILED,
                row_count=row_count,
                error_type=type(exc).__name__,
                error_message=_exception_message(exc),
            )

        mail_results = [self._send_message(message) for message in messages]
        successful_count = sum(1 for mail_result in mail_results if mail_result.result.success)
        status = self._status_for_mail_results(mail_results)
        has_failure = successful_count < len(mail_results)
        error_message = self._combined_error_message(mail_results) if has_failure else None

        return ExecutionResult(
            status=status,
            row_count=row_count,
            mail_count=successful_count,
            error_type="MailSendError" if has_failure else "",
            error_message=error_message,
            mail_results=mail_results,
        )

    def _build_messages(self, rule: AlertRule, rows: list[dict]) -> list[EmailMessage]:
        fixed_recipients = _parse_recipients(rule.recipients)
        fixed_cc_recipients = _parse_recipients(rule.cc_recipients)
        context = {"rule_name": rule.name, "rows": rows, "row_count": len(rows)}

        if rule.send_mode == SendMode.SUMMARY:
            if not fixed_recipients:
                raise ValueError("recipients must not be empty")
            rendered = render_summary(rule.subject_template, rule.body_template, rows, context)
            return [
                EmailMessage(
                    recipients=fixed_recipients,
                    cc_recipients=fixed_cc_recipients,
                    subject=rendered.subject,
                    html_body=rendered.html_body,
                )
            ]

        messages = []
        for row in rows:
            rendered = render_per_row(rule.subject_template, rule.body_template, row, context)
            recipients = _row_recipients(row, rule.dynamic_recipient_field) or fixed_recipients
            if not recipients:
                raise ValueError("recipients must not be empty")
            cc_recipients = _row_recipients(row, rule.dynamic_cc_field) or fixed_cc_recipients
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
            result = MailSendResult(success=False, error_message=_exception_message(exc))
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
        return "; ".join(errors) if errors else "one or more emails failed"


def _parse_recipients(value: str) -> list[str]:
    return [recipient.strip() for recipient in split(r"[,;]", value or "") if recipient.strip()]


def _row_recipients(row: dict, field_name: str) -> list[str]:
    if not field_name:
        return []
    value = row.get(field_name)
    if value is None:
        return []
    return _parse_recipients(str(value))


def _rule_sql(rule: AlertRule) -> str:
    return getattr(rule, "sql_query", rule.sql_text)


def _rule_max_rows(rule: AlertRule, fallback: int) -> int:
    return getattr(rule, "max_rows", fallback)


def _exception_message(exc: Exception) -> str:
    return str(exc) or type(exc).__name__
