import importlib
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock

import pytest
from sqlalchemy import Index, event, func, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, select

from app.models import (
    AdminUser,
    AlertRule,
    AlertSuppression,
    AlertRuleVersion,
    ExecutionLog,
    RuleExecutionLease,
    SendMode,
    SmtpConfig,
    SqlDataSource,
    TriggerType,
    WorkerHeartbeat,
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
        "alertruleversion",
        "executionlog",
        "maillog",
    } <= table_names


def test_init_db_adds_session_version_to_existing_admin_user_table(tmp_path):
    from app.db import create_db_engine, init_db

    database_path = tmp_path / "legacy-admin.sqlite3"
    engine = create_db_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE adminuser (
                    id INTEGER PRIMARY KEY,
                    username VARCHAR NOT NULL,
                    password_hash VARCHAR NOT NULL,
                    created_at DATETIME NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO adminuser (id, username, password_hash, created_at)
                VALUES (1, 'admin', 'legacy-hash', '2026-01-01 00:00:00')
                """
            )
        )

    init_db(engine)
    init_db(engine)

    columns = {column["name"] for column in inspect(engine).get_columns("adminuser")}
    assert "session_version" in columns
    with Session(engine) as session:
        user = session.get(AdminUser, 1)
        assert user is not None
        assert user.session_version == 1


def test_init_db_creates_rule_execution_lease_table(engine):
    assert "ruleexecutionlease" in inspect(engine).get_table_names()


def test_init_db_adds_worker_heartbeat_table_to_existing_database(tmp_path):
    from app.db import create_db_engine, init_db

    database_path = tmp_path / "legacy.sqlite3"
    legacy_engine = create_db_engine(f"sqlite:///{database_path}")
    legacy_tables = [
        table
        for table in SQLModel.metadata.sorted_tables
        if table.name != "workerheartbeat"
    ]
    SQLModel.metadata.create_all(legacy_engine, tables=legacy_tables)

    init_db(legacy_engine)

    assert "workerheartbeat" in inspect(legacy_engine).get_table_names()
    columns = {
        item["name"] for item in inspect(legacy_engine).get_columns("workerheartbeat")
    }
    assert columns == {
        "id",
        "worker_id",
        "started_at",
        "last_seen_at",
        "last_sync_ok",
        "last_error",
    }


def test_init_db_is_idempotent_after_worker_heartbeat_exists(engine):
    from app.db import init_db
    from app.worker_health import record_worker_start

    with Session(engine) as session:
        record_worker_start(session, "worker-a", now=utc_now())

    init_db(engine)
    init_db(engine)

    with Session(engine) as session:
        assert session.exec(select(func.count()).select_from(WorkerHeartbeat)).one() == 1


def test_init_db_adds_log_indexes_to_existing_sqlite_database(tmp_path):
    from app.db import create_db_engine, init_db

    database_path = tmp_path / "legacy-log-indexes.sqlite3"
    engine = create_db_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE executionlog ("
                "id INTEGER PRIMARY KEY, rule_id INTEGER NOT NULL, trigger_type VARCHAR NOT NULL, "
                "status VARCHAR NOT NULL, started_at DATETIME NOT NULL, finished_at DATETIME, "
                "row_count INTEGER NOT NULL, email_count INTEGER NOT NULL, duration_ms INTEGER NOT NULL, "
                "error_type VARCHAR NOT NULL, error_message VARCHAR NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE maillog ("
                "id INTEGER PRIMARY KEY, execution_log_id INTEGER NOT NULL, recipients VARCHAR NOT NULL, "
                "cc_recipients VARCHAR NOT NULL, subject VARCHAR NOT NULL, status VARCHAR NOT NULL, "
                "error_message VARCHAR NOT NULL, sent_at DATETIME NOT NULL)"
            )
        )

    init_db(engine)
    init_db(engine)

    assert {
        "ix_executionlog_started_at",
        "ix_executionlog_status",
        "ix_executionlog_rule_id",
    } <= {index["name"] for index in inspect(engine).get_indexes("executionlog")}
    assert {
        "ix_maillog_sent_at",
        "ix_maillog_status",
        "ix_maillog_execution_log_id",
    } <= {index["name"] for index in inspect(engine).get_indexes("maillog")}


def test_init_db_keeps_newest_enabled_smtp_config_and_creates_unique_index(tmp_path):
    from app.db import create_db_engine, init_db

    database_path = tmp_path / "legacy-smtp.sqlite3"
    engine = create_db_engine(f"sqlite:///{database_path}")
    SQLModel.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO smtpconfig "
                "(id, host, port, username, encrypted_password, sender, use_tls, use_ssl, "
                "timeout_seconds, enabled, updated_at) VALUES "
                "(1, 'old.example.com', 587, 'mailer', 'encrypted', 'alerts@example.com', "
                "1, 0, 10, 1, '2026-01-01 00:00:00'), "
                "(2, 'newer.example.com', 587, 'mailer', 'encrypted', 'alerts@example.com', "
                "1, 0, 10, 1, '2026-01-02 00:00:00'), "
                "(3, 'latest-id.example.com', 587, 'mailer', 'encrypted', 'alerts@example.com', "
                "1, 0, 10, 1, '2026-01-02 00:00:00')"
            )
        )

    init_db(engine)
    init_db(engine)

    with Session(engine) as session:
        enabled_configs = session.exec(
            select(SmtpConfig).where(SmtpConfig.enabled == True)  # noqa: E712
        ).all()
        assert [config.id for config in enabled_configs] == [3]
        session.add(
            SmtpConfig(
                host="conflict.example.com",
                username="mailer",
                encrypted_password="encrypted",
                sender="alerts@example.com",
                enabled=True,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()

    assert "uq_smtpconfig_single_enabled" in {
        index["name"] for index in inspect(engine).get_indexes("smtpconfig")
    }


def test_init_db_rolls_back_smtp_cleanup_when_unique_index_creation_fails(tmp_path):
    from app.db import create_db_engine, init_db

    database_path = tmp_path / "failed-smtp-migration.sqlite3"
    engine = create_db_engine(f"sqlite:///{database_path}")
    SQLModel.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO smtpconfig "
                "(id, host, port, username, encrypted_password, sender, use_tls, use_ssl, "
                "timeout_seconds, enabled, updated_at) VALUES "
                "(1, 'first.example.com', 587, 'mailer', 'encrypted', 'alerts@example.com', "
                "1, 0, 10, 1, '2026-01-01 00:00:00'), "
                "(2, 'second.example.com', 587, 'mailer', 'encrypted', 'alerts@example.com', "
                "1, 0, 10, 1, '2026-01-02 00:00:00')"
            )
        )

    def fail_unique_index(_connection, _cursor, statement, _parameters, _context, _executemany):
        if "CREATE UNIQUE INDEX IF NOT EXISTS uq_smtpconfig_single_enabled" in statement:
            raise RuntimeError("injected smtp index creation failure")

    event.listen(engine, "before_cursor_execute", fail_unique_index)
    try:
        with pytest.raises(RuntimeError, match="smtp index creation failure"):
            init_db(engine)
    finally:
        event.remove(engine, "before_cursor_execute", fail_unique_index)

    with engine.connect() as connection:
        enabled_ids = connection.execute(
            text("SELECT id FROM smtpconfig WHERE enabled = 1 ORDER BY id")
        ).scalars().all()
    assert enabled_ids == [1, 2]
    assert "uq_smtpconfig_single_enabled" not in {
        index["name"] for index in inspect(engine).get_indexes("smtpconfig")
    }


def test_init_db_rolls_back_sqlite_upgrade_when_worker_heartbeat_index_creation_fails(tmp_path):
    from app.db import create_db_engine, init_db

    database_path = tmp_path / "failed-heartbeat-index.sqlite3"
    engine = create_db_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE sqldatasource (id INTEGER PRIMARY KEY)"))

    failure_index = Index(
        "ix_workerheartbeat_rollback_test",
        WorkerHeartbeat.__table__.c.worker_id,
    )

    def fail_before_index_create(_target, _connection, **_kwargs):
        raise RuntimeError("injected worker heartbeat index creation failure")

    event.listen(failure_index, "before_create", fail_before_index_create)
    try:
        with pytest.raises(RuntimeError, match="worker heartbeat index creation failure"):
            init_db(engine)
    finally:
        event.remove(failure_index, "before_create", fail_before_index_create)
        WorkerHeartbeat.__table__.indexes.remove(failure_index)

    assert set(inspect(engine).get_table_names()) == {"sqldatasource"}
    assert {column["name"] for column in inspect(engine).get_columns("sqldatasource")} == {
        "id"
    }


def test_rule_execution_lease_uses_rule_as_primary_key(session):
    rule = _create_rule(session)
    lease = RuleExecutionLease(
        rule_id=rule.id,
        owner_token="owner-a",
        expires_at=utc_now(),
    )
    session.add(lease)
    session.commit()

    assert session.get(RuleExecutionLease, rule.id).owner_token == "owner-a"


def test_init_db_adds_rule_execution_lease_table_to_existing_database(tmp_path):
    from app.db import create_db_engine, init_db

    database_path = tmp_path / "legacy.sqlite3"
    legacy_engine = create_db_engine(f"sqlite:///{database_path}")
    with legacy_engine.begin() as connection:
        connection.execute(text("CREATE TABLE legacy_marker (id INTEGER PRIMARY KEY)"))

    init_db(legacy_engine)

    assert "ruleexecutionlease" in inspect(legacy_engine).get_table_names()


def test_init_db_serializes_concurrent_sqlite_upgrade_missing_only_lease_table(tmp_path):
    from app.db import create_db_engine, init_db

    database_path = tmp_path / "concurrent-legacy.sqlite3"
    database_url = f"sqlite:///{database_path}"
    seed_engine = create_db_engine(database_url)
    expected_tables = {table.name for table in SQLModel.metadata.sorted_tables}
    legacy_tables = [
        table
        for table in SQLModel.metadata.sorted_tables
        if table.name != "ruleexecutionlease"
    ]
    SQLModel.metadata.create_all(seed_engine, tables=legacy_tables)
    assert set(inspect(seed_engine).get_table_names()) == expected_tables - {
        "ruleexecutionlease"
    }
    seed_engine.dispose()

    engines = [create_db_engine(database_url), create_db_engine(database_url)]
    begin_barrier = Barrier(3)
    begin_attempts = set()
    lease_create_calls = []
    calls_lock = Lock()

    def make_begin_listener(call_number):
        def synchronize_begin(
            _connection,
            _cursor,
            statement,
            _parameters,
            _context,
            _executemany,
        ):
            if statement.strip().upper() != "BEGIN IMMEDIATE":
                return
            with calls_lock:
                if call_number in begin_attempts:
                    return
                begin_attempts.add(call_number)
            begin_barrier.wait(timeout=5)

        return synchronize_begin

    begin_listeners = [make_begin_listener(index) for index in range(len(engines))]
    for engine, listener in zip(engines, begin_listeners, strict=True):
        event.listen(engine, "before_cursor_execute", listener)

    def record_lease_creation(_target, _connection, **_kwargs):
        with calls_lock:
            lease_create_calls.append(1)

    event.listen(RuleExecutionLease.__table__, "before_create", record_lease_creation)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(init_db, engine) for engine in engines]
            begin_barrier.wait(timeout=5)
            assert begin_attempts == {0, 1}
            for future in futures:
                future.result()
            assert len(lease_create_calls) == 1
    finally:
        event.remove(
            RuleExecutionLease.__table__,
            "before_create",
            record_lease_creation,
        )
        for engine, listener in zip(engines, begin_listeners, strict=True):
            event.remove(engine, "before_cursor_execute", listener)
            engine.dispose()

    verification_engine = create_db_engine(database_url)
    try:
        assert set(inspect(verification_engine).get_table_names()) == expected_tables
    finally:
        verification_engine.dispose()


def test_init_db_rolls_back_partial_sqlite_initialization_after_create_failure(tmp_path):
    from app.db import create_db_engine, init_db

    database_path = tmp_path / "failed-initialization.sqlite3"
    database_url = f"sqlite:///{database_path}"
    engine = create_db_engine(database_url)
    expected_tables = {table.name for table in SQLModel.metadata.sorted_tables}
    tables_seen_before_failure = set()

    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE sqldatasource (id INTEGER PRIMARY KEY)"))

    def fail_before_lease_create(_target, connection, **_kwargs):
        tables_seen_before_failure.update(inspect(connection).get_table_names())
        raise RuntimeError("injected schema initialization failure")

    event.listen(RuleExecutionLease.__table__, "before_create", fail_before_lease_create)
    try:
        with pytest.raises(RuntimeError, match="injected schema initialization failure"):
            init_db(engine)
    finally:
        event.remove(
            RuleExecutionLease.__table__,
            "before_create",
            fail_before_lease_create,
        )

    assert {"adminuser", "alertrule"} <= tables_seen_before_failure
    assert set(inspect(engine).get_table_names()) == {"sqldatasource"}
    assert {column["name"] for column in inspect(engine).get_columns("sqldatasource")} == {
        "id"
    }

    init_db(engine)

    assert set(inspect(engine).get_table_names()) == expected_tables
    assert "odbc_driver" in {
        column["name"] for column in inspect(engine).get_columns("sqldatasource")
    }
    engine.dispose()


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
        "archived_at",
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


def test_alert_rule_version_persists_for_rule(session):
    rule = _create_rule(session)
    version = AlertRuleVersion(
        rule_id=rule.id,
        version_number=1,
        changed_by="admin",
        snapshot_json='{"name": "large orders"}',
    )
    session.add(version)
    session.commit()
    session.refresh(version)

    assert version.id is not None
    assert version.rule_id == rule.id
    assert version.version_number == 1
    assert version.changed_by == "admin"
    assert version.snapshot_json == '{"name": "large orders"}'


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
