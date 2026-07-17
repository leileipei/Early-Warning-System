import logging
import re
import traceback


_ASSIGNMENT_VALUE = r"(?:\{[^}]*\}|\"[^\"]*\"|'[^']*'|[^;\s]+)"
SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b((?:[A-Za-z0-9]+_)*(?:pwd|password|secret|secret_key|session_secret))"
    rf"\s*=\s*{_ASSIGNMENT_VALUE}"
)
ODBC_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(server|uid|user\s*id|database|pwd|password)"
    rf"\s*=\s*{_ASSIGNMENT_VALUE}"
)
FERNET_VALUE = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43}=(?![A-Za-z0-9_-])")
ODBC_CONNECTION_STRING = re.compile(r"(?i)\bDRIVER=\{[^\r\n]*")


def redact_sensitive_text(value: object, *, limit: int = 300) -> str:
    text = ODBC_CONNECTION_STRING.sub("[REDACTED CONNECTION STRING]", str(value))
    text = ODBC_SENSITIVE_ASSIGNMENT.sub(r"\1=[REDACTED]", text)
    text = SENSITIVE_ASSIGNMENT.sub(r"\1=[REDACTED]", text)
    text = FERNET_VALUE.sub("[REDACTED KEY]", text)
    return text[:limit]


def public_error_summary(exc: BaseException, *, fallback: str) -> str:
    _ = exc
    return fallback


def log_exception_safely(logger: logging.Logger, message: str, exc: BaseException) -> None:
    rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    logger.error(
        "%s: error_type=%s\n%s",
        redact_sensitive_text(message),
        type(exc).__name__,
        redact_sensitive_text(rendered, limit=4_000),
    )
