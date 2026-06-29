from collections.abc import Generator

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.settings import get_settings

_engine: Engine | None = None


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


def get_session() -> Generator[Session, None, None]:
    with Session(get_engine()) as session:
        yield session
