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
        "update orders set amount = 0",
        "delete from orders",
        "insert into audit values (1)",
        "drop table orders",
        "exec dbo.build_warning",
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
