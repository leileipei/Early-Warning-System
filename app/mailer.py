from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage as MimeEmailMessage


@dataclass(frozen=True)
class EmailMessage:
    recipients: list[str]
    cc_recipients: list[str]
    subject: str
    html_body: str


@dataclass(frozen=True)
class MailSendResult:
    success: bool
    error_message: str = ""


class SmtpMailer:
    def __init__(self, sender: str, client_factory: Callable):
        self.sender = sender
        self.client_factory = client_factory

    def send(self, message: EmailMessage) -> MailSendResult:
        mime = MimeEmailMessage()
        mime["From"] = self.sender
        mime["To"] = ", ".join(message.recipients)
        if message.cc_recipients:
            mime["Cc"] = ", ".join(message.cc_recipients)
        mime["Subject"] = message.subject
        mime.set_content("HTML 邮件需要使用支持 HTML 的客户端查看。")
        mime.add_alternative(message.html_body, subtype="html")

        all_recipients = message.recipients + message.cc_recipients
        try:
            client = self.client_factory()
            client.sendmail(self.sender, all_recipients, mime.as_string())
            return MailSendResult(success=True)
        except Exception as exc:
            return MailSendResult(success=False, error_message=str(exc))
