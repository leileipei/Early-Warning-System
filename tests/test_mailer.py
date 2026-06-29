from email import message_from_string

from app.mailer import EmailMessage, MailSendResult, SmtpMailer


class FakeSmtpClient:
    def __init__(self):
        self.sent = []

    def sendmail(self, sender, recipients, body):
        self.sent.append((sender, recipients, body))


class FailingSmtpClient:
    def sendmail(self, sender, recipients, body):
        raise RuntimeError("smtp unavailable")


def test_smtp_mailer_sends_html_message():
    fake = FakeSmtpClient()
    mailer = SmtpMailer(sender="alerts@example.com", client_factory=lambda: fake)
    message = EmailMessage(
        recipients=["ops@example.com"],
        cc_recipients=[],
        subject="预警",
        html_body="<p>hello</p>",
    )

    result = mailer.send(message)

    assert result == MailSendResult(success=True, error_message="")
    assert fake.sent[0][0] == "alerts@example.com"
    assert fake.sent[0][1] == ["ops@example.com"]


def test_smtp_mailer_includes_cc_recipients_in_headers_and_smtp_recipients():
    fake = FakeSmtpClient()
    mailer = SmtpMailer(sender="alerts@example.com", client_factory=lambda: fake)
    message = EmailMessage(
        recipients=["ops@example.com"],
        cc_recipients=["lead@example.com", "audit@example.com"],
        subject="预警",
        html_body="<p>hello</p>",
    )

    result = mailer.send(message)

    assert result == MailSendResult(success=True, error_message="")
    assert fake.sent[0][1] == [
        "ops@example.com",
        "lead@example.com",
        "audit@example.com",
    ]
    mime = message_from_string(fake.sent[0][2])
    assert mime["To"] == "ops@example.com"
    assert mime["Cc"] == "lead@example.com, audit@example.com"


def test_smtp_mailer_returns_failure_result_when_sendmail_raises():
    mailer = SmtpMailer(
        sender="alerts@example.com",
        client_factory=lambda: FailingSmtpClient(),
    )
    message = EmailMessage(
        recipients=["ops@example.com"],
        cc_recipients=[],
        subject="预警",
        html_body="<p>hello</p>",
    )

    result = mailer.send(message)

    assert result == MailSendResult(
        success=False,
        error_message="smtp unavailable",
    )
