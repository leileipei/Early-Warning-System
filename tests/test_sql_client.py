import sys
from dataclasses import dataclass

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
    assert "SERVER={db.example.internal},1433;" in client.connection_string
    assert "DATABASE={warnings};" in client.connection_string
    assert "UID={warning_user};" in client.connection_string
    assert "PWD={secret};" in client.connection_string
    assert "Encrypt=yes;" in client.connection_string
    assert "TrustServerCertificate=yes;" in client.connection_string
    assert "Connection Timeout=5;" in client.connection_string


def test_pyodbc_sql_server_client_supports_advanced_connection_options():
    client = PyodbcSqlServerClient(
        host="db.example.internal",
        port=1433,
        database="warnings",
        username="warning_user",
        password="secret",
        connect_timeout_seconds=5,
        odbc_driver="ODBC Driver 17 for SQL Server",
        server_override=r"db.example.internal\\REPORTING",
        encrypt="optional",
        trust_server_certificate="no",
        extra_params="ApplicationIntent=ReadOnly; MultiSubnetFailover=Yes; ",
    )

    assert "DRIVER={ODBC Driver 17 for SQL Server};" in client.connection_string
    assert r"SERVER={db.example.internal\\REPORTING};" in client.connection_string
    assert "Encrypt=optional;" in client.connection_string
    assert "TrustServerCertificate=no;" in client.connection_string
    assert "ApplicationIntent=ReadOnly;" in client.connection_string
    assert "MultiSubnetFailover=Yes;" in client.connection_string
    assert "SERVER={db.example.internal},1433;" not in client.connection_string


def test_pyodbc_sql_server_client_escapes_braced_connection_string_values():
    client = PyodbcSqlServerClient(
        host="db.example.internal",
        port=1433,
        database="warnings",
        username="warning_user",
        password="sec;ret}x",
        connect_timeout_seconds=5,
    )

    assert "PWD={sec;ret}}x};" in client.connection_string
    assert "ret}x;" not in client.connection_string


class FakePyodbcCursor(FakeCursor):
    def __init__(self):
        self.timeout = None
        self.executed_sql = None
        self.max_rows = None

    def execute(self, sql):
        self.executed_sql = sql

    def fetchmany(self, max_rows):
        self.max_rows = max_rows
        return [(1, 12000), (2, 15000)]


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


def test_query_executes_original_sql_limits_rows_with_fetchmany_and_sets_timeout(monkeypatch):
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
    assert cursor.executed_sql == "select id, amount from orders"
    assert cursor.max_rows == 25
    assert result == QueryResult(rows=[{"id": 1, "amount": 12000}, {"id": 2, "amount": 15000}])


@pytest.mark.parametrize(
    ("sql", "expected_sql"),
    [
        (
            "with recent as (select id from orders) select * from recent",
            "with recent as (select id from orders) select * from recent",
        ),
        (
            "select id, amount from orders order by amount desc",
            "select id, amount from orders order by amount desc",
        ),
        ("select id, amount from orders;  \n", "select id, amount from orders"),
    ],
)
def test_query_preserves_cte_and_order_by_and_strips_trailing_semicolon(
    monkeypatch, sql, expected_sql
):
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

    client.query(sql, timeout_seconds=7, max_rows=25)

    assert cursor.executed_sql == expected_sql
    assert "SELECT TOP" not in cursor.executed_sql
    assert cursor.max_rows == 25


def test_validate_syntax_asks_sql_server_to_parse_without_executing(monkeypatch):
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

    client.validate_syntax("select id from orders;  \n", timeout_seconds=9)

    assert fake_pyodbc.connection_string == client.connection_string
    assert cursor.timeout == 9
    assert cursor.executed_sql == "SET PARSEONLY ON;\nselect id from orders;\nSET PARSEONLY OFF;"


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


@dataclass
class MissingDependencyImporter:
    missing_name: str

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "pyodbc":
            raise ModuleNotFoundError(
                f"No module named '{self.missing_name}'",
                name=self.missing_name,
            )
        return None


def test_query_wraps_missing_pyodbc_import_error(monkeypatch):
    monkeypatch.delitem(sys.modules, "pyodbc", raising=False)
    importer = MissingDependencyImporter("pyodbc")
    monkeypatch.setattr(sys, "meta_path", [importer])
    client = PyodbcSqlServerClient(
        host="db.example.internal",
        port=1433,
        database="warnings",
        username="warning_user",
        password="secret",
        connect_timeout_seconds=5,
    )

    with pytest.raises(RuntimeError, match="pyodbc is required to query SQL Server"):
        client.query("select id from orders", timeout_seconds=7, max_rows=25)


def test_query_does_not_hide_pyodbc_internal_import_errors(monkeypatch):
    monkeypatch.delitem(sys.modules, "pyodbc", raising=False)
    importer = MissingDependencyImporter("pyodbc_dependency")
    monkeypatch.setattr(sys, "meta_path", [importer])
    client = PyodbcSqlServerClient(
        host="db.example.internal",
        port=1433,
        database="warnings",
        username="warning_user",
        password="secret",
        connect_timeout_seconds=5,
    )

    with pytest.raises(ModuleNotFoundError) as exc_info:
        client.query("select id from orders", timeout_seconds=7, max_rows=25)

    assert exc_info.value.name == "pyodbc_dependency"
