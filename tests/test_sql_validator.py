import pytest

from app.sql_validator import SqlValidationError, validate_select_sql


def test_allows_plain_select():
    validate_select_sql("select id, amount from orders where amount > 10000")


def test_allows_cte_select():
    validate_select_sql("with recent as (select id from orders) select * from recent")


def test_allows_single_trailing_semicolon():
    validate_select_sql("select id from orders;")


def test_allows_comments_without_treating_words_inside_as_sql():
    validate_select_sql("select id from orders -- update is just a comment")


@pytest.mark.parametrize(
    "sql",
    [
        "select 'drop' as label",
        'select "update" as label',
    ],
)
def test_allows_dangerous_words_inside_string_literals(sql):
    validate_select_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "select 1 -- ; delete from x",
        "select ';' as semicolon",
        "select 1; -- comment",
        "select 1; /* comment */",
    ],
)
def test_allows_semicolons_inside_comments_and_string_literals(sql):
    validate_select_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "select 1; 'tail'",
        'select 1; "tail"',
        "select 1; [tail]",
        "select 1; 2",
        "select 1; +",
        "select 1; tail",
    ],
)
def test_rejects_any_non_comment_content_after_real_semicolon(sql):
    with pytest.raises(SqlValidationError):
        validate_select_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "",
        "   ",
        "values (1)",
        "alter table orders add note varchar(100)",
        "create table audit (id int)",
        "update orders set amount = 0",
        "delete from orders",
        "insert into audit values (1)",
        "merge into orders using updates on orders.id = updates.id",
        "drop table orders",
        "exec dbo.build_warning",
        "execute dbo.build_warning",
        "truncate table orders",
        "select * into audit_copy from orders",
        "select * from orders into outfile '/tmp/x'",
        "select * from orders; delete from orders",
        "SELECT * FROM orders WHERE id IN (DELETE)",
        ";select 1",
        "select 1;;",
        "select 1; select 2",
    ],
)
def test_rejects_non_read_only_sql(sql):
    with pytest.raises(SqlValidationError):
        validate_select_sql(sql)
