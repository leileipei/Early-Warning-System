import smtplib
from datetime import datetime

from sqlmodel import Session, select

from app.crypto import SecretCipher
from app.executor import ExecutionResult, RuleExecutor
from app.mailer import SmtpMailer
from app.models import (
    AlertRule,
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

    def client_factory():
        if config.use_ssl:
            client = smtplib.SMTP_SSL(config.host, config.port, timeout=config.timeout_seconds)
        else:
            client = smtplib.SMTP(config.host, config.port, timeout=config.timeout_seconds)
            if config.use_tls:
                client.starttls()

        client.login(config.username, password)
        return client

    return SmtpMailer(sender=config.sender, client_factory=client_factory)


def execute_rule_by_id(
    session: Session,
    rule_id: int,
    trigger_type: TriggerType = TriggerType.MANUAL,
) -> ExecutionLog:
    rule = session.get(AlertRule, rule_id)
    if rule is None:
        raise RuleNotFoundError(f"rule {rule_id} not found")

    started_at = datetime.utcnow()
    try:
        data_source = _get_enabled_data_source(session, rule)
        smtp_config = _get_enabled_smtp_config(session)
        executor = RuleExecutor(
            sql_client=build_sql_client(data_source),
            mailer=build_smtp_mailer(smtp_config),
            max_rows=rule.max_rows,
        )
        result = executor.execute(rule, trigger_type=trigger_type)
    except ConfigurationError as exc:
        result = ExecutionResult(
            status=ExecutionStatus.FAILED,
            error_type="ConfigurationError",
            error_message=str(exc),
        )
    except Exception as exc:
        result = ExecutionResult(
            status=ExecutionStatus.FAILED,
            error_type=type(exc).__name__,
            error_message=str(exc) or type(exc).__name__,
        )

    return persist_execution_result(
        session=session,
        rule=rule,
        trigger_type=trigger_type,
        result=result,
        started_at=started_at,
        finished_at=datetime.utcnow(),
    )


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
