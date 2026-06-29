import pytest

from app.executor import RuleExecutor
from app.mailer import MailSendResult
from app.models import AlertRule, ExecutionStatus, SendMode


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
