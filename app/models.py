from datetime import datetime
from enum import StrEnum
from typing import Optional

from sqlmodel import Field, SQLModel


def utc_now() -> datetime:
    return datetime.utcnow()


class SendMode(StrEnum):
    SUMMARY = "summary"
    PER_ROW = "per_row"


class ExecutionStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL_FAILED = "partial_failed"


class TriggerType(StrEnum):
    SCHEDULED = "scheduled"
    MANUAL = "manual"


class MailStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"


class AdminUser(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    password_hash: str
    created_at: datetime = Field(default_factory=utc_now)


class SqlDataSource(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    host: str
    port: int = 1433
    database: str
    username: str
    encrypted_password: str
    enabled: bool = True
    connect_timeout_seconds: int = 10
    odbc_driver: str = "ODBC Driver 18 for SQL Server"
    server_override: str = ""
    encrypt: str = "yes"
    trust_server_certificate: str = "yes"
    extra_params: str = ""
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class SmtpConfig(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    host: str
    port: int = 587
    username: str
    encrypted_password: str
    sender: str
    use_tls: bool = True
    use_ssl: bool = False
    timeout_seconds: int = 10
    enabled: bool = True
    updated_at: datetime = Field(default_factory=utc_now)


class AlertRule(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    data_source_id: int = Field(foreign_key="sqldatasource.id")
    sql_text: str
    cron_expression: str
    recipients: str
    cc_recipients: str = ""
    subject_template: str
    body_template: str
    send_mode: SendMode = SendMode.SUMMARY
    query_timeout_seconds: int = 30
    max_rows: int = 500
    enabled: bool = True
    notes: str = ""
    dynamic_recipient_field: str = ""
    dynamic_cc_field: str = ""
    suppress_duplicates: bool = False
    suppression_key_field: str = ""
    suppression_window_hours: int = 24
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AlertSuppression(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    rule_id: int = Field(foreign_key="alertrule.id", index=True)
    suppression_key: str = Field(index=True)
    first_seen_at: datetime = Field(default_factory=utc_now)
    last_seen_at: datetime = Field(default_factory=utc_now)
    hit_count: int = 1


class AlertRuleVersion(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    rule_id: int = Field(foreign_key="alertrule.id", index=True)
    version_number: int
    changed_by: str
    snapshot_json: str
    changed_at: datetime = Field(default_factory=utc_now)


class ExecutionLog(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    rule_id: int = Field(foreign_key="alertrule.id")
    trigger_type: TriggerType
    status: ExecutionStatus = ExecutionStatus.RUNNING
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: Optional[datetime] = None
    row_count: int = 0
    email_count: int = 0
    duration_ms: int = 0
    error_type: str = ""
    error_message: str = ""


class MailLog(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    execution_log_id: int = Field(foreign_key="executionlog.id")
    recipients: str
    cc_recipients: str = ""
    subject: str
    status: MailStatus
    error_message: str = ""
    sent_at: datetime = Field(default_factory=utc_now)
