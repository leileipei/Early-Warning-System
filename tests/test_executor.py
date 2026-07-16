import pytest
from sqlmodel import select

import app.execution_service as execution_service
from app.executor import RuleExecutor
from app.mailer import MailSendResult
from app.models import (
    AlertRule,
    AlertSuppression,
    ExecutionLog,
    ExecutionStatus,
    MailLog,
    MailStatus,
    SendMode,
    SmtpConfig,
    SqlDataSource,
    TriggerType,
    utc_now,
)


class FakeSqlClient:
    def __init__(self, rows=None, error=None):
        self.rows = rows or []
        self.error = error
        self.calls = []

    def query(self, sql, timeout_seconds, max_rows):
        self.calls.append(
            {
                "sql": sql,
                "timeout_seconds": timeout_seconds,
                "max_rows": max_rows,
            }
        )
        if self.error is not None:
            raise self.error

        from app.sql_client import QueryResult

        return QueryResult(rows=self.rows)


class SequenceSqlClient:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def query(self, sql, timeout_seconds, max_rows):
        self.calls.append(
            {
                "sql": sql,
                "timeout_seconds": timeout_seconds,
                "max_rows": max_rows,
            }
        )
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome

        from app.sql_client import QueryResult

        return QueryResult(rows=outcome)


class FakeMailer:
    def __init__(self, results=None, error=None):
        self.messages = []
        self.results = results or []
        self.error = error

    def send(self, message):
        self.messages.append(message)
        if self.error is not None:
            raise self.error
        if self.results:
            return self.results.pop(0)
        return MailSendResult(success=True)


class FakeCipher:
    def __init__(self, plaintext):
        self.plaintext = plaintext

    def decrypt(self, encrypted_value):
        return self.plaintext


class RecordingSmtpClient:
    def __init__(self):
        self.starttls_context = None

    def starttls(self, *, context):
        self.starttls_context = context

    def login(self, username, password):
        return None


@pytest.mark.parametrize("use_ssl", [False, True])
def test_build_smtp_mailer_uses_verified_tls_context(monkeypatch, use_ssl):
    from ssl import CERT_REQUIRED

    captured = {}
    client = RecordingSmtpClient()

    def fake_smtp(*args, **kwargs):
        captured.update(kwargs)
        return client

    monkeypatch.setattr(execution_service.smtplib, "SMTP", fake_smtp)
    monkeypatch.setattr(execution_service.smtplib, "SMTP_SSL", fake_smtp)
    monkeypatch.setattr(execution_service, "_cipher", lambda: FakeCipher("secret"))
    config = SmtpConfig(
        host="smtp.example.com",
        port=465 if use_ssl else 587,
        username="mailer",
        encrypted_password="encrypted",
        sender="alerts@example.com",
        use_ssl=use_ssl,
        use_tls=not use_ssl,
    )

    mailer = execution_service.build_smtp_mailer(config)
    mailer.client_factory()

    context = captured["context"] if use_ssl else client.starttls_context
    assert context.verify_mode == CERT_REQUIRED
    assert context.check_hostname is True


def make_rule(send_mode=SendMode.SUMMARY, **overrides):
    data = {
        "id": 1,
        "name": "大额订单",
        "data_source_id": 1,
        "sql_text": "select id, amount from orders",
        "cron_expression": "0 9 * * *",
        "recipients": "ops@example.com",
        "subject_template": "预警 {{rule_name}}",
        "body_template": "{{table}}",
        "send_mode": send_mode,
        "enabled": True,
    }
    data.update(overrides)
    return AlertRule(**data)


def persist_data_source(session, *, enabled=True):
    data_source = SqlDataSource(
        name="生产库",
        host="db.example.com",
        port=1433,
        database="erp",
        username="readonly",
        encrypted_password="encrypted",
        enabled=enabled,
    )
    session.add(data_source)
    session.commit()
    session.refresh(data_source)
    return data_source


def persist_smtp_config(session):
    smtp_config = SmtpConfig(
        host="smtp.example.com",
        port=587,
        username="mailer",
        encrypted_password="encrypted",
        sender="alerts@example.com",
        enabled=True,
    )
    session.add(smtp_config)
    session.commit()
    session.refresh(smtp_config)
    return smtp_config


def persist_rule(session, data_source, **overrides):
    rule = make_rule(data_source_id=data_source.id, **overrides)
    session.add(rule)
    session.commit()
    session.refresh(rule)
    return rule


def test_summary_mode_sends_one_email_with_all_rows():
    mailer = FakeMailer()
    sql_client = FakeSqlClient([{"id": 1, "amount": 100}, {"id": 2, "amount": 200}])
    executor = RuleExecutor(sql_client=sql_client, mailer=mailer)

    result = executor.execute(make_rule(max_rows=25))

    assert result.status == ExecutionStatus.SUCCESS
    assert result.row_count == 2
    assert result.mail_count == 1
    assert result.email_count == 1
    assert len(mailer.messages) == 1
    assert sql_client.calls == [
        {
            "sql": "select id, amount from orders",
            "timeout_seconds": 30,
            "max_rows": 25,
        }
    ]
    assert "100" in mailer.messages[0].html_body
    assert "200" in mailer.messages[0].html_body
    assert result.mail_results[0].message == mailer.messages[0]
    assert result.mail_results[0].result.success is True


def test_per_row_mode_sends_one_email_per_row():
    mailer = FakeMailer()
    executor = RuleExecutor(
        sql_client=FakeSqlClient([{"id": 1, "amount": 100}, {"id": 2, "amount": 200}]),
        mailer=mailer,
    )

    result = executor.execute(
        make_rule(
            SendMode.PER_ROW,
            subject_template="订单 {{id}}",
            body_template="金额 {{amount}}",
        )
    )

    assert result.status == ExecutionStatus.SUCCESS
    assert result.row_count == 2
    assert result.mail_count == 2
    assert [message.subject for message in mailer.messages] == ["订单 1", "订单 2"]
    assert [message.html_body for message in mailer.messages] == ["金额 100", "金额 200"]


def test_per_row_mode_uses_dynamic_recipients_from_row():
    mailer = FakeMailer()
    executor = RuleExecutor(
        sql_client=FakeSqlClient(
            [
                {
                    "id": 1,
                    "amount": 100,
                    "owner_email": "owner@example.com; backup@example.com",
                    "manager_email": "manager@example.com",
                }
            ]
        ),
        mailer=mailer,
    )

    result = executor.execute(
        make_rule(
            SendMode.PER_ROW,
            recipients="fallback@example.com",
            cc_recipients="team@example.com",
            dynamic_recipient_field="owner_email",
            dynamic_cc_field="manager_email",
            subject_template="订单 {{id}}",
            body_template="金额 {{amount}}",
        )
    )

    assert result.status == ExecutionStatus.SUCCESS
    assert result.mail_count == 1
    assert mailer.messages[0].recipients == ["owner@example.com", "backup@example.com"]
    assert mailer.messages[0].cc_recipients == ["manager@example.com"]


def test_per_row_mode_allows_dynamic_recipients_without_fixed_fallback():
    mailer = FakeMailer()
    executor = RuleExecutor(
        sql_client=FakeSqlClient([{"id": 1, "amount": 100, "owner_email": "owner@example.com"}]),
        mailer=mailer,
    )

    result = executor.execute(
        make_rule(
            SendMode.PER_ROW,
            recipients="",
            dynamic_recipient_field="owner_email",
            subject_template="订单 {{id}}",
            body_template="金额 {{amount}}",
        )
    )

    assert result.status == ExecutionStatus.SUCCESS
    assert mailer.messages[0].recipients == ["owner@example.com"]


def test_per_row_mode_falls_back_to_fixed_recipients_when_dynamic_field_empty():
    mailer = FakeMailer()
    executor = RuleExecutor(
        sql_client=FakeSqlClient([{"id": 1, "amount": 100, "owner_email": "", "manager_email": ""}]),
        mailer=mailer,
    )

    result = executor.execute(
        make_rule(
            SendMode.PER_ROW,
            recipients="fallback@example.com",
            cc_recipients="team@example.com",
            dynamic_recipient_field="owner_email",
            dynamic_cc_field="manager_email",
            subject_template="订单 {{id}}",
            body_template="金额 {{amount}}",
        )
    )

    assert result.status == ExecutionStatus.SUCCESS
    assert mailer.messages[0].recipients == ["fallback@example.com"]
    assert mailer.messages[0].cc_recipients == ["team@example.com"]


def test_summary_mode_ignores_dynamic_recipient_fields():
    mailer = FakeMailer()
    executor = RuleExecutor(
        sql_client=FakeSqlClient(
            [{"id": 1, "amount": 100, "owner_email": "owner@example.com", "manager_email": "manager@example.com"}]
        ),
        mailer=mailer,
    )

    result = executor.execute(
        make_rule(
            SendMode.SUMMARY,
            recipients="ops@example.com",
            cc_recipients="team@example.com",
            dynamic_recipient_field="owner_email",
            dynamic_cc_field="manager_email",
        )
    )

    assert result.status == ExecutionStatus.SUCCESS
    assert mailer.messages[0].recipients == ["ops@example.com"]
    assert mailer.messages[0].cc_recipients == ["team@example.com"]


def test_per_row_mode_fails_when_dynamic_and_fixed_recipients_empty():
    mailer = FakeMailer()
    executor = RuleExecutor(
        sql_client=FakeSqlClient([{"id": 1, "amount": 100, "owner_email": ""}]),
        mailer=mailer,
    )

    result = executor.execute(
        make_rule(
            SendMode.PER_ROW,
            recipients="",
            dynamic_recipient_field="owner_email",
            subject_template="订单 {{id}}",
            body_template="金额 {{amount}}",
        )
    )

    assert result.status == ExecutionStatus.FAILED
    assert "recipients" in result.error_message
    assert mailer.messages == []


def test_no_rows_succeeds_without_sending_email():
    mailer = FakeMailer()
    executor = RuleExecutor(sql_client=FakeSqlClient([]), mailer=mailer)

    result = executor.execute(make_rule())

    assert result.status == ExecutionStatus.SUCCESS
    assert result.row_count == 0
    assert result.mail_count == 0
    assert result.mail_results == []
    assert mailer.messages == []


def test_invalid_sql_fails_before_query_or_email():
    mailer = FakeMailer()
    sql_client = FakeSqlClient([{"id": 1}])
    executor = RuleExecutor(sql_client=sql_client, mailer=mailer)

    result = executor.execute(make_rule(sql_text="delete from orders"))

    assert result.status == ExecutionStatus.FAILED
    assert result.row_count == 0
    assert result.mail_count == 0
    assert "SELECT" in result.error_message
    assert sql_client.calls == []
    assert mailer.messages == []


def test_sql_client_exception_fails():
    mailer = FakeMailer()
    executor = RuleExecutor(sql_client=FakeSqlClient(error=RuntimeError("query timed out")), mailer=mailer)

    result = executor.execute(make_rule())

    assert result.status == ExecutionStatus.FAILED
    assert result.row_count == 0
    assert result.mail_count == 0
    assert result.error_type == "RuntimeError"
    assert result.error_message == "query timed out"
    assert mailer.messages == []


def test_exception_without_message_records_error_type_as_message():
    mailer = FakeMailer()
    executor = RuleExecutor(sql_client=FakeSqlClient(error=RuntimeError()), mailer=mailer)

    result = executor.execute(make_rule())

    assert result.status == ExecutionStatus.FAILED
    assert result.error_type == "RuntimeError"
    assert result.error_message == "RuntimeError"


def test_template_error_fails_without_sending_email():
    mailer = FakeMailer()
    executor = RuleExecutor(sql_client=FakeSqlClient([{"id": 1, "amount": 100}]), mailer=mailer)

    result = executor.execute(make_rule(body_template="{{missing}}"))

    assert result.status == ExecutionStatus.FAILED
    assert result.row_count == 1
    assert result.mail_count == 0
    assert "missing" in result.error_message
    assert mailer.messages == []


def test_unsafe_template_fails_without_sending_email():
    mailer = FakeMailer()
    executor = RuleExecutor(sql_client=FakeSqlClient([{"id": 1}]), mailer=mailer)

    result = executor.execute(make_rule(body_template="{{ ''.__class__.__mro__ }}"))

    assert result.status == ExecutionStatus.FAILED
    assert result.error_type == "TemplateRenderError"
    assert mailer.messages == []


def test_empty_recipients_fail_before_rendering_or_sending():
    mailer = FakeMailer()
    executor = RuleExecutor(sql_client=FakeSqlClient([{"id": 1, "amount": 100}]), mailer=mailer)

    result = executor.execute(make_rule(recipients=" , ; "))

    assert result.status == ExecutionStatus.FAILED
    assert result.row_count == 1
    assert result.mail_count == 0
    assert "recipients" in result.error_message
    assert mailer.messages == []


def test_all_mail_failures_fail():
    mailer = FakeMailer(results=[MailSendResult(success=False), MailSendResult(success=False)])
    executor = RuleExecutor(
        sql_client=FakeSqlClient([{"id": 1, "amount": 100}, {"id": 2, "amount": 200}]),
        mailer=mailer,
    )

    result = executor.execute(make_rule(SendMode.PER_ROW, body_template="{{id}}"))

    assert result.status == ExecutionStatus.FAILED
    assert result.row_count == 2
    assert result.mail_count == 0
    assert result.error_type == "MailSendError"
    assert result.error_message == "one or more emails failed"
    assert len(result.mail_results) == 2
    assert all(not mail_result.result.success for mail_result in result.mail_results)


def test_partial_mail_failures_are_partial_failed():
    mailer = FakeMailer(results=[MailSendResult(success=True), MailSendResult(success=False)])
    executor = RuleExecutor(
        sql_client=FakeSqlClient([{"id": 1, "amount": 100}, {"id": 2, "amount": 200}]),
        mailer=mailer,
    )

    result = executor.execute(make_rule(SendMode.PER_ROW, body_template="{{id}}"))

    assert result.status == ExecutionStatus.PARTIAL_FAILED
    assert result.row_count == 2
    assert result.mail_count == 1
    assert result.error_type == "MailSendError"
    assert result.error_message == "one or more emails failed"
    assert [mail_result.result.success for mail_result in result.mail_results] == [True, False]


def test_mailer_exception_is_recorded_as_failed_send():
    mailer = FakeMailer(error=RuntimeError("smtp unavailable"))
    executor = RuleExecutor(sql_client=FakeSqlClient([{"id": 1, "amount": 100}]), mailer=mailer)

    result = executor.execute(make_rule())

    assert result.status == ExecutionStatus.FAILED
    assert result.row_count == 1
    assert result.mail_count == 0
    assert len(result.mail_results) == 1
    assert result.mail_results[0].result == MailSendResult(
        success=False,
        error_message="smtp unavailable",
    )


def test_recipients_and_cc_are_parsed_for_messages():
    mailer = FakeMailer()
    executor = RuleExecutor(sql_client=FakeSqlClient([{"id": 1, "amount": 100}]), mailer=mailer)

    result = executor.execute(
        make_rule(
            recipients=" ops@example.com, oncall@example.com; audit@example.com ",
            cc_recipients=" lead@example.com; ; finance@example.com, ",
        )
    )

    assert result.status == ExecutionStatus.SUCCESS
    assert mailer.messages[0].recipients == ["ops@example.com", "oncall@example.com", "audit@example.com"]
    assert mailer.messages[0].cc_recipients == ["lead@example.com", "finance@example.com"]


@pytest.mark.parametrize("legacy_trigger_arg", [None, object()])
def test_execute_accepts_optional_legacy_trigger_argument(legacy_trigger_arg):
    mailer = FakeMailer()
    executor = RuleExecutor(sql_client=FakeSqlClient([]), mailer=mailer)

    if legacy_trigger_arg is None:
        result = executor.execute(make_rule())
    else:
        result = executor.execute(make_rule(), legacy_trigger_arg)

    assert result.status == ExecutionStatus.SUCCESS


def test_execute_rule_by_id_persists_failed_execution_when_sql_client_fails(monkeypatch, session):
    data_source = persist_data_source(session)
    persist_smtp_config(session)
    rule = persist_rule(session, data_source)
    monkeypatch.setattr(
        execution_service,
        "build_sql_client",
        lambda data_source: FakeSqlClient(error=RuntimeError("query timed out")),
    )
    monkeypatch.setattr(execution_service, "build_smtp_mailer", lambda config: FakeMailer())

    execution_log = execution_service.execute_rule_by_id(
        session,
        rule.id,
        TriggerType.MANUAL,
        retry_delay_seconds=0,
    )

    assert execution_log.status == ExecutionStatus.FAILED
    assert execution_log.row_count == 0
    assert execution_log.email_count == 0
    assert execution_log.error_type == "RuntimeError"
    assert execution_log.error_message == "query timed out（已重试 2 次）"
    assert execution_log.duration_ms >= 0
    assert execution_log.finished_at is not None
    assert session.exec(select(MailLog)).all() == []


def test_execute_rule_by_id_rejects_archived_rule_without_execution_log(session):
    data_source = persist_data_source(session)
    rule = persist_rule(session, data_source, archived_at=utc_now())

    with pytest.raises(execution_service.RuleNotFoundError):
        execution_service.execute_rule_by_id(session, rule.id)

    assert session.exec(select(ExecutionLog)).all() == []


def test_execute_rule_by_id_retries_transient_sql_failure_then_persists_success(monkeypatch, session):
    data_source = persist_data_source(session)
    persist_smtp_config(session)
    rule = persist_rule(session, data_source)
    sql_client = SequenceSqlClient([RuntimeError("query timed out"), [{"id": 1, "amount": 100}]])
    monkeypatch.setattr(execution_service, "build_sql_client", lambda data_source: sql_client)
    monkeypatch.setattr(execution_service, "build_smtp_mailer", lambda config: FakeMailer())

    execution_log = execution_service.execute_rule_by_id(
        session,
        rule.id,
        TriggerType.MANUAL,
        retry_delay_seconds=0,
    )

    assert execution_log.status == ExecutionStatus.SUCCESS
    assert execution_log.row_count == 1
    assert execution_log.email_count == 1
    assert execution_log.error_type == ""
    assert execution_log.error_message == ""
    assert len(sql_client.calls) == 2
    assert session.exec(select(MailLog)).one().status == MailStatus.SUCCESS


def test_execute_rule_by_id_persists_one_log_after_exhausting_retries(monkeypatch, session):
    data_source = persist_data_source(session)
    persist_smtp_config(session)
    rule = persist_rule(session, data_source)
    sql_client = FakeSqlClient(error=RuntimeError("query timed out"))
    monkeypatch.setattr(execution_service, "build_sql_client", lambda data_source: sql_client)
    monkeypatch.setattr(execution_service, "build_smtp_mailer", lambda config: FakeMailer())

    execution_log = execution_service.execute_rule_by_id(
        session,
        rule.id,
        TriggerType.MANUAL,
        retry_delay_seconds=0,
    )

    execution_logs = session.exec(select(execution_service.ExecutionLog)).all()
    assert execution_logs == [execution_log]
    assert execution_log.status == ExecutionStatus.FAILED
    assert execution_log.error_type == "RuntimeError"
    assert execution_log.error_message == "query timed out（已重试 2 次）"
    assert len(sql_client.calls) == 3
    assert session.exec(select(MailLog)).all() == []


def test_execute_rule_by_id_persists_partial_mail_results(monkeypatch, session):
    data_source = persist_data_source(session)
    persist_smtp_config(session)
    rule = persist_rule(session, data_source, send_mode=SendMode.PER_ROW, body_template="{{id}}")
    monkeypatch.setattr(
        execution_service,
        "build_sql_client",
        lambda data_source: FakeSqlClient([{"id": 1}, {"id": 2}]),
    )
    monkeypatch.setattr(
        execution_service,
        "build_smtp_mailer",
        lambda config: FakeMailer(
            results=[
                MailSendResult(success=True),
                MailSendResult(success=False, error_message="smtp rejected"),
            ]
        ),
    )

    execution_log = execution_service.execute_rule_by_id(
        session,
        rule.id,
        TriggerType.MANUAL,
        retry_delay_seconds=0,
    )

    assert execution_log.status == ExecutionStatus.PARTIAL_FAILED
    assert execution_log.row_count == 2
    assert execution_log.email_count == 1
    assert execution_log.error_type == "MailSendError"
    mail_logs = session.exec(select(MailLog).order_by(MailLog.id)).all()
    assert [mail_log.status for mail_log in mail_logs] == [MailStatus.SUCCESS, MailStatus.FAILED]
    assert [mail_log.subject for mail_log in mail_logs] == ["预警 大额订单", "预警 大额订单"]
    assert mail_logs[0].recipients == "ops@example.com"
    assert mail_logs[1].error_message == "smtp rejected"
    assert len(mail_logs) == 2


def test_execute_rule_by_id_records_suppression_keys_after_success(monkeypatch, session):
    data_source = persist_data_source(session)
    persist_smtp_config(session)
    rule = persist_rule(
        session,
        data_source,
        suppress_duplicates=True,
        suppression_key_field="id",
        suppression_window_hours=24,
    )
    monkeypatch.setattr(
        execution_service,
        "build_sql_client",
        lambda data_source: FakeSqlClient([{"id": 1, "amount": 100}, {"id": 2, "amount": 200}]),
    )
    monkeypatch.setattr(execution_service, "build_smtp_mailer", lambda config: FakeMailer())

    execution_log = execution_service.execute_rule_by_id(
        session,
        rule.id,
        TriggerType.MANUAL,
        retry_delay_seconds=0,
    )

    suppressions = session.exec(select(AlertSuppression).order_by(AlertSuppression.suppression_key)).all()
    assert execution_log.status == ExecutionStatus.SUCCESS
    assert execution_log.row_count == 2
    assert execution_log.email_count == 1
    assert [suppression.suppression_key for suppression in suppressions] == ["1", "2"]
    assert [suppression.hit_count for suppression in suppressions] == [1, 1]


def test_execute_rule_by_id_suppresses_repeated_rows_inside_window(monkeypatch, session):
    data_source = persist_data_source(session)
    persist_smtp_config(session)
    rule = persist_rule(
        session,
        data_source,
        suppress_duplicates=True,
        suppression_key_field="id",
        suppression_window_hours=24,
    )
    sql_client = FakeSqlClient([{"id": 1, "amount": 100}, {"id": 2, "amount": 200}])
    monkeypatch.setattr(execution_service, "build_sql_client", lambda data_source: sql_client)
    monkeypatch.setattr(execution_service, "build_smtp_mailer", lambda config: FakeMailer())

    first_log = execution_service.execute_rule_by_id(
        session,
        rule.id,
        TriggerType.MANUAL,
        retry_delay_seconds=0,
    )
    second_log = execution_service.execute_rule_by_id(
        session,
        rule.id,
        TriggerType.MANUAL,
        retry_delay_seconds=0,
    )

    suppressions = session.exec(select(AlertSuppression).order_by(AlertSuppression.suppression_key)).all()
    mail_logs = session.exec(select(MailLog)).all()
    assert first_log.email_count == 1
    assert second_log.status == ExecutionStatus.SUCCESS
    assert second_log.row_count == 2
    assert second_log.email_count == 0
    assert len(mail_logs) == 1
    assert [suppression.hit_count for suppression in suppressions] == [2, 2]


def test_execute_rule_by_id_suppresses_only_matching_keys(monkeypatch, session):
    data_source = persist_data_source(session)
    persist_smtp_config(session)
    rule = persist_rule(
        session,
        data_source,
        suppress_duplicates=True,
        suppression_key_field="id",
        suppression_window_hours=24,
        send_mode=SendMode.PER_ROW,
        body_template="{{id}}",
    )
    session.add(AlertSuppression(rule_id=rule.id, suppression_key="1"))
    session.commit()
    mailer = FakeMailer()
    monkeypatch.setattr(
        execution_service,
        "build_sql_client",
        lambda data_source: FakeSqlClient([{"id": 1}, {"id": 2}]),
    )
    monkeypatch.setattr(execution_service, "build_smtp_mailer", lambda config: mailer)

    execution_log = execution_service.execute_rule_by_id(
        session,
        rule.id,
        TriggerType.MANUAL,
        retry_delay_seconds=0,
    )

    suppressions = session.exec(select(AlertSuppression).order_by(AlertSuppression.suppression_key)).all()
    assert execution_log.status == ExecutionStatus.SUCCESS
    assert execution_log.row_count == 2
    assert execution_log.email_count == 1
    assert [message.html_body for message in mailer.messages] == ["2"]
    assert [suppression.suppression_key for suppression in suppressions] == ["1", "2"]
    assert [suppression.hit_count for suppression in suppressions] == [2, 1]


def test_execute_rule_by_id_does_not_record_suppression_on_partial_failure(monkeypatch, session):
    data_source = persist_data_source(session)
    persist_smtp_config(session)
    rule = persist_rule(
        session,
        data_source,
        suppress_duplicates=True,
        suppression_key_field="id",
        suppression_window_hours=24,
        send_mode=SendMode.PER_ROW,
        body_template="{{id}}",
    )
    monkeypatch.setattr(
        execution_service,
        "build_sql_client",
        lambda data_source: FakeSqlClient([{"id": 1}, {"id": 2}]),
    )
    monkeypatch.setattr(
        execution_service,
        "build_smtp_mailer",
        lambda config: FakeMailer(results=[MailSendResult(success=True), MailSendResult(success=False)]),
    )

    execution_log = execution_service.execute_rule_by_id(
        session,
        rule.id,
        TriggerType.MANUAL,
        retry_delay_seconds=0,
    )

    assert execution_log.status == ExecutionStatus.PARTIAL_FAILED
    assert session.exec(select(AlertSuppression)).all() == []


def test_execute_rule_by_id_persists_failed_log_when_data_source_disabled(monkeypatch, session):
    data_source = persist_data_source(session, enabled=False)
    persist_smtp_config(session)
    rule = persist_rule(session, data_source)
    sleep_calls = []

    execution_log = execution_service.execute_rule_by_id(
        session,
        rule.id,
        TriggerType.MANUAL,
        sleep_fn=lambda seconds: sleep_calls.append(seconds),
    )

    assert execution_log.status == ExecutionStatus.FAILED
    assert execution_log.error_type == "ConfigurationError"
    assert "数据源" in execution_log.error_message
    assert "已重试" not in execution_log.error_message
    assert sleep_calls == []
    assert session.exec(select(MailLog)).all() == []


def test_execute_rule_by_id_persists_failed_log_when_builder_raises(monkeypatch, session):
    data_source = persist_data_source(session)
    persist_smtp_config(session)
    rule = persist_rule(session, data_source)

    def raise_builder_error(data_source):
        raise RuntimeError("cannot decrypt")

    monkeypatch.setattr(execution_service, "build_sql_client", raise_builder_error)

    execution_log = execution_service.execute_rule_by_id(
        session,
        rule.id,
        TriggerType.MANUAL,
        retry_delay_seconds=0,
    )

    assert execution_log.status == ExecutionStatus.FAILED
    assert execution_log.error_type == "RuntimeError"
    assert execution_log.error_message == "cannot decrypt（已重试 2 次）"
    assert session.exec(select(MailLog)).all() == []
