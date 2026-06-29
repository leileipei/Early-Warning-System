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
            f"SERVER={host},{port};"
            f"DATABASE={database};"
            f"UID={username};"
            f"PWD={password};"
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
            raise RuntimeError("pyodbc is required to query SQL Server") from exc

        limited_sql = f"SELECT TOP ({max_rows}) * FROM ({sql}) AS warning_source"
        with pyodbc.connect(self.connection_string) as connection:
            cursor = connection.cursor()
            cursor.timeout = timeout_seconds
            cursor.execute(limited_sql)
            return rows_from_cursor(cursor)
