from collections.abc import Generator

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy import inspect, text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.settings import get_settings

_engine: Engine | None = None
_schema_initialized = False


def create_db_engine(database_url: str | None = None) -> Engine:
    url = database_url or get_settings().database_url
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
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
    SQLModel.metadata.create_all(target_engine)
    migrate_sqlite_schema(target_engine)


def ensure_schema_initialized() -> None:
    global _schema_initialized

    if _schema_initialized:
        return
    init_db()
    _schema_initialized = True


def migrate_sqlite_schema(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        return

    inspector = inspect(engine)
    if "sqldatasource" not in inspector.get_table_names():
        return

    existing_columns = {column["name"] for column in inspector.get_columns("sqldatasource")}
    columns_to_add = {
        "odbc_driver": "VARCHAR NOT NULL DEFAULT 'ODBC Driver 18 for SQL Server'",
        "server_override": "VARCHAR NOT NULL DEFAULT ''",
        "encrypt": "VARCHAR NOT NULL DEFAULT 'yes'",
        "trust_server_certificate": "VARCHAR NOT NULL DEFAULT 'yes'",
        "extra_params": "VARCHAR NOT NULL DEFAULT ''",
    }
    with engine.begin() as connection:
        for column_name, ddl in columns_to_add.items():
            if column_name not in existing_columns:
                connection.execute(text(f"ALTER TABLE sqldatasource ADD COLUMN {column_name} {ddl}"))

    if "alertrule" not in inspector.get_table_names():
        return

    existing_rule_columns = {column["name"] for column in inspector.get_columns("alertrule")}
    rule_columns_to_add = {
        "suppress_duplicates": "BOOLEAN NOT NULL DEFAULT 0",
        "suppression_key_field": "VARCHAR NOT NULL DEFAULT ''",
        "suppression_window_hours": "INTEGER NOT NULL DEFAULT 24",
    }
    with engine.begin() as connection:
        for column_name, ddl in rule_columns_to_add.items():
            if column_name not in existing_rule_columns:
                connection.execute(text(f"ALTER TABLE alertrule ADD COLUMN {column_name} {ddl}"))


def get_session() -> Generator[Session, None, None]:
    ensure_schema_initialized()
    with Session(get_engine()) as session:
        yield session
