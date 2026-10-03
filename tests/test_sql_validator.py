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
        "select * from openquery(remote_server, 'delete from orders')",
        "select * from openrowset('SQLNCLI', 'Server=remote;', 'exec dbo.build_warning')",
        "select * from opendatasource('SQLNCLI', 'Data Source=remote').db.dbo.orders",
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


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 AS id GRANT SELECT TO public",
        "SELECT 1 DENY SELECT TO public",
        "SELECT 1 REVOKE SELECT FROM public",
        "SELECT 1 WAITFOR DELAY '00:00:05'",
        "SELECT 1 DBCC CHECKDB",
        "SELECT 1 SELECT 2",
        "SELECT 1 SET PARSEONLY OFF DELETE FROM orders",
        "SELECT NEXT VALUE FOR dbo.warning_sequence",
        "SELECT 1 AS id WITH c AS (SELECT 2 AS id) SELECT id FROM c",
    ],
)
def test_rejects_unseparated_batches_and_state_changing_queries(sql):
    with pytest.raises(SqlValidationError):
        validate_select_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT [update], [delete], [into] FROM [drop]",
        'SELECT "update", "delete" FROM "drop"',
        "SELECT [semi;colon] FROM [orders]",
        "SELECT TOP (5) [update] FROM dbo.orders ORDER BY [update] DESC",
        "WITH c AS (SELECT [delete] AS id FROM dbo.orders) SELECT id FROM c UNION ALL SELECT 1",
        "SELECT 1 /* outer /* inner */ DROP is still a comment */",
    ],
)
def test_accepts_quoted_identifiers_and_read_only_tsql_structures(sql):
    validate_select_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "WITH c AS (VACUUM 'PWD=validator-secret') SELECT * FROM c",
        "WITH c AS (FOOBAR 'PWD=validator-secret') SELECT * FROM c",
    ],
)
def test_rejects_unsupported_cte_bodies_without_leaking_sql(sql, caplog):
    with pytest.raises(SqlValidationError) as error:
        validate_select_sql(sql)
    assert "validator-secret" not in str(error.value)
    assert "validator-secret" not in caplog.text
