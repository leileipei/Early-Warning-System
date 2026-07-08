import importlib
import sys

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.models import (
    AlertRule,
    AlertSuppression,
    ExecutionLog,
    SendMode,
    SqlDataSource,
    TriggerType,
    utc_now,
)


def _create_rule(session):
    source = SqlDataSource(
        name="prod",
        host="db.example.com",
        port=1433,
        database="erp",
        username="readonly",
        encrypted_password="encrypted",
        enabled=True,
    )
    session.add(source)
    session.commit()
    session.refresh(source)

    rule = AlertRule(
        name="large orders",
        data_source_id=source.id,
        sql_text="select id, amount from orders where amount > 10000",
        cron_expression="0 9 * * *",
        recipients="ops@example.com",
        subject_template="大额订单预警",
        body_template="{{table}}",
        send_mode=SendMode.SUMMARY,
        enabled=True,
    )
    session.add(rule)
    session.commit()
    session.refresh(rule)
    return rule


def test_create_rule_with_sql_server_source(session):
    rule = _create_rule(session)

    assert rule.id is not None
    assert rule.send_mode == SendMode.SUMMARY
    assert rule.dynamic_recipient_field == ""
    assert rule.dynamic_cc_field == ""


def test_import_db_without_required_secrets(tmp_path, monkeypatch):
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    sys.modules.pop("app.db", None)

    imported = importlib.import_module("app.db")

    assert imported is not None


def test_init_db_creates_model_tables(engine):
    table_names = set(inspect(engine).get_table_names())

    assert {
        "adminuser",
        "sqldatasource",
        "smtpconfig",
        "alertrule",
        "alertsuppression",
        "executionlog",
        "maillog",
    } <= table_names


def test_init_db_adds_sql_server_connection_option_columns_to_existing_sqlite_table(tmp_path):
    from app.db import create_db_engine, init_db

    database_path = tmp_path / "legacy.sqlite3"
    legacy_engine = create_db_engine(f"sqlite:///{database_path}")
    with legacy_engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE sqldatasource (
                    id INTEGER PRIMARY KEY,
                    name VARCHAR NOT NULL,
                    host VARCHAR NOT NULL,
                    port INTEGER NOT NULL,
                    database VARCHAR NOT NULL,
                    username VARCHAR NOT NULL,
                    encrypted_password VARCHAR NOT NULL,
                    enabled BOOLEAN NOT NULL,
                    connect_timeout_seconds INTEGER NOT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
                """
            )
        )

    init_db(legacy_engine)

    columns = {column["name"] for column in inspect(legacy_engine).get_columns("sqldatasource")}
    assert {
        "odbc_driver",
        "server_override",
        "encrypt",
        "trust_server_certificate",
        "extra_params",
    } <= columns


def test_init_db_adds_duplicate_suppression_columns_to_existing_sqlite_rule_table(tmp_path):
    from app.db import create_db_engine, init_db

    database_path = tmp_path / "legacy_rules.sqlite3"
    legacy_engine = create_db_engine(f"sqlite:///{database_path}")
    with legacy_engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE alertrule (
                    id INTEGER PRIMARY KEY,
                    name VARCHAR NOT NULL,
                    data_source_id INTEGER NOT NULL,
                    sql_text VARCHAR NOT NULL,
                    cron_expression VARCHAR NOT NULL,
                    recipients VARCHAR NOT NULL,
                    cc_recipients VARCHAR NOT NULL,
                    subject_template VARCHAR NOT NULL,
                    body_template VARCHAR NOT NULL,
                    send_mode VARCHAR NOT NULL,
                    query_timeout_seconds INTEGER NOT NULL,
                    max_rows INTEGER NOT NULL,
                    enabled BOOLEAN NOT NULL,
                    notes VARCHAR NOT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
                """
            )
        )

    init_db(legacy_engine)

    columns = {column["name"] for column in inspect(legacy_engine).get_columns("alertrule")}
    assert {
        "suppress_duplicates",
        "suppression_key_field",
        "suppression_window_hours",
    } <= columns


def test_init_db_adds_dynamic_recipient_columns_to_existing_sqlite_rule_table(tmp_path):
    from app.db import create_db_engine, init_db

    database_path = tmp_path / "legacy_dynamic_recipients.sqlite3"
    legacy_engine = create_db_engine(f"sqlite:///{database_path}")
    with legacy_engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE alertrule (
                    id INTEGER PRIMARY KEY,
                    name VARCHAR NOT NULL,
                    data_source_id INTEGER NOT NULL,
                    sql_text VARCHAR NOT NULL,
                    cron_expression VARCHAR NOT NULL,
                    recipients VARCHAR NOT NULL,
                    cc_recipients VARCHAR NOT NULL,
                    subject_template VARCHAR NOT NULL,
                    body_template VARCHAR NOT NULL,
                    send_mode VARCHAR NOT NULL,
                    query_timeout_seconds INTEGER NOT NULL,
                    max_rows INTEGER NOT NULL,
                    enabled BOOLEAN NOT NULL,
                    notes VARCHAR NOT NULL,
                    suppress_duplicates BOOLEAN NOT NULL,
                    suppression_key_field VARCHAR NOT NULL,
                    suppression_window_hours INTEGER NOT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
                """
            )
        )

    init_db(legacy_engine)

    columns = {column["name"] for column in inspect(legacy_engine).get_columns("alertrule")}
    assert {"dynamic_recipient_field", "dynamic_cc_field"} <= columns


def test_alert_suppression_persists_for_rule(session):
    rule = _create_rule(session)
    suppression = AlertSuppression(rule_id=rule.id, suppression_key="order-1001")
    session.add(suppression)
    session.commit()
    session.refresh(suppression)

    assert suppression.id is not None
    assert suppression.rule_id == rule.id
    assert suppression.suppression_key == "order-1001"
    assert suppression.hit_count == 1


def test_sqlite_foreign_keys_are_enforced(session):
    session.add(ExecutionLog(rule_id=999, trigger_type=TriggerType.MANUAL))

    with pytest.raises(IntegrityError):
        session.commit()


def test_alert_rule_persists_across_new_session(engine, session):
    rule = _create_rule(session)

    with Session(engine) as new_session:
        persisted_rule = new_session.exec(
            select(AlertRule).where(AlertRule.id == rule.id)
        ).one()

    assert persisted_rule.name == "large orders"
    assert persisted_rule.send_mode == SendMode.SUMMARY


def test_utc_now_returns_naive_utc_datetime():
    timestamp = utc_now()

    assert timestamp.tzinfo is None
