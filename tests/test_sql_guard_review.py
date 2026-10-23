"""Regressions for the code review of the SQL Guard (plan 05): one group per finding.

Each of these was reproduced against the guard before it was fixed. They sit in their own file so the matrix in
`test_sql_guard.py` keeps the plan's shape.
"""

import pytest
import sqlglot
from sqlglot import exp

from src.errors import ErrorKind
from tests.guard_support import (
    ALICE, CEO, accepted, make_allowlist, make_guard, refused, violation_rule,
)

RANKED = "SELECT user_id, SUM(sale_price) AS spend FROM order_items GROUP BY user_id ORDER BY spend DESC LIMIT 10"


@pytest.fixture(scope="module")
def guard():
    return make_guard()


def parse_output(sql: str) -> exp.Expression:
    (tree,) = sqlglot.parse(sql, read="bigquery")
    return tree


# 1. a set operation lines its branches up by position -------------------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "SELECT a FROM (SELECT 0 AS a, 0 AS b UNION ALL SELECT user_id AS b, 0 AS a FROM orders)",
    "WITH t AS (SELECT 0 AS a, 0 AS b UNION ALL SELECT user_id AS b, 0 AS a FROM orders) SELECT a FROM t",
    "SELECT t.a FROM (SELECT 0 AS a, 0 AS b UNION ALL SELECT 0, 0 UNION ALL SELECT user_id, 0 FROM orders) t",
    "SELECT * FROM (SELECT 0 AS a UNION ALL SELECT user_id FROM orders)",
    "SELECT y FROM (SELECT 0 AS x, 1 AS y UNION ALL SELECT 1, user_id FROM orders) ORDER BY y",
    "SELECT x FROM (SELECT 0 AS x, 1 AS y UNION ALL SELECT user_id, 1 FROM orders)",
])
def test_a_customer_id_in_a_later_union_branch_is_followed_by_position_and_not_by_name(guard, sql):
    """BigQuery names the columns after the first branch and matches the others by position, so the IDs in
    `SELECT user_id AS b, 0 AS a` land in the first branch's column `a`."""
    assert violation_rule(guard, sql) == "PER_CUSTOMER_ROWS"


@pytest.mark.parametrize("sql", [
    "SELECT b FROM (SELECT 0 AS a, 0 AS b UNION ALL SELECT user_id AS b, 0 AS a FROM orders)",  # position 1 holds the zeros
    "SELECT x FROM (SELECT 0 AS x, 1 AS y UNION ALL SELECT 1, user_id FROM orders) ORDER BY x",  # the IDs are in y
    "SELECT y FROM (SELECT 0 AS x, 1 AS y UNION ALL SELECT user_id, 1 FROM orders)",  # the IDs are in x
])
def test_a_union_with_no_id_in_the_position_that_is_read_is_fine(guard, sql):
    gq = accepted(guard, sql, ALICE)
    assert not gq.per_customer and gq.id_columns == ()


# 2. recursion -------------------------------------------------------------------------------------------------------------

def test_recursion_is_not_supported_and_the_error_says_how_to_write_it_without(guard):
    sql = "WITH RECURSIVE r AS (SELECT user_id AS a, 0 AS b FROM orders UNION ALL SELECT b, a FROM r WHERE b = 0) SELECT b FROM r"
    err = refused(guard, sql)
    assert err.kind == ErrorKind.SQL_SYNTAX and err.repairable and "RECURSIVE" in err.detail
    plain = "WITH RECURSIVE r AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM r WHERE n < 3) SELECT n FROM r"
    assert refused(guard, plain).kind == ErrorKind.SQL_SYNTAX


def test_lineage_does_not_keep_a_result_it_worked_out_inside_a_cycle():
    from src.guards.lineage import _Lineage

    tree = sqlglot.parse_one("SELECT n FROM (SELECT 1 AS n) AS q", read="bigquery")
    lineage = _Lineage(tree, make_allowlist())
    scope = lineage.root_scope
    key = ("column", id(scope), "n", True)
    lineage._busy.add(key)  # as if the walk were already inside this query, which a self-referencing CTE would cause
    assert lineage._output(scope, "n", True) == frozenset() and lineage._cycles == 1
    assert key not in lineage._memo  # an answer given while a cycle was open may be partial: it was not kept
    lineage._busy.discard(key)


# 3. a customer-ID output has a real name ----------------------------------------------------------------------------------

@pytest.mark.parametrize("projection", ["CAST(user_id AS STRING)", "CONCAT('c', CAST(user_id AS STRING))", "user_id + 0", "FORMAT('%d', user_id)"])
def test_an_unnamed_customer_id_output_gets_a_real_name_for_the_result_guard(guard, projection):
    """BigQuery would call it f0_, and the Result Guard replaces only the columns named in `id_columns`."""
    sql = f"SELECT {projection}, SUM(sale_price) AS spend FROM order_items GROUP BY user_id ORDER BY spend DESC LIMIT 10"
    gq = accepted(guard, sql, ALICE)
    assert gq.per_customer and gq.id_columns == ("customer_id",)
    assert "AS customer_id" in gq.sql and "_col_" not in gq.sql


def test_a_bare_column_and_an_alias_keep_their_names_and_a_taken_name_is_not_reused(guard):
    assert accepted(guard, RANKED, ALICE).id_columns == ("user_id",)
    gq = accepted(guard, "SELECT CAST(user_id AS STRING), SUM(sale_price) AS customer_id FROM order_items GROUP BY user_id ORDER BY 2 DESC LIMIT 5", ALICE)
    assert gq.id_columns == ("customer_id_",) and "AS customer_id_" in gq.sql


def test_every_name_in_id_columns_is_a_column_the_query_really_outputs(guard):
    for sql in (
        "SELECT user_id, SUM(sale_price) AS s FROM order_items GROUP BY user_id ORDER BY s DESC",
        "SELECT oi.user_id AS who, SUM(oi.sale_price) AS s FROM order_items oi GROUP BY oi.user_id ORDER BY s DESC",
        "SELECT CAST(user_id AS STRING), SUM(sale_price) AS s FROM order_items GROUP BY user_id ORDER BY s DESC",
    ):
        gq = accepted(guard, sql, CEO)
        outputs = {p.alias_or_name for p in parse_output(gq.sql).expressions}
        assert set(gq.id_columns) <= outputs, (gq.id_columns, outputs)


# 4. the customer count must not change which rows a set operation keeps -------------------------------------------------

@pytest.mark.parametrize("operator", ["EXCEPT DISTINCT", "INTERSECT DISTINCT", "UNION DISTINCT"])
def test_the_customer_count_is_not_added_where_it_would_change_which_rows_match(guard, operator):
    """These compare whole rows, so another column per branch would change the answer. No count means the Result Guard
    withholds the result, instead of showing a wrong one."""
    gq = accepted(guard, f"SELECT state FROM users GROUP BY state {operator} SELECT state FROM users WHERE age > 50 GROUP BY state", ALICE)
    assert gq.demographic and gq.k_column is None and "__k_users" not in gq.sql


def test_union_all_still_gets_the_count_in_every_branch(guard):
    gq = accepted(guard, "SELECT state FROM users GROUP BY state UNION ALL SELECT state FROM users WHERE age > 50 GROUP BY state", ALICE)
    assert gq.k_column == "__k_users" and gq.sql.count("AS __k_users") == 2


# 5. string escapes --------------------------------------------------------------------------------------------------------

BACKSLASH = "\\"
ILLEGAL = [BACKSLASH + "d+", BACKSLASH + ".", BACKSLASH + "s+", BACKSLASH + "w+@" + BACKSLASH + "w+", "%" + BACKSLASH + "_%", BACKSLASH + "(", BACKSLASH + "-"]
LEGAL = [
    "r'" + BACKSLASH + "d+'", "R'" + BACKSLASH + "d+'", 'r"' + BACKSLASH + 'd+"', "'" + 2 * BACKSLASH + "d+'", "'a" + BACKSLASH + "nb'",
    "'tab" + BACKSLASH + "there'", "'" + BACKSLASH + "x41'", "'" + BACKSLASH + "u00e9'", "'" + BACKSLASH + "101'", "'it" + BACKSLASH + "'s'",
    '"say ' + BACKSLASH + '"hi' + BACKSLASH + '""', "'back" + 2 * BACKSLASH + "slash'", "'q" + BACKSLASH + "?'",
    "'" + BACKSLASH + "a" + BACKSLASH + "b" + BACKSLASH + "f" + BACKSLASH + "r" + BACKSLASH + "v'", "'no backslash at all'",
    "b'" + BACKSLASH + "x41'", "'''triple " + BACKSLASH + "n quoted'''",
]


@pytest.mark.parametrize("body", ILLEGAL)
@pytest.mark.parametrize("quote", ["'", '"', "'''"])
def test_an_illegal_escape_is_refused_and_not_silently_rewritten(guard, body, quote):
    """BigQuery says "illegal escape sequence". sqlglot would read it as the plain character and write that back, so the
    query would run and match something else. Here it is refused, so the SQL loop can repair it."""
    err = refused(guard, f"SELECT COUNT(*) AS n FROM products WHERE REGEXP_CONTAINS(name, {quote}{body}{quote})")
    assert err.kind == ErrorKind.SQL_SYNTAX and err.repairable and "backslash" in err.detail
    assert body not in err.detail  # the literal itself is not repeated


@pytest.mark.parametrize("literal", LEGAL)
def test_legal_escapes_and_raw_strings_pass(guard, literal):
    accepted(guard, f"SELECT COUNT(*) AS n FROM products WHERE REGEXP_CONTAINS(name, {literal})", ALICE)


def test_a_backslash_in_a_comment_is_not_an_escape(guard):
    sql = "SELECT COUNT(*) AS n FROM products /* a " + BACKSLASH + "d in a comment */ WHERE name = 'x' -- and " + BACKSLASH + "s here"
    accepted(guard, sql, ALICE)


def test_a_legal_escape_means_what_bigquery_says_it_means(guard):
    gq = accepted(guard, "SELECT COUNT(*) AS n FROM products WHERE name = 'a" + BACKSLASH + "tb' OR name = '" + BACKSLASH + "x41'", ALICE)
    assert "'a" + BACKSLASH + "tb'" in gq.sql and "'A'" in gq.sql


# 6. GROUP BY stays as written -------------------------------------------------------------------------------------------

def test_group_by_is_not_swapped_for_an_alias_that_shares_a_columns_name(guard):
    """sqlglot swaps a GROUP BY expression for its SELECT alias when there is an ORDER BY. If the alias is also a column
    of the table, the GROUP BY then names that column and groups by every raw timestamp."""
    sql = "SELECT DATE_TRUNC(created_at, MONTH) AS created_at, COUNT(*) AS n FROM orders GROUP BY DATE_TRUNC(created_at, MONTH) ORDER BY created_at"
    assert "GROUP BY DATE_TRUNC(created_at, MONTH) ORDER BY created_at" in accepted(guard, sql, CEO).sql


@pytest.mark.parametrize("sql,tail", [
    ("SELECT DATE_TRUNC(created_at, MONTH) AS month, COUNT(*) AS n FROM orders GROUP BY DATE_TRUNC(created_at, MONTH) ORDER BY month",
     "GROUP BY DATE_TRUNC(created_at, MONTH) ORDER BY month"),
    ("SELECT status AS s, COUNT(*) AS n FROM orders GROUP BY status ORDER BY n DESC", "GROUP BY status ORDER BY n DESC"),
    ("SELECT status AS s, COUNT(*) AS n FROM orders GROUP BY 1 ORDER BY 2 DESC", "GROUP BY 1 ORDER BY 2 DESC"),
    ("SELECT status AS s, COUNT(*) AS n FROM orders GROUP BY s ORDER BY n DESC", "GROUP BY s ORDER BY n DESC"),
])
def test_group_by_is_written_back_as_the_model_wrote_it(guard, sql, tail):
    assert tail in accepted(guard, sql, CEO).sql


def test_the_fingerprint_tells_apart_queries_that_group_differently(guard):
    by_month = "SELECT DATE_TRUNC(created_at, MONTH) AS created_at, COUNT(*) AS n FROM orders GROUP BY DATE_TRUNC(created_at, MONTH) ORDER BY created_at"
    by_raw = by_month.replace("GROUP BY DATE_TRUNC(created_at, MONTH)", "GROUP BY created_at")
    assert accepted(guard, by_month, CEO).fingerprint != accepted(guard, by_raw, CEO).fingerprint


# 7. a column that names its table by its path ------------------------------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "SELECT thelook_ecommerce.orders.order_id FROM thelook_ecommerce.orders",
    "SELECT `bigquery-public-data.thelook_ecommerce.orders`.order_id FROM `bigquery-public-data.thelook_ecommerce.orders`",
    "SELECT `bigquery-public-data`.thelook_ecommerce.orders.order_id FROM `bigquery-public-data`.thelook_ecommerce.orders",
    "SELECT thelook_ecommerce.products.* FROM thelook_ecommerce.products",
])
def test_a_column_written_with_its_tables_path_is_rewritten_to_the_alias_the_view_has(guard, sql):
    gq = accepted(guard, sql, ALICE)
    top = [c for c in parse_output(gq.sql).find_all(exp.Column) if c.find_ancestor(exp.Subquery) is None]
    assert top and all(c.args.get("db") is None and c.args.get("catalog") is None for c in top), gq.sql
    assert gq.sql.count("UNNEST(@brands)") == 1


@pytest.mark.parametrize("sql", [
    "SELECT thelook_ecommerce.orders.user_id FROM thelook_ecommerce.orders",
    "SELECT `bigquery-public-data.thelook_ecommerce.orders`.user_id FROM `bigquery-public-data.thelook_ecommerce.orders` LIMIT 5",
    "SELECT thelook_ecommerce.users.age FROM thelook_ecommerce.users",
])
def test_the_lineage_still_sees_customer_data_behind_a_path_qualified_column(guard, sql):
    assert violation_rule(guard, sql) in ("PER_CUSTOMER_ROWS", "DEMOGRAPHIC_ROW_LEVEL")


# 8. the sqlglot version ----------------------------------------------------------------------------------------------------

def test_a_sqlglot_older_than_the_guard_needs_is_refused_at_startup_not_discovered_in_results(monkeypatch):
    from src.errors import ConfigError
    from src.guards import sql_guard

    class OldSqlglot:
        __version__ = "27.0.0"

        @staticmethod
        def parse_one(*args, **kwargs):
            return exp.Select(expressions=[exp.Literal.number(1)], **{"from": exp.From(this=exp.to_table("a"))})  # `from`, not `from_`

    monkeypatch.setattr(sql_guard, "sqlglot", OldSqlglot)
    with pytest.raises(ConfigError) as err:
        make_guard()
    assert "27.0.0" in err.value.message and "requirements.txt" in err.value.fix


# 9. comments cannot write metadata ---------------------------------------------------------------------------------------

def test_a_comment_cannot_rename_a_function_in_the_generated_text(guard):
    sql = "SELECT COUNT(*) AS n /* sqlglot.meta bigquery_name=EXTERNAL_QUERY */ FROM orders"
    assert "EXTERNAL_QUERY" in sqlglot.parse_one(sql, read="bigquery").sql(dialect="bigquery", normalize_functions="upper")  # what a bare generator does
    gq = accepted(guard, sql, ALICE)
    assert "EXTERNAL_QUERY" not in gq.sql and gq.sql.startswith("SELECT COUNT(*) AS n FROM (")


def test_a_comment_cannot_forge_a_view(guard):
    gq = accepted(guard, "SELECT COUNT(*) AS n FROM orders /* sqlglot.meta scope_view */", ALICE)
    assert gq.sql.count("UNNEST(@brands)") == 1


# 10. error details hold no text the user could have typed -------------------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "SELECT * FROM 'jane.doe@example.com'", "SELECT * FROM `jane.doe@example.com`", "SELECT * FROM `other.jane.doe@example.com`",
    "SELECT * FROM `jane.doe@example.com.orders_*`",
])
def test_a_value_typed_where_a_table_goes_is_not_copied_into_the_error(guard, sql):
    err = refused(guard, sql)
    assert "jane" not in err.detail and "example" not in err.detail and "@" not in err.detail


def test_an_unknown_column_that_is_not_a_plain_name_is_not_copied_either(guard):
    err = refused(guard, "SELECT `jane@example.org` FROM orders")
    assert err.kind == ErrorKind.SQL_UNKNOWN_COLUMN and "jane" not in err.detail and "@" not in err.detail
    assert "revenue" in refused(guard, "SELECT revenue FROM orders").detail  # a plain name still reaches the repair step


def test_no_violation_detail_repeats_text_from_the_query(guard):
    canary = "canary9f3a"
    for sql in (
        f"SELECT * FROM {canary}.orders", f"SELECT * FROM `{canary}.thelook_ecommerce.orders`", f"SELECT {canary}.x()",
        f"SELECT * FROM {canary}", f"SELECT '{canary}' FROM payments",
        f"SELECT `{canary}`.email FROM users", f"SELECT * FROM {canary}.INFORMATION_SCHEMA.TABLES",
    ):
        err = refused(guard, sql)
        assert canary not in err.detail and canary not in err.user_message, (sql, err.detail)
