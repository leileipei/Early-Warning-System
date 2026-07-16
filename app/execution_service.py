import smtplib
import ssl
import time
from dataclasses import replace
from datetime import datetime, timedelta

from sqlmodel import Session, select

from app.crypto import SecretCipher
from app.execution_lock import rule_execution_lease
from app.executor import ExecutionResult, RuleExecutor
from app.mailer import SmtpMailer
from app.models import (
    AlertRule,
    AlertSuppression,
    ExecutionLog,
    ExecutionStatus,
    MailLog,
    MailStatus,
    SmtpConfig,
    SqlDataSource,
    TriggerType,
)
from app.settings import get_settings
from app.sql_client import PyodbcSqlServerClient


class RuleNotFoundError(Exception):
    pass


class ConfigurationError(Exception):
    pass


def build_sql_client(data_source: SqlDataSource) -> PyodbcSqlServerClient:
    password = _cipher().decrypt(data_source.encrypted_password)
    return PyodbcSqlServerClient(
        host=data_source.host,
        port=data_source.port,
        database=data_source.database,
        username=data_source.username,
        password=password,
        connect_timeout_seconds=data_source.connect_timeout_seconds,
        odbc_driver=data_source.odbc_driver,
        server_override=data_source.server_override,
        encrypt=data_source.encrypt,
        trust_server_certificate=data_source.trust_server_certificate,
        extra_params=data_source.extra_params,
    )


def build_smtp_mailer(config: SmtpConfig) -> SmtpMailer:
    password = _cipher().decrypt(config.encrypted_password)
    tls_context = ssl.create_default_context()

    def client_factory():
        if config.use_ssl:
            client = smtplib.SMTP_SSL(
                config.host,
                config.port,
                timeout=config.timeout_seconds,
                context=tls_context,
            )
        else:
            client = smtplib.SMTP(config.host, config.port, timeout=config.timeout_seconds)
            if config.use_tls:
                client.starttls(context=tls_context)

        client.login(config.username, password)
        return client

    return SmtpMailer(sender=config.sender, client_factory=client_factory)


def execute_rule_by_id(
    session: Session,
    rule_id: int,
    trigger_type: TriggerType = TriggerType.MANUAL,
    *,
    max_attempts: int = 3,
    retry_delay_seconds: float = 1.0,
    sleep_fn=time.sleep,
) -> ExecutionLog:
    rule = session.get(AlertRule, rule_id)
    if rule is None or rule.archived_at is not None:
        raise RuleNotFoundError(f"rule {rule_id} not found")

    with rule_execution_lease(
        session,
        rule_id,
        lease_seconds=get_settings().rule_execution_lease_seconds,
    ):
        started_at = datetime.utcnow()
        total_attempts = max(1, max_attempts)
        result = None
        attempts_used = 0
        for attempt in range(1, total_attempts + 1):
            attempts_used = attempt
            result = _execute_rule_once(session, rule, trigger_type)
            if not _is_retryable_result(result) or attempt == total_attempts:
                break
            if retry_delay_seconds > 0:
                sleep_fn(retry_delay_seconds)

        if result is None:
            result = ExecutionResult(
                status=ExecutionStatus.FAILED,
                error_type="RuntimeError",
                error_message="规则执行失败",
            )

        result = _with_exhausted_retry_message(result, attempts_used)
        return persist_execution_result(
            session=session,
            rule=rule,
            trigger_type=trigger_type,
            result=result,
            started_at=started_at,
            finished_at=datetime.utcnow(),
        )


def _execute_rule_once(session: Session, rule: AlertRule, trigger_type: TriggerType) -> ExecutionResult:
    try:
        data_source = _get_enabled_data_source(session, rule)
        smtp_config = _get_enabled_smtp_config(session)
        suppression_filter, suppression_state = _build_suppression_filter(session, rule)
        executor = RuleExecutor(
            sql_client=build_sql_client(data_source),
            mailer=build_smtp_mailer(smtp_config),
            max_rows=rule.max_rows,
        )
        result = executor.execute(
            rule,
            trigger_type=trigger_type,
            row_filter=suppression_filter,
        )
        if suppression_state is not None and result.status == ExecutionStatus.SUCCESS:
            _persist_suppression_state(session, rule, suppression_state)
    except ConfigurationError as exc:
        result = ExecutionResult(
            status=ExecutionStatus.FAILED,
            error_type="ConfigurationError",
            error_message=str(exc),
        )
    except Exception as exc:
        return ExecutionResult(
            status=ExecutionStatus.FAILED,
            error_type=type(exc).__name__,
            error_message=str(exc) or type(exc).__name__,
        )

    return result


def _build_suppression_filter(session: Session, rule: AlertRule):
    if not rule.suppress_duplicates or not rule.suppression_key_field:
        return None, None

    now = datetime.utcnow()
    cutoff = now - timedelta(hours=rule.suppression_window_hours)
    state = {"new_keys": [], "suppressed_keys": [], "now": now}
    seen_in_run = set()

    def filter_rows(rows: list[dict]) -> list[dict]:
        filtered_rows = []
        for row in rows:
            key = _row_suppression_key(row, rule.suppression_key_field)
            if not key:
                filtered_rows.append(row)
                continue

            suppression = _get_suppression_record(session, rule.id, key)
            if key in seen_in_run or (suppression is not None and suppression.last_seen_at >= cutoff):
                state["suppressed_keys"].append(key)
                continue

            seen_in_run.add(key)
            state["new_keys"].append(key)
            filtered_rows.append(row)
        return filtered_rows

    return filter_rows, state


def _row_suppression_key(row: dict, field_name: str) -> str:
    value = row.get(field_name)
    if value is None:
        return ""
    return str(value).strip()


def _get_suppression_record(session: Session, rule_id: int, key: str) -> AlertSuppression | None:
    return session.exec(
        select(AlertSuppression).where(
            AlertSuppression.rule_id == rule_id,
            AlertSuppression.suppression_key == key,
        )
    ).first()


def _persist_suppression_state(session: Session, rule: AlertRule, state: dict) -> None:
    now = state["now"]
    for key in [*state["new_keys"], *state["suppressed_keys"]]:
        suppression = _get_suppression_record(session, rule.id, key)
        if suppression is None:
            suppression = AlertSuppression(
                rule_id=rule.id,
                suppression_key=key,
                first_seen_at=now,
                last_seen_at=now,
                hit_count=1,
            )
        else:
            suppression.last_seen_at = now
            suppression.hit_count += 1
        session.add(suppression)
    session.commit()


def _is_retryable_result(result: ExecutionResult) -> bool:
    if result.status != ExecutionStatus.FAILED:
        return False
    return result.error_type in {
        "ConnectionError",
        "MailSendError",
        "OSError",
        "RuntimeError",
        "TimeoutError",
    }


def _with_exhausted_retry_message(result: ExecutionResult, attempts_used: int) -> ExecutionResult:
    retry_count = attempts_used - 1
    if retry_count <= 0 or not _is_retryable_result(result):
        return result
    message = result.error_message or result.error_type or "规则执行失败"
    return replace(result, error_message=f"{message}（已重试 {retry_count} 次）")


def persist_execution_result(
    *,
    session: Session,
    rule: AlertRule,
    trigger_type: TriggerType,
    result: ExecutionResult,
    started_at: datetime,
    finished_at: datetime,
) -> ExecutionLog:
    execution_log = ExecutionLog(
        rule_id=rule.id,
        trigger_type=trigger_type,
        status=result.status,
        started_at=started_at,
        finished_at=finished_at,
        row_count=result.row_count,
        email_count=result.email_count,
        duration_ms=max(0, int((finished_at - started_at).total_seconds() * 1000)),
        error_type=result.error_type or "",
        error_message=result.error_message or "",
    )
    session.add(execution_log)
    session.commit()
    session.refresh(execution_log)

    for mail_result in result.mail_results:
        mail_log = MailLog(
            execution_log_id=execution_log.id,
            recipients=",".join(mail_result.message.recipients),
            cc_recipients=",".join(mail_result.message.cc_recipients),
            subject=mail_result.message.subject,
            status=MailStatus.SUCCESS if mail_result.result.success else MailStatus.FAILED,
            error_message=mail_result.result.error_message or "",
        )
        session.add(mail_log)

    session.commit()
    session.refresh(execution_log)
    return execution_log


def _get_enabled_data_source(session: Session, rule: AlertRule) -> SqlDataSource:
    data_source = session.get(SqlDataSource, rule.data_source_id)
    if data_source is None:
        raise ConfigurationError("规则关联的数据源不存在")
    if not data_source.enabled:
        raise ConfigurationError("规则关联的数据源已禁用")
    return data_source


def _get_enabled_smtp_config(session: Session) -> SmtpConfig:
    smtp_config = session.exec(
        select(SmtpConfig)
        .where(SmtpConfig.enabled == True)  # noqa: E712
        .order_by(SmtpConfig.updated_at.desc())
    ).first()
    if smtp_config is None:
        raise ConfigurationError("未配置可用的 SMTP 服务")
    return smtp_config


def _cipher() -> SecretCipher:
    return SecretCipher.from_key_material(get_settings().secret_key)
