import sys

import pytest

from app.sql_client import PyodbcSqlServerClient, QueryResult, rows_from_cursor


class FakeCursor:
    description = [("id",), ("amount",)]

    def fetchall(self):
        return [(1, 12000), (2, 15000)]


def test_rows_from_cursor_returns_dicts():
    result = rows_from_cursor(FakeCursor())

    assert result == QueryResult(rows=[{"id": 1, "amount": 12000}, {"id": 2, "amount": 15000}])


def test_pyodbc_sql_server_client_builds_connection_string():
    client = PyodbcSqlServerClient(
        host="db.example.internal",
        port=1433,
        database="warnings",
        username="warning_user",
        password="secret",
        connect_timeout_seconds=5,
    )

    assert "DRIVER={ODBC Driver 18 for SQL Server};" in client.connection_string
    assert "SERVER=db.example.internal,1433;" in client.connection_string
    assert "DATABASE=warnings;" in client.connection_string
    assert "UID=warning_user;" in client.connection_string
    assert "PWD=secret;" in client.connection_string
    assert "Encrypt=yes;" in client.connection_string
    assert "TrustServerCertificate=yes;" in client.connection_string
    assert "Connection Timeout=5;" in client.connection_string


class FakePyodbcCursor(FakeCursor):
    def __init__(self):
        self.timeout = None
        self.executed_sql = None

    def execute(self, sql):
        self.executed_sql = sql


class FakePyodbcConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def cursor(self):
        return self._cursor


class FakePyodbc:
    def __init__(self, connection):
        self.connection = connection
        self.connection_string = None

    def connect(self, connection_string):
        self.connection_string = connection_string
        return self.connection


def test_query_wraps_sql_limits_rows_and_sets_timeout(monkeypatch):
    cursor = FakePyodbcCursor()
    fake_pyodbc = FakePyodbc(FakePyodbcConnection(cursor))
    monkeypatch.setitem(sys.modules, "pyodbc", fake_pyodbc)
    client = PyodbcSqlServerClient(
        host="db.example.internal",
        port=1433,
        database="warnings",
        username="warning_user",
        password="secret",
        connect_timeout_seconds=5,
    )

    result = client.query("select id, amount from orders", timeout_seconds=7, max_rows=25)

    assert fake_pyodbc.connection_string == client.connection_string
    assert cursor.timeout == 7
    assert cursor.executed_sql == (
        "SELECT TOP (25) * FROM (select id, amount from orders) AS warning_source"
    )
    assert result == QueryResult(rows=[{"id": 1, "amount": 12000}, {"id": 2, "amount": 15000}])


@pytest.mark.parametrize("max_rows", [0, -1, 1.5, "25"])
def test_query_rejects_invalid_max_rows_before_connecting(monkeypatch, max_rows):
    fake_pyodbc = FakePyodbc(FakePyodbcConnection(FakePyodbcCursor()))
    monkeypatch.setitem(sys.modules, "pyodbc", fake_pyodbc)
    client = PyodbcSqlServerClient(
        host="db.example.internal",
        port=1433,
        database="warnings",
        username="warning_user",
        password="secret",
        connect_timeout_seconds=5,
    )

    with pytest.raises(ValueError, match="max_rows must be a positive integer"):
        client.query("select id, amount from orders", timeout_seconds=7, max_rows=max_rows)

    assert fake_pyodbc.connection_string is None
