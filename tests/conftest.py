import pytest
from sqlmodel import Session


@pytest.fixture()
def engine():
    from app.db import create_db_engine, init_db

    engine = create_db_engine("sqlite:///:memory:")
    init_db(engine)
    return engine


@pytest.fixture()
def session(engine):
    with Session(engine) as session:
        yield session
