class SqlValidationError(ValueError):
    pass


DANGEROUS_WORDS = {
    "alter",
    "create",
    "delete",
    "drop",
    "exec",
    "execute",
    "into",
    "insert",
    "merge",
    "truncate",
    "update",
}


def _is_word_char(char: str) -> bool:
    return char.isalpha() or char == "_"


def _scan_sql(sql: str) -> tuple[list[str], int, bool]:
    words: list[str] = []
    semicolon_count = 0
    content_after_semicolon = False
    index = 0

    while index < len(sql):
        char = sql[index]
        next_char = sql[index + 1] if index + 1 < len(sql) else ""

        if char.isspace():
            index += 1
            continue

        if char == "-" and next_char == "-":
            index += 2
            while index < len(sql) and sql[index] != "\n":
                index += 1
            continue

        if char == "/" and next_char == "*":
            index += 2
            while index + 1 < len(sql) and not (sql[index] == "*" and sql[index + 1] == "/"):
                index += 1
            index = min(index + 2, len(sql))
            continue

        if semicolon_count:
            content_after_semicolon = True

        if char in {"'", '"'}:
            quote = char
            index += 1
            while index < len(sql):
                if sql[index] == quote:
                    if index + 1 < len(sql) and sql[index + 1] == quote:
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            continue

        if char == ";":
            semicolon_count += 1
            index += 1
            continue

        if _is_word_char(char):
            start = index
            while index < len(sql) and _is_word_char(sql[index]):
                index += 1
            words.append(sql[start:index].lower())
            continue

        index += 1

    return words, semicolon_count, content_after_semicolon


def validate_select_sql(sql: str) -> None:
    normalized = sql.strip()
    if not normalized:
        raise SqlValidationError("SQL 不能为空")

    words, semicolon_count, content_after_semicolon = _scan_sql(normalized)
    if semicolon_count > 1 or content_after_semicolon:
        raise SqlValidationError("只允许单条 SELECT 查询")

    first_word = words[0] if words else ""
    if first_word not in {"select", "with"}:
        raise SqlValidationError("只允许 SELECT 查询")

    blocked = set(words).intersection(DANGEROUS_WORDS)
    if blocked:
        blocked_list = ", ".join(sorted(blocked))
        raise SqlValidationError(f"SQL 包含禁止关键字: {blocked_list}")
