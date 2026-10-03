import logging
from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage as MimeEmailMessage

from app.error_reporting import log_exception_safely, public_error_summary


logger = logging.getLogger(__name__)
SMTP_SEND_FAILURE = "SMTP 发送失败，请检查服务器、端口、加密方式和账号配置"
SMTP_PARTIAL_FAILURE = "SMTP 部分收件人被拒收，请检查邮箱地址及服务器策略"


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
    partial_success: bool = False


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
        client = None
        try:
            client = self.client_factory()
            refused = client.sendmail(self.sender, all_recipients, mime.as_string())
            if refused:
                partial_success = any(address not in refused for address in all_recipients)
                logger.warning(
                    "SMTP recipients refused: refused_count=%d; partial_success=%s",
                    len(refused),
                    partial_success,
                )
                return MailSendResult(
                    success=False,
                    error_message=SMTP_PARTIAL_FAILURE if partial_success else SMTP_SEND_FAILURE,
                    partial_success=partial_success,
                )
            return MailSendResult(success=True)
        except Exception as exc:
            log_exception_safely(logger, "SMTP send failed: operation=smtp_send", exc)
            return MailSendResult(
                success=False,
                error_message=public_error_summary(exc, fallback=SMTP_SEND_FAILURE),
            )
        finally:
            if client is not None:
                self._close_client(client)

    def _close_client(self, client) -> None:
        quit_method = getattr(client, "quit", None)
        if callable(quit_method):
            try:
                quit_method()
                return
            except Exception:
                pass

        close_method = getattr(client, "close", None)
        if callable(close_method):
            try:
                close_method()
            except Exception:
                pass
