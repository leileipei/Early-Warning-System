import logging
from dataclasses import dataclass
from typing import Protocol

from app.error_reporting import log_exception_safely


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class QueryResult:
    rows: list[dict]


class SqlClient(Protocol):
    def query(self, sql: str, timeout_seconds: int, max_rows: int) -> QueryResult:
        raise NotImplementedError

    def validate_syntax(self, sql: str, timeout_seconds: int) -> None:
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


def normalize_extra_params(extra_params: str) -> str:
    parts = [part.strip() for part in extra_params.split(";") if part.strip()]
    if not parts:
        return ""
    return "".join(f"{part};" for part in parts)


class PyodbcSqlServerClient:
    def __init__(
        self,
        host: str,
        port: int,
        database: str,
        username: str,
        password: str,
        connect_timeout_seconds: int,
        odbc_driver: str = "ODBC Driver 18 for SQL Server",
        server_override: str = "",
        encrypt: str = "yes",
        trust_server_certificate: str = "no",
        extra_params: str = "",
    ):
        if server_override.strip():
            server_fragment = f"SERVER={odbc_brace_escape(server_override.strip())};"
        else:
            server_fragment = f"SERVER={odbc_brace_escape(f'{host},{port}')};"
        self.connection_string = (
            f"DRIVER={odbc_brace_escape(odbc_driver)};"
            f"{server_fragment}"
            f"DATABASE={odbc_brace_escape(database)};"
            f"UID={odbc_brace_escape(username)};"
            f"PWD={odbc_brace_escape(password)};"
            f"Encrypt={encrypt};"
            f"TrustServerCertificate={trust_server_certificate};"
            f"Connection Timeout={connect_timeout_seconds};"
            f"{normalize_extra_params(extra_params)}"
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
        try:
            with pyodbc.connect(self.connection_string) as connection:
                connection.timeout = timeout_seconds
                cursor = connection.cursor()
                cursor.execute(executable_sql)
                return rows_from_cursor_limited(cursor, max_rows)
        except Exception as exc:
            log_exception_safely(logger, "SQL Server query failed", exc)
            raise

    def validate_syntax(self, sql: str, timeout_seconds: int) -> None:
        try:
            import pyodbc
        except ModuleNotFoundError as exc:
            if exc.name == "pyodbc":
                raise RuntimeError("pyodbc is required to query SQL Server") from exc
            raise

        executable_sql = strip_single_trailing_semicolon(sql)
        parse_only_batch = f"SET PARSEONLY ON;\n{executable_sql};\nSET PARSEONLY OFF;"
        try:
            with pyodbc.connect(self.connection_string) as connection:
                connection.timeout = timeout_seconds
                cursor = connection.cursor()
                cursor.execute(parse_only_batch)
        except Exception as exc:
            log_exception_safely(logger, "SQL Server syntax validation failed", exc)
            raise
