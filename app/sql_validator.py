from sqlglot import ErrorLevel, exp
from sqlglot.dialects.tsql import TSQL
from sqlglot.errors import ParseError, TokenError
from sqlglot.tokens import TokenType


class SqlValidationError(ValueError):
    pass


class _ReadOnlyTSQLParser(TSQL.Parser):
    def _warn_unsupported(self) -> None:
        # Reject fallback commands without logging SQL text or embedded credentials.
        raise ParseError("Unsupported SQL structure")


_QUERY_TYPES = (exp.Select, exp.SetOperation)
_UNSAFE_NODES = (exp.DDL, exp.DML, exp.Command, exp.Into, exp.NextValueFor, exp.Lock)
_EXTERNAL_FUNCTIONS = {"OPENQUERY", "OPENROWSET", "OPENDATASOURCE"}
_STATEMENT_KEYWORDS = {
    "ALTER", "CREATE", "DELETE", "DROP", "EXEC", "EXECUTE", "INSERT", "MERGE",
    "TRUNCATE", "UPDATE", "GRANT", "DENY", "REVOKE", "DBCC", "WAITFOR",
}


def validate_select_sql(sql: str) -> None:
    normalized = sql.strip()
    if not normalized:
        raise SqlValidationError("SQL 不能为空")

    try:
        dialect = TSQL()
        tokens = dialect.tokenize(normalized)
        if not tokens or tokens[0].token_type not in {TokenType.SELECT, TokenType.WITH}:
            raise SqlValidationError("只允许 SELECT 查询")
        terminators = [
            index for index, token in enumerate(tokens) if token.token_type == TokenType.SEMICOLON
        ]
        if len(terminators) > 1 or (terminators and terminators[0] != len(tokens) - 1):
            raise SqlValidationError("只允许单条 SELECT 查询")

        statements = _ReadOnlyTSQLParser(dialect=dialect, error_level=ErrorLevel.IMMEDIATE).parse(
            tokens, sql=normalized
        )
    except (ParseError, TokenError, RecursionError) as exc:
        raise SqlValidationError("SQL 无法安全解析，请检查语法或不支持的查询结构") from exc

    statements = [statement for statement in statements if not isinstance(statement, exp.Semicolon)]
    if len(statements) != 1 or not isinstance(statements[0], _QUERY_TYPES):
        raise SqlValidationError("只允许单条 SELECT 查询")

    for node in statements[0].walk():
        if isinstance(node, _UNSAFE_NODES):
            raise SqlValidationError("只允许只读 SELECT 查询，不允许修改数据或数据库状态")
        if isinstance(node, exp.CTE) and not isinstance(node.this.unnest(), _QUERY_TYPES):
            raise SqlValidationError("WITH 子查询必须是只读 SELECT 查询")
        if isinstance(node, exp.Anonymous) and node.name.upper() in _EXTERNAL_FUNCTIONS:
            raise SqlValidationError("不允许外部数据源查询函数")
        if (
            isinstance(node, exp.Identifier)
            and not node.args.get("quoted")
            and node.name.upper() in _STATEMENT_KEYWORDS
        ):
            raise SqlValidationError("语句关键字作为标识符时必须使用方括号或双引号")


def validate_select_only_sql(sql: str) -> None:
    validate_select_sql(sql)
