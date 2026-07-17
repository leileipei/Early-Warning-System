from collections.abc import Generator

from sqlalchemy import event, inspect, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.settings import get_settings

_engine: Engine | None = None
_schema_initialized = False

_LOG_INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS ix_executionlog_started_at ON executionlog (started_at)",
    "CREATE INDEX IF NOT EXISTS ix_executionlog_status ON executionlog (status)",
    "CREATE INDEX IF NOT EXISTS ix_executionlog_rule_id ON executionlog (rule_id)",
    "CREATE INDEX IF NOT EXISTS ix_maillog_sent_at ON maillog (sent_at)",
    "CREATE INDEX IF NOT EXISTS ix_maillog_status ON maillog (status)",
    "CREATE INDEX IF NOT EXISTS ix_maillog_execution_log_id ON maillog (execution_log_id)",
)


def create_db_engine(database_url: str | None = None) -> Engine:
    url = database_url or get_settings().database_url
    connect_args = (
        {"check_same_thread": False, "timeout": 30} if url.startswith("sqlite") else {}
    )
    engine_args = {"poolclass": StaticPool} if url in {"sqlite://", "sqlite:///:memory:"} else {}
    engine = create_engine(url, connect_args=connect_args, **engine_args)

    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
            finally:
                cursor.close()

    return engine


def get_engine() -> Engine:
    global _engine

    if _engine is None:
        _engine = create_db_engine()
    return _engine


def init_db(engine: Engine | None = None) -> None:
    import app.models  # noqa: F401

    target_engine = engine if engine is not None else get_engine()
    if target_engine.dialect.name == "sqlite":
        with target_engine.connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                SQLModel.metadata.create_all(connection)
                migrate_sqlite_schema(connection)
            except Exception:
                connection.rollback()
                raise
            connection.commit()
        return

    SQLModel.metadata.create_all(target_engine)


def ensure_schema_initialized() -> None:
    global _schema_initialized

    if _schema_initialized:
        return
    init_db()
    _schema_initialized = True


def migrate_sqlite_schema(bind: Engine | Connection) -> None:
    if bind.dialect.name != "sqlite":
        return

    if isinstance(bind, Engine):
        with bind.begin() as connection:
            _migrate_sqlite_schema(connection)
        return

    _migrate_sqlite_schema(bind)


def _migrate_sqlite_schema(connection: Connection) -> None:
    inspector = inspect(connection)
    table_names = set(inspector.get_table_names())
    if "sqldatasource" in table_names:
        existing_columns = {
            column["name"] for column in inspector.get_columns("sqldatasource")
        }
        columns_to_add = {
            "odbc_driver": "VARCHAR NOT NULL DEFAULT 'ODBC Driver 18 for SQL Server'",
            "server_override": "VARCHAR NOT NULL DEFAULT ''",
            "encrypt": "VARCHAR NOT NULL DEFAULT 'yes'",
            "trust_server_certificate": "VARCHAR NOT NULL DEFAULT 'yes'",
            "extra_params": "VARCHAR NOT NULL DEFAULT ''",
        }
        for column_name, ddl in columns_to_add.items():
            if column_name not in existing_columns:
                connection.execute(
                    text(f"ALTER TABLE sqldatasource ADD COLUMN {column_name} {ddl}")
                )

    if "alertrule" in table_names:
        existing_rule_columns = {
            column["name"] for column in inspector.get_columns("alertrule")
        }
        rule_columns_to_add = {
            "dynamic_recipient_field": "VARCHAR NOT NULL DEFAULT ''",
            "dynamic_cc_field": "VARCHAR NOT NULL DEFAULT ''",
            "suppress_duplicates": "BOOLEAN NOT NULL DEFAULT 0",
            "suppression_key_field": "VARCHAR NOT NULL DEFAULT ''",
            "suppression_window_hours": "INTEGER NOT NULL DEFAULT 24",
            "archived_at": "TIMESTAMP",
        }
        for column_name, ddl in rule_columns_to_add.items():
            if column_name not in existing_rule_columns:
                connection.execute(text(f"ALTER TABLE alertrule ADD COLUMN {column_name} {ddl}"))

    heartbeat_columns = {
        "id",
        "worker_id",
        "started_at",
        "last_seen_at",
        "last_sync_ok",
        "last_error",
    }
    if "workerheartbeat" in table_names:
        existing_heartbeat_columns = {
            column["name"] for column in inspector.get_columns("workerheartbeat")
        }
        missing_columns = heartbeat_columns - existing_heartbeat_columns
        if missing_columns:
            columns = ", ".join(sorted(missing_columns))
            raise RuntimeError(f"workerheartbeat schema is incomplete: {columns}")

    if {"executionlog", "maillog"} <= table_names:
        for statement in _LOG_INDEX_STATEMENTS:
            connection.exec_driver_sql(statement)


def get_session() -> Generator[Session, None, None]:
    ensure_schema_initialized()
    with Session(get_engine()) as session:
        yield session
