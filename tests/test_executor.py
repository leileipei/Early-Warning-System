from app.executor import RuleExecutor
from app.mailer import MailSendResult
from app.models import AlertRule, ExecutionStatus, SendMode, TriggerType


class FakeSqlClient:
    def __init__(self, rows=None, error=None):
        self.rows = rows or []
        self.error = error

    def query(self, sql, timeout_seconds, max_rows):
        if self.error is not None:
            raise self.error

        from app.sql_client import QueryResult

        return QueryResult(rows=self.rows)


class FakeMailer:
    def __init__(self, results=None):
        self.messages = []
        self.results = results or []

    def send(self, message):
        self.messages.append(message)
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


def test_summary_mode_sends_one_email():
    mailer = FakeMailer()
    executor = RuleExecutor(sql_client=FakeSqlClient([{"id": 1, "amount": 100}]), mailer=mailer)

    result = executor.execute(make_rule(), TriggerType.MANUAL)

    assert result.status == ExecutionStatus.SUCCESS
    assert result.row_count == 1
    assert result.email_count == 1
    assert len(mailer.messages) == 1


def test_per_row_mode_sends_one_email_per_row():
    mailer = FakeMailer()
    executor = RuleExecutor(
        sql_client=FakeSqlClient([{"id": 1, "amount": 100}, {"id": 2, "amount": 200}]),
        mailer=mailer,
    )

    result = executor.execute(make_rule(SendMode.PER_ROW), TriggerType.MANUAL)

    assert result.status == ExecutionStatus.SUCCESS
    assert result.row_count == 2
    assert result.email_count == 2


def test_no_rows_succeeds_without_sending_email():
    mailer = FakeMailer()
    executor = RuleExecutor(sql_client=FakeSqlClient([]), mailer=mailer)

    result = executor.execute(make_rule(), TriggerType.MANUAL)

    assert result.status == ExecutionStatus.SUCCESS
    assert result.row_count == 0
    assert result.email_count == 0
    assert mailer.messages == []


def test_invalid_sql_fails_before_query_or_email():
    mailer = FakeMailer()
    sql_client = FakeSqlClient([{"id": 1}])
    executor = RuleExecutor(sql_client=sql_client, mailer=mailer)

    result = executor.execute(make_rule(sql_text="delete from orders"), TriggerType.MANUAL)

    assert result.status == ExecutionStatus.FAILED
    assert result.row_count == 0
    assert result.email_count == 0
    assert "SELECT" in result.error_message
    assert mailer.messages == []


def test_sql_client_exception_fails():
    mailer = FakeMailer()
    executor = RuleExecutor(
        sql_client=FakeSqlClient(error=RuntimeError("query timed out")),
        mailer=mailer,
    )

    result = executor.execute(make_rule(), TriggerType.MANUAL)

    assert result.status == ExecutionStatus.FAILED
    assert result.row_count == 0
    assert result.email_count == 0
    assert result.error_message == "query timed out"
    assert mailer.messages == []


def test_template_error_fails_without_sending_email():
    mailer = FakeMailer()
    executor = RuleExecutor(sql_client=FakeSqlClient([{"id": 1, "amount": 100}]), mailer=mailer)

    result = executor.execute(make_rule(body_template="{{missing}}"), TriggerType.MANUAL)

    assert result.status == ExecutionStatus.FAILED
    assert result.row_count == 0
    assert result.email_count == 0
    assert "missing" in result.error_message
    assert mailer.messages == []


def test_all_mail_failures_fail():
    mailer = FakeMailer(results=[MailSendResult(success=False), MailSendResult(success=False)])
    executor = RuleExecutor(
        sql_client=FakeSqlClient([{"id": 1, "amount": 100}, {"id": 2, "amount": 200}]),
        mailer=mailer,
    )

    result = executor.execute(
        make_rule(SendMode.PER_ROW, body_template="{{id}}"),
        TriggerType.MANUAL,
    )

    assert result.status == ExecutionStatus.FAILED
    assert result.row_count == 2
    assert result.email_count == 2


def test_partial_mail_failures_are_partial_failed():
    mailer = FakeMailer(results=[MailSendResult(success=True), MailSendResult(success=False)])
    executor = RuleExecutor(
        sql_client=FakeSqlClient([{"id": 1, "amount": 100}, {"id": 2, "amount": 200}]),
        mailer=mailer,
    )

    result = executor.execute(
        make_rule(SendMode.PER_ROW, body_template="{{id}}"),
        TriggerType.MANUAL,
    )

    assert result.status == ExecutionStatus.PARTIAL_FAILED
    assert result.row_count == 2
    assert result.email_count == 2


def test_recipients_and_cc_are_parsed_for_messages():
    mailer = FakeMailer()
    executor = RuleExecutor(sql_client=FakeSqlClient([{"id": 1, "amount": 100}]), mailer=mailer)

    result = executor.execute(
        make_rule(
            recipients=" ops@example.com, oncall@example.com, ",
            cc_recipients=" lead@example.com, , audit@example.com ",
        ),
        TriggerType.MANUAL,
    )

    assert result.status == ExecutionStatus.SUCCESS
    assert mailer.messages[0].recipients == ["ops@example.com", "oncall@example.com"]
    assert mailer.messages[0].cc_recipients == ["lead@example.com", "audit@example.com"]
