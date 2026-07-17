from email import message_from_string

from app.mailer import EmailMessage, MailSendResult, SmtpMailer


class FakeSmtpClient:
    def __init__(self):
        self.sent = []
        self.events = []

    def sendmail(self, sender, recipients, body):
        self.events.append("sendmail")
        self.sent.append((sender, recipients, body))

    def quit(self):
        self.events.append("quit")

    def close(self):
        self.events.append("close")


class FailingSmtpClient:
    def __init__(self):
        self.events = []

    def sendmail(self, sender, recipients, body):
        self.events.append("sendmail")
        raise RuntimeError("smtp unavailable")

    def quit(self):
        self.events.append("quit")

    def close(self):
        self.events.append("close")


class QuitFailingSmtpClient(FakeSmtpClient):
    def quit(self):
        self.events.append("quit")
        raise RuntimeError("quit unavailable")


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
    mime = message_from_string(fake.sent[0][2])
    assert mime.is_multipart()
    parts = list(mime.walk())
    text_part = next(part for part in parts if part.get_content_type() == "text/plain")
    html_part = next(part for part in parts if part.get_content_type() == "text/html")
    plain_body = text_part.get_payload(decode=True).decode(text_part.get_content_charset())
    assert "HTML 邮件需要使用支持 HTML 的客户端查看。" in plain_body
    assert "<p>hello</p>" in html_part.get_payload(decode=True).decode(
        html_part.get_content_charset()
    )


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


def test_smtp_mailer_returns_fixed_error_and_logs_redacted_failure(caplog):
    class SensitiveFailingSmtpClient(FailingSmtpClient):
        def sendmail(self, sender, recipients, body):
            self.events.append("sendmail")
            raise RuntimeError("SMTP_PASSWORD=smtp-secret Fernet=" + "c" * 43 + "=")

    fake = SensitiveFailingSmtpClient()
    mailer = SmtpMailer(sender="alerts@example.com", client_factory=lambda: fake)
    message = EmailMessage(
        recipients=["ops@example.com"],
        cc_recipients=[],
        subject="预警",
        html_body="<p>hello</p>",
    )

    with caplog.at_level("ERROR"):
        result = mailer.send(message)

    assert result == MailSendResult(
        success=False,
        error_message="SMTP 发送失败，请检查服务器、端口、加密方式和账号配置",
    )
    assert "error_type=RuntimeError" in caplog.text
    assert "smtp-secret" not in caplog.text
    assert "c" * 43 + "=" not in caplog.text


def test_smtp_mailer_quits_client_after_successful_send():
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
    assert fake.events == ["sendmail", "quit"]


def test_smtp_mailer_quits_client_after_sendmail_failure():
    fake = FailingSmtpClient()
    mailer = SmtpMailer(sender="alerts@example.com", client_factory=lambda: fake)
    message = EmailMessage(
        recipients=["ops@example.com"],
        cc_recipients=[],
        subject="预警",
        html_body="<p>hello</p>",
    )

    result = mailer.send(message)

    assert result == MailSendResult(
        success=False,
        error_message="SMTP 发送失败，请检查服务器、端口、加密方式和账号配置",
    )
    assert fake.events == ["sendmail", "quit"]


def test_smtp_mailer_falls_back_to_close_when_quit_raises():
    fake = QuitFailingSmtpClient()
    mailer = SmtpMailer(sender="alerts@example.com", client_factory=lambda: fake)
    message = EmailMessage(
        recipients=["ops@example.com"],
        cc_recipients=[],
        subject="预警",
        html_body="<p>hello</p>",
    )

    result = mailer.send(message)

    assert result == MailSendResult(success=True, error_message="")
    assert fake.events == ["sendmail", "quit", "close"]
