import logging

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
