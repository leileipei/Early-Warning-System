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
SENSITIVE_MAPPING_KEY = re.compile(
    rf"(?i)(?<![A-Za-z0-9_])(?P<quote>['\"]?)(?P<key>{_SENSITIVE_FIELD_NAME})"
    rf"(?![A-Za-z0-9_])(?P=quote)[ \t]*:[ \t]*"
)
ODBC_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(server|data\s+source|uid|user\s*id|database|initial\s+catalog|pwd|password)"
    rf"\s*=\s*{_ASSIGNMENT_VALUE}"
)
FERNET_VALUE = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43}=(?![A-Za-z0-9_-])")
ODBC_CONNECTION_STRING = re.compile(r"(?i)\bDRIVER=\{[^\r\n]*")
_STRUCTURED_VALUE_SCAN_LIMIT = 32_768
_STRUCTURED_VALUE_DEPTH_LIMIT = 64


def _line_end(text: str, start: int) -> int:
    ends = [position for marker in ("\r", "\n") if (position := text.find(marker, start)) >= 0]
    return min(ends, default=len(text))


def _consume_trailing_fragment(text: str, start: int, end: int) -> int:
    position = start
    while position < end and text[position] not in ",}]":
        position += 1
    return position


def _structured_value_end(text: str, start: int) -> int:
    line_end = _line_end(text, start)
    if start >= line_end:
        return start

    scan_end = min(line_end, start + _STRUCTURED_VALUE_SCAN_LIMIT)
    first = text[start]
    if first in "[{":
        closing_for = {"[": "]", "{": "}"}
        stack = [closing_for[first]]
        quote = ""
        escaped = False
        position = start + 1
        while position < scan_end:
            character = text[position]
            if quote:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == quote:
                    quote = ""
            elif character in "'\"":
                quote = character
            elif character in closing_for:
                if len(stack) >= _STRUCTURED_VALUE_DEPTH_LIMIT:
                    return line_end
                stack.append(closing_for[character])
            elif character in "]}":
                if character != stack[-1]:
                    return line_end
                stack.pop()
                if not stack:
                    return _consume_trailing_fragment(text, position + 1, line_end)
            position += 1
        return line_end

    if first in "'\"":
        quote = first
        escaped = False
        position = start + 1
        while position < scan_end:
            character = text[position]
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                return _consume_trailing_fragment(text, position + 1, line_end)
            position += 1
        return line_end

    position = start
    while position < scan_end and text[position] not in ",}]":
        position += 1
    return position if position < scan_end else line_end


def _redact_sensitive_mapping_values(text: str) -> str:
    rendered: list[str] = []
    cursor = 0
    while match := SENSITIVE_MAPPING_KEY.search(text, cursor):
        value_start = match.end()
        value_end = _structured_value_end(text, value_start)
        rendered.append(text[cursor:value_start])
        rendered.append("[REDACTED]")
        cursor = max(value_end, value_start)
    rendered.append(text[cursor:])
    return "".join(rendered)


def redact_sensitive_text(value: object, *, limit: int = 300) -> str:
    text = ODBC_CONNECTION_STRING.sub("[REDACTED CONNECTION STRING]", str(value))
    text = ODBC_SENSITIVE_ASSIGNMENT.sub(r"\1=[REDACTED]", text)
    text = SENSITIVE_ASSIGNMENT.sub(r"\1=[REDACTED]", text)
    text = _redact_sensitive_mapping_values(text)
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
