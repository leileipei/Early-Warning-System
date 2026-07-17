import logging
import re
import traceback
from uuid import uuid4


_DOUBLE_QUOTED_VALUE = r'"(?:\\.|[^"\\])*"(?=[;\s]|\Z)'
_SINGLE_QUOTED_VALUE = r"'(?:\\.|[^'\\])*'(?=[;\s]|\Z)"
_ODBC_BRACED_VALUE = r"\{(?:}}|[^}])*\}(?!})(?=[;\s]|\Z)"
_UNTERMINATED_DOUBLE_QUOTED_VALUE = r'"[^\r\n]*'
_UNTERMINATED_SINGLE_QUOTED_VALUE = r"'[^\r\n]*"
_UNTERMINATED_ODBC_BRACED_VALUE = r"\{[^\r\n]*"
_ASSIGNMENT_VALUE = (
    rf"(?:{_DOUBLE_QUOTED_VALUE}|{_SINGLE_QUOTED_VALUE}|{_ODBC_BRACED_VALUE}|"
    rf"{_UNTERMINATED_DOUBLE_QUOTED_VALUE}|{_UNTERMINATED_SINGLE_QUOTED_VALUE}|"
    rf"{_UNTERMINATED_ODBC_BRACED_VALUE}|[^;\s]+)"
)
_SENSITIVE_FIELD_NAME = (
    r"(?:[A-Za-z0-9]+_)*(?:pwd|password|secret|secret_key|session_secret)"
)
SENSITIVE_ASSIGNMENT = re.compile(
    rf"(?i)\b({_SENSITIVE_FIELD_NAME})"
    rf"\s*=\s*{_ASSIGNMENT_VALUE}"
)
_STRUCTURED_DOUBLE_QUOTED_VALUE = r'"(?:\\.|[^"\\\r\n])*"[^,\r\n}\]]*'
_STRUCTURED_SINGLE_QUOTED_VALUE = r"'(?:\\.|[^'\\\r\n])*'[^,\r\n}\]]*"
_UNTERMINATED_STRUCTURED_DOUBLE_VALUE = r'"[^\r\n]*'
_UNTERMINATED_STRUCTURED_SINGLE_VALUE = r"'[^\r\n]*"
_STRUCTURED_VALUE = (
    rf"(?:{_STRUCTURED_DOUBLE_QUOTED_VALUE}|{_STRUCTURED_SINGLE_QUOTED_VALUE}|"
    rf"{_UNTERMINATED_STRUCTURED_DOUBLE_VALUE}|{_UNTERMINATED_STRUCTURED_SINGLE_VALUE}|"
    r"[^,\s}\]]+)"
)
SENSITIVE_MAPPING_VALUE = re.compile(
    rf"(?i)(?P<quote>['\"]?)(?P<key>{_SENSITIVE_FIELD_NAME})(?P=quote)"
    rf"\s*:\s*{_STRUCTURED_VALUE}"
)
ODBC_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(server|data\s+source|uid|user\s*id|database|initial\s+catalog|pwd|password)"
    rf"\s*=\s*{_ASSIGNMENT_VALUE}"
)
FERNET_VALUE = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43}=(?![A-Za-z0-9_-])")
ODBC_CONNECTION_STRING = re.compile(r"(?i)\bDRIVER=\{[^\r\n]*")


def redact_sensitive_text(value: object, *, limit: int = 300) -> str:
    text = ODBC_CONNECTION_STRING.sub("[REDACTED CONNECTION STRING]", str(value))
    text = ODBC_SENSITIVE_ASSIGNMENT.sub(r"\1=[REDACTED]", text)
    text = SENSITIVE_ASSIGNMENT.sub(r"\1=[REDACTED]", text)
    text = SENSITIVE_MAPPING_VALUE.sub(
        lambda match: (
            f"{match.group('quote')}{match.group('key')}{match.group('quote')}: [REDACTED]"
        ),
        text,
    )
    text = FERNET_VALUE.sub("[REDACTED KEY]", text)
    return text[:limit]


def public_error_summary(exc: BaseException, *, fallback: str) -> str:
    _ = exc
    return fallback


def log_failure_safely(logger: logging.Logger, message: str, *, error_type: str) -> None:
    logger.error(
        "%s: error_id=%s; error_type=%s",
        redact_sensitive_text(message),
        uuid4().hex,
        error_type,
    )


def log_exception_safely(logger: logging.Logger, message: str, exc: BaseException) -> None:
    rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    error_id = uuid4().hex
    logger.error(
        "%s: error_id=%s; error_type=%s\n%s",
        redact_sensitive_text(message),
        error_id,
        type(exc).__name__,
        redact_sensitive_text(rendered, limit=4_000),
    )
