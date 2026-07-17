import json
import logging

import pytest

from app.error_reporting import log_exception_safely, redact_sensitive_text


def test_redact_sensitive_text_removes_complete_odbc_string_and_fernet_key():
    fernet_key = "a" * 43 + "="
    connection_string = (
        "DRIVER={ODBC Driver 18 for SQL Server};SERVER={db.internal,1433};"
        "UID={report_user};PWD={database-password};"
    )

    rendered = redact_sensitive_text(
        f"connect failed: {connection_string}\nFernet={fernet_key} SMTP_PASSWORD=smtp-secret"
    )

    assert "[REDACTED CONNECTION STRING]" in rendered
    assert "[REDACTED KEY]" in rendered
    for secret in ("db.internal", "report_user", "database-password", fernet_key, "smtp-secret"):
        assert secret not in rendered


def test_redact_sensitive_text_handles_quoted_prefixed_values_and_multiline_odbc():
    rendered = redact_sensitive_text(
        'SMTP_PASSWORD="alpha beta gamma"\n'
        "APP_SMTP_PASSWORD=delta-epsilon\n"
        "DRIVER={ODBC Driver 18 for SQL Server};\n"
        "SERVER=warning-db.internal,1433;\n"
        "UID=warning_user;\n"
        "DATABASE=warning_database;\n"
        "PWD='database password';"
    )

    for secret in (
        "alpha beta gamma",
        "delta-epsilon",
        "warning-db.internal",
        "warning_user",
        "warning_database",
        "database password",
    ):
        assert secret not in rendered


def test_redact_sensitive_text_consumes_escaped_quoted_and_odbc_braced_values():
    rendered = redact_sensitive_text(
        'SMTP_PASSWORD="alpha\\\" QUOTETAIL"; '
        'DATABASE="alpha\\\" DATABASETAIL"; '
        "UID={alpha;beta}}UIDTAIL}; "
        "SERVER={alpha;beta}}SERVERTAIL};"
    )

    for secret in ("alpha", "beta", "QUOTETAIL", "DATABASETAIL", "UIDTAIL", "SERVERTAIL"):
        assert secret not in rendered


def test_redact_sensitive_text_fails_closed_for_unterminated_values_and_odbc_aliases():
    rendered = redact_sensitive_text(
        'SMTP_PASSWORD="alpha UNTERMINATED_TAIL\n'
        "unrelated=keep\n"
        "UID={alpha;beta}}UNTERMINATED_UID_TAIL\n"
        "unrelated_next=keep2\n"
        "DATA SOURCE=odbc-host; INITIAL CATALOG=odbc-database;"
    )

    for secret in (
        "alpha",
        "beta",
        "UNTERMINATED_TAIL",
        "UNTERMINATED_UID_TAIL",
        "odbc-host",
        "odbc-database",
    ):
        assert secret not in rendered
    assert "unrelated=keep" in rendered
    assert "unrelated_next=keep2" in rendered


def test_redact_sensitive_text_fails_closed_when_closed_values_have_trailing_fragments():
    rendered = redact_sensitive_text(
        'SMTP_PASSWORD="alpha"QUOTEDTAIL; '
        "UID={beta}UIDCLOSEDTAIL; "
        'SERVER="gamma"SERVERCLOSEDTAIL'
    )

    for secret in ("alpha", "beta", "gamma", "QUOTEDTAIL", "UIDCLOSEDTAIL", "SERVERCLOSEDTAIL"):
        assert secret not in rendered


def test_redact_sensitive_text_handles_json_and_python_dict_values():
    rendered = redact_sensitive_text(
        '{"password": "smtp-secret", "safe": "keep-json", '
        '"nested": {"secret_key" : "nested-secret"}} '
        "{'session_secret': 'session-secret', 'safe': 'keep-dict'}"
    )

    for secret in ("smtp-secret", "nested-secret", "session-secret"):
        assert secret not in rendered
    assert "keep-json" in rendered
    assert "keep-dict" in rendered


@pytest.mark.parametrize(
    ("value", "secrets", "preserved"),
    [
        (
            '{"password": {"value": "deep-secret", "items": ["alpha", "beta"]}, '
            '"safe": "keep-json"}',
            ("deep-secret", "alpha", "beta"),
            '"safe": "keep-json"',
        ),
        (
            '{"secret": [{"value": "list-secret"}, ["nested", "values"]], '
            '"safe": 2}',
            ("list-secret", "nested", "values"),
            '"safe": 2',
        ),
        (
            "{'session_secret': {'outer': [{'inner': 'deep-single'}, "
            "{'more': 'with spaces, and commas'}]}, 'safe': 'keep-dict'}",
            ("deep-single", "with spaces, and commas"),
            "'safe': 'keep-dict'",
        ),
    ],
)
def test_redact_sensitive_text_consumes_complete_nested_mapping_values(
    value, secrets, preserved
):
    rendered = redact_sensitive_text(value)

    for secret in secrets:
        assert secret not in rendered
    assert preserved in rendered


@pytest.mark.parametrize(
    ("value", "secrets", "preserved"),
    [
        (
            json.dumps(
                {
                    "password": {
                        "value": "pretty-secret",
                        "items": ["pretty-list-secret", {"deep": "pretty-deep-secret"}],
                    },
                    "safe": "keep-pretty",
                },
                indent=2,
            ),
            ("pretty-secret", "pretty-list-secret", "pretty-deep-secret"),
            '"safe": "keep-pretty"',
        ),
        (
            "{\n"
            "  'secret':\n"
            "  {\n"
            "    'items': [\n"
            "      {'value': 'colon-newline-secret'},\n"
            "      'colon-newline-list-secret'\n"
            "    ]\n"
            "  },\n"
            "  'safe': 'keep-colon-newline'\n"
            "}",
            ("colon-newline-secret", "colon-newline-list-secret"),
            "'safe': 'keep-colon-newline'",
        ),
        (
            "{\n"
            "  'session_secret'\n"
            "  :\n"
            "  [\n"
            "    {'value': 'split-key-secret'},\n"
            "    ['split-key-list-secret']\n"
            "  ],\n"
            "  'safe': 'keep-split-key'\n"
            "}",
            ("split-key-secret", "split-key-list-secret"),
            "'safe': 'keep-split-key'",
        ),
    ],
)
def test_redact_sensitive_text_handles_multiline_mapping_whitespace(
    value, secrets, preserved
):
    rendered = redact_sensitive_text(value)

    for secret in secrets:
        assert secret not in rendered
    assert preserved in rendered


def test_redact_sensitive_text_fails_closed_for_unterminated_multiline_structure():
    rendered = redact_sensitive_text(
        "{\n"
        '  "password": {\n'
        '    "items": ["multiline-open-secret", {"deep": "multiline-tail-secret"}]\n'
        '  "safe": "keep-after-multiline-unclosed"'
    )

    assert "multiline-open-secret" not in rendered
    assert "multiline-tail-secret" not in rendered
    assert '"safe": "keep-after-multiline-unclosed"' not in rendered


@pytest.mark.parametrize(
    ("value", "secrets", "discarded"),
    [
        (
            '{"password": {"nested": ["open-secret", {"deep": "tail-secret"}]\n'
            '"safe": "keep-next-line"',
            ("open-secret", "tail-secret"),
            '"safe": "keep-next-line"',
        ),
        (
            "{'secret': ['single-open', {'deep': 'single-tail'}]\n"
            "'safe': 'keep-single-next-line'",
            ("single-open", "single-tail"),
            "'safe': 'keep-single-next-line'",
        ),
    ],
)
def test_redact_sensitive_text_fails_closed_for_unterminated_nested_values(
    value, secrets, discarded
):
    rendered = redact_sensitive_text(value)

    for secret in secrets:
        assert secret not in rendered
    assert discarded not in rendered


@pytest.mark.parametrize(
    ("value", "secret", "preserved"),
    [
        ('{"PASSWORD" : "mixed-case-secret", "safe": 1}', "mixed-case-secret", '"safe": 1'),
        ("{'password': 'single-quoted-secret', 'safe': 2}", "single-quoted-secret", "'safe': 2"),
    ],
)
def test_redact_sensitive_text_preserves_sibling_after_complete_scalar_values(
    value, secret, preserved
):
    rendered = redact_sensitive_text(value)

    assert secret not in rendered
    assert preserved in rendered


def test_redact_sensitive_text_discards_tail_after_unterminated_scalar_value():
    rendered = redact_sensitive_text(
        '{"password": "unterminated-secret\n"safe": "discard-after-unterminated"'
    )

    assert "unterminated-secret" not in rendered
    assert "discard-after-unterminated" not in rendered


def test_redact_sensitive_text_fails_closed_for_flat_unterminated_structure():
    rendered = redact_sensitive_text(
        '{"password": {\n'
        '"value": "flat-open-secret",\n'
        '"items": ["flat-list-secret"]'
    )

    assert "flat-open-secret" not in rendered
    assert "flat-list-secret" not in rendered


def test_redact_sensitive_text_fails_closed_when_depth_limit_is_exceeded():
    rendered = redact_sensitive_text(
        '{"password": '
        + "[" * 65
        + '\n"value": "depth-limit-secret"'
        + "]" * 65
        + ', "safe": "discard-after-depth-limit"}'
    )

    assert "depth-limit-secret" not in rendered
    assert "discard-after-depth-limit" not in rendered


def test_redact_sensitive_text_fails_closed_when_colon_is_outside_scan_window():
    rendered = redact_sensitive_text(
        '"password"' + " " * 32_769 + ': "wide-secret"',
        limit=40_000,
    )

    assert "wide-secret" not in rendered


def test_log_exception_safely_keeps_a_bounded_redacted_traceback(caplog):
    logger = logging.getLogger("tests.error_reporting")
    fernet_key = "b" * 43 + "="
    error = RuntimeError(
        "DRIVER={ODBC Driver 18 for SQL Server};SERVER={db.internal,1433};"
        "UID={report_user};PWD={database-password}; "
        f"SMTP_PASSWORD=smtp-secret Fernet={fernet_key}"
    )

    with caplog.at_level(logging.ERROR, logger=logger.name):
        try:
            raise error
        except RuntimeError as exc:
            log_exception_safely(logger, "sql request failed: request_id=req-42", exc)

    assert "error_type=RuntimeError" in caplog.text
    assert "request_id=req-42" in caplog.text
    assert "Traceback" in caplog.text
    assert len(caplog.text) <= 4_300
    for secret in ("db.internal", "report_user", "database-password", fernet_key, "smtp-secret"):
        assert secret not in caplog.text
