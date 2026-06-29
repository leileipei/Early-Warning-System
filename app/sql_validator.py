import re


class SqlValidationError(ValueError):
    pass


DANGEROUS_WORDS = {
    "alter",
    "create",
    "delete",
    "drop",
    "exec",
    "execute",
    "insert",
    "merge",
    "truncate",
    "update",
}


def _strip_sql_comments(sql: str) -> str:
    without_block_comments = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    return re.sub(r"--.*?(?=\n|$)", " ", without_block_comments)


def validate_select_sql(sql: str) -> None:
    normalized = sql.strip().strip(";").strip()
    if not normalized:
        raise SqlValidationError("SQL 不能为空")

    sql_without_comments = _strip_sql_comments(normalized)
    if ";" in sql_without_comments:
        raise SqlValidationError("只允许单条 SELECT 查询")

    lowered = re.sub(r"\s+", " ", sql_without_comments.lower()).strip()
    first_word = lowered.split(" ", 1)[0]
    if first_word not in {"select", "with"}:
        raise SqlValidationError("只允许 SELECT 查询")

    words = set(re.findall(r"[a-z_]+", lowered))
    blocked = words.intersection(DANGEROUS_WORDS)
    if blocked:
        blocked_list = ", ".join(sorted(blocked))
        raise SqlValidationError(f"SQL 包含禁止关键字: {blocked_list}")
