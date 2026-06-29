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
