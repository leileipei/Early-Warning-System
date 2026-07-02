import importlib
import sys

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.models import (
    AlertRule,
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
