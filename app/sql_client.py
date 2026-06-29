from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class QueryResult:
    rows: list[dict]


class SqlClient(Protocol):
    def query(self, sql: str, timeout_seconds: int, max_rows: int) -> QueryResult:
        raise NotImplementedError


def rows_from_cursor(cursor) -> QueryResult:
    columns = [column[0] for column in cursor.description]
    rows = [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
    return QueryResult(rows=rows)


def rows_from_cursor_limited(cursor, max_rows: int) -> QueryResult:
    columns = [column[0] for column in cursor.description]
    rows = [dict(zip(columns, row, strict=True)) for row in cursor.fetchmany(max_rows)]
    return QueryResult(rows=rows)


def odbc_brace_escape(value: str) -> str:
    return "{" + str(value).replace("}", "}}") + "}"


def strip_single_trailing_semicolon(sql: str) -> str:
    stripped_sql = sql.rstrip()
    if stripped_sql.endswith(";"):
        return stripped_sql[:-1].rstrip()
    return stripped_sql


class PyodbcSqlServerClient:
    def __init__(
        self,
        host: str,
        port: int,
        database: str,
        username: str,
        password: str,
        connect_timeout_seconds: int,
    ):
        self.connection_string = (
            "DRIVER={ODBC Driver 18 for SQL Server};"
            f"SERVER={odbc_brace_escape(host)},{port};"
            f"DATABASE={odbc_brace_escape(database)};"
            f"UID={odbc_brace_escape(username)};"
            f"PWD={odbc_brace_escape(password)};"
            "Encrypt=yes;"
            "TrustServerCertificate=yes;"
            f"Connection Timeout={connect_timeout_seconds};"
        )

    def query(self, sql: str, timeout_seconds: int, max_rows: int) -> QueryResult:
        if type(max_rows) is not int or max_rows < 1:
            raise ValueError("max_rows must be a positive integer")

        try:
            import pyodbc
        except ModuleNotFoundError as exc:
            if exc.name == "pyodbc":
                raise RuntimeError("pyodbc is required to query SQL Server") from exc
            raise

        executable_sql = strip_single_trailing_semicolon(sql)
        with pyodbc.connect(self.connection_string) as connection:
            cursor = connection.cursor()
            cursor.timeout = timeout_seconds
            cursor.execute(executable_sql)
            return rows_from_cursor_limited(cursor, max_rows)
