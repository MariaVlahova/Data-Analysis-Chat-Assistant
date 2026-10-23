"""The SQL Guard (plan 05): the test matrix from the plan, one group per heading.

Each case checks the exact error kind and rule ID, or the shape of the rewritten query. Plan 09 reuses and extends it.
"""
import re

import pytest
import sqlglot
from sqlglot import exp

from src.config import BigQueryConfig, Settings
from src.errors import AgentError, ErrorKind
from src.guards.query import ArrayParam
from src.guards.refusals import REFUSALS, refusal_text
from src.guards.scope_views import base_table_refs
from src.guards.sql_guard import SQLGuard, generate_sql
from tests.guard_support import (
    ALICE, BLOCKED, BOB, BRAND_FILTER, CEO, DATASET, NOBODY, REPO_ROOT, accepted, make_allowlist, make_guard,
    make_settings, refused, violation_rule,
)


@pytest.fixture(scope="module")
def guard() -> SQLGuard:
    return make_guard()


def parse_output(sql: str) -> exp.Expression:
    (tree,) = sqlglot.parse(sql, read="bigquery")
    return tree


def brand_filters(sql: str) -> int:
    return sql.count(BRAND_FILTER)


def mentions_blocked_name(sql: str) -> list[str]:
    names = {part.lower() for i in parse_output(sql).find_all(exp.Identifier, exp.Var) for part in i.name.split(".")}
    return sorted(names & set(BLOCKED))


# valid queries ----------------------------------------------------------------------------------------------------
# (SQL, number of base-table references in it). Each reference becomes one scope view with one brand filter.

VALID = {
    "one-table aggregate": ("SELECT status, COUNT(*) AS n FROM orders GROUP BY status", 1),
    "three-table join": (
        "SELECT p.brand, o.status, SUM(oi.sale_price) AS revenue FROM order_items oi "
        "JOIN orders o ON o.order_id = oi.order_id JOIN products p ON p.id = oi.product_id GROUP BY p.brand, o.status", 3),
    "cte": (
        "WITH monthly AS (SELECT DATE_TRUNC(created_at, MONTH) AS month, SUM(sale_price) AS revenue FROM order_items GROUP BY month) "
        "SELECT * FROM monthly ORDER BY month", 1),
    "scalar subquery": ("SELECT (SELECT COUNT(*) FROM orders) AS orders_total, (SELECT COUNT(*) FROM products) AS products_total", 2),
    "union all": ("SELECT brand AS label FROM products UNION ALL SELECT category AS label FROM products", 2),
    "window function": ("SELECT brand, retail_price, AVG(retail_price) OVER (PARTITION BY brand) AS brand_avg FROM products", 1),
    "backticked fully qualified name": ("SELECT COUNT(*) AS n FROM `bigquery-public-data.thelook_ecommerce.orders`", 1),
    "unquoted dashed project": ("SELECT COUNT(*) AS n FROM bigquery-public-data.thelook_ecommerce.orders", 1),
    "dataset-qualified name": ("SELECT COUNT(*) AS n FROM thelook_ecommerce.orders", 1),
    "backticked parts": ("SELECT COUNT(*) AS n FROM `bigquery-public-data`.`thelook_ecommerce`.`orders` AS o", 1),
    "comments inside the SQL": ("SELECT /* total */ COUNT(*) AS n -- how many\nFROM orders", 1),
    "date filter": ("SELECT DATE(created_at) AS day, SUM(sale_price) AS s FROM order_items WHERE created_at >= TIMESTAMP '2026-01-01' GROUP BY day", 1),
    "IN subquery": ("SELECT COUNT(*) AS n FROM orders WHERE order_id IN (SELECT order_id FROM order_items WHERE status = 'Returned')", 2),
    "correlated EXISTS": ("SELECT COUNT(*) AS n FROM orders o WHERE EXISTS (SELECT 1 FROM order_items oi WHERE oi.order_id = o.order_id)", 2),
    "self join": ("SELECT COUNT(*) AS n FROM orders o1 JOIN orders o2 ON o1.order_id = o2.order_id", 2),
    "a CTE called like a table": ("WITH orders AS (SELECT 1 AS order_id) SELECT COUNT(*) AS n FROM orders", 0),
    "a CTE that reads the table it is called after": ("WITH orders AS (SELECT order_id FROM orders) SELECT COUNT(*) AS n FROM orders", 1),
    "no table at all": ("SELECT 1 AS one", 0),
    "trailing semicolon": ("SELECT COUNT(*) AS n FROM orders;", 1),
    "parenthesised query": ("(SELECT COUNT(*) AS n FROM orders)", 1),
    "safe prefix and builtin namespaces": ("SELECT SAFE.DIVIDE(SUM(sale_price), COUNT(*)) AS avg_price, NET.HOST('https://a.test/x') AS h FROM order_items", 1),
    "string literals that look like column names": ("SELECT COUNT(*) AS n FROM orders WHERE status = 'email' OR status = 'first_name'", 1),
    "approximate counts of customers": ("SELECT APPROX_COUNT_DISTINCT(user_id) AS buyers, COUNT(DISTINCT user_id) AS exact_buyers FROM order_items", 1),
}


@pytest.mark.parametrize("name", VALID)
def test_a_valid_query_runs_for_a_brand_manager_with_one_brand_filter_per_table_reference(guard, name):
    sql, references = VALID[name]
    gq = accepted(guard, sql, ALICE)
    assert brand_filters(gq.sql) == references
    assert bool(gq.params) == bool(references)
    assert len(base_table_refs(parse_output(gq.sql))) >= references
    assert mentions_blocked_name(gq.sql) == []


@pytest.mark.parametrize("name", VALID)
def test_the_same_query_runs_for_the_ceo_with_no_brand_filter_and_no_parameters(guard, name):
    gq = accepted(guard, VALID[name][0], CEO)
    assert brand_filters(gq.sql) == 0 and gq.params == () and gq.scope_label == "all brands"


@pytest.mark.parametrize("name", VALID)
def test_the_generated_sql_is_stable_and_is_one_select(guard, name):
    """Parsing the generated text and generating it again gives the same text: no round-trip drift for BigQuery."""
    text = accepted(guard, VALID[name][0], ALICE).sql
    tree = parse_output(text)
    assert isinstance(tree, (exp.Select, exp.SetOperation, exp.Subquery))
    assert generate_sql(tree) == text


def test_the_executed_text_is_generated_from_the_tree_not_copied_from_the_input(guard):
    gq = accepted(guard, "select   count(*)   as n -- sneaky\n from   orders /* more */", ALICE)
    assert "sneaky" not in gq.sql and "more" not in gq.sql and gq.sql.startswith("SELECT COUNT(*) AS n FROM (")


# structure --------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("sql,rule", [
    ("DROP TABLE orders", "NOT_SELECT"),
    ("DELETE FROM orders WHERE TRUE", "NOT_SELECT"),
    ("INSERT INTO orders (order_id) VALUES (1)", "NOT_SELECT"),
    ("UPDATE orders SET status = 'x' WHERE TRUE", "NOT_SELECT"),
    ("MERGE orders t USING orders s ON t.order_id = s.order_id WHEN MATCHED THEN DELETE", "NOT_SELECT"),
    ("CREATE TABLE copy_of_orders AS SELECT * FROM orders", "NOT_SELECT"),
    ("CREATE OR REPLACE VIEW v AS SELECT 1", "NOT_SELECT"),
    ("ALTER TABLE orders ADD COLUMN x INT64", "NOT_SELECT"),
    ("TRUNCATE TABLE orders", "NOT_SELECT"),
    ("EXECUTE IMMEDIATE 'SELECT 1'", "NOT_SELECT"),
    ("WITH a AS (SELECT 1) DELETE FROM orders WHERE TRUE", "NOT_SELECT"),
    ("SELECT 1; DROP TABLE orders", "MULTI_STATEMENT"),
    ("SELECT 1; SELECT 2", "MULTI_STATEMENT"),
    ("CREATE TEMP FUNCTION f(x INT64) AS (x + 1); SELECT f(1)", "MULTI_STATEMENT"),
])
def test_only_one_select_runs(guard, sql, rule):
    assert violation_rule(guard, sql) == rule


@pytest.mark.parametrize("sql", ["", "   \n\t ", "-- just a comment", "/* nothing */", ";"])
def test_an_empty_or_comment_only_query_is_the_models_syntax_mistake_and_repairable(guard, sql):
    err = refused(guard, sql)
    assert err.kind == ErrorKind.SQL_SYNTAX and err.repairable


@pytest.mark.parametrize("sql", [None, 42, b"SELECT 1"])
def test_something_that_is_not_text_is_a_syntax_error_not_a_crash(guard, sql):
    assert refused(guard, sql).kind == ErrorKind.SQL_SYNTAX


# functions and system variables ---------------------------------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "SELECT * FROM EXTERNAL_QUERY('connection', 'SELECT * FROM secrets')",
    "SELECT external_query('connection', 'select 1')",
    "SELECT `EXTERNAL_QUERY`('connection', 'select 1')",
    "SELECT ML.PREDICT(MODEL m, TABLE orders)",
    "SELECT * FROM ML.PREDICT(MODEL m, (SELECT * FROM orders))",
    "SELECT ml.generate_text(MODEL m, TABLE orders)",
    "SELECT `ML`.`PREDICT`(MODEL m, TABLE orders)",
    "SELECT AI.GENERATE('write me a poem')",
    "SELECT SESSION_USER()",
    "SELECT @@project_id",
    "SELECT * FROM VECTOR_SEARCH(TABLE orders, 'c', TABLE orders)",
    "SELECT bqutil.fn.cw_array_sum([1, 2])",
    "SELECT my_dataset.my_function(1)",
    "SELECT * FROM my_dataset.table_function(1)",
])
def test_denied_functions_table_functions_and_system_variables_are_refused(guard, sql):
    assert violation_rule(guard, sql) == "DISALLOWED_FUNCTION"


def test_a_function_that_cannot_be_parsed_is_a_repairable_syntax_error(guard):
    assert refused(guard, "SELECT * FROM CHANGES(TABLE orders, NULL, NULL)").kind == ErrorKind.SQL_SYNTAX


def test_config_adds_to_the_deny_list_and_names_are_matched_without_regard_to_case():
    custom = make_guard(denied_functions=["FARM_FINGERPRINT"])
    assert violation_rule(custom, "SELECT farm_fingerprint('x')") == "DISALLOWED_FUNCTION"
    accepted(make_guard(), "SELECT FARM_FINGERPRINT('x') AS h", ALICE)  # not on the built-in list: allowed (everything else is)


@pytest.mark.parametrize("configured", [[], ["FARM_FINGERPRINT"], ["SOMETHING_ELSE", "NS.*"]])
def test_the_built_in_deny_list_cannot_be_removed_by_config(configured):
    """A typo or an emptied list in config.yaml must not switch a protection off."""
    guard = make_guard(denied_functions=configured)
    for sql in ("SELECT SESSION_USER() AS who", "SELECT * FROM EXTERNAL_QUERY('c', 'select 1')", "SELECT ML.PREDICT(MODEL m, TABLE orders)",
                "SELECT AI.GENERATE('x')"):
        assert violation_rule(guard, sql) == "DISALLOWED_FUNCTION", sql


def test_the_default_config_has_no_extra_names_so_there_is_one_list_to_read():
    from src.config import ALWAYS_DENIED_FUNCTIONS

    assert make_settings().guards.denied_functions == [] and "SESSION_USER" in ALWAYS_DENIED_FUNCTIONS
    assert "denied_functions: []" in (REPO_ROOT / "config.yaml").read_text(encoding="utf-8")


def test_a_namespace_that_is_not_a_builtin_is_refused_even_if_the_deny_list_is_empty():
    """`ML.PREDICT` and a stored UDF look alike. Whatever the deny list says, only builtin namespaces may be called."""
    assert violation_rule(make_guard(denied_functions=[]), "SELECT my_ds.my_function(1)") == "DISALLOWED_FUNCTION"


def test_the_deny_list_is_validated_when_the_config_loads():
    with pytest.raises(ValueError):
        make_settings(denied_functions=["not a name!"])
    with pytest.raises(ValueError):
        make_settings(denied_functions=["ML.*.EXTRA"])


@pytest.mark.parametrize("sql", ["SELECT COUNT(*) FROM orders WHERE status = @status", "SELECT COUNT(*) FROM orders WHERE status = ?"])
def test_a_query_parameter_written_by_the_model_is_a_repairable_mistake(guard, sql):
    err = refused(guard, sql)
    assert err.kind == ErrorKind.SQL_SYNTAX and err.repairable and "parameters" in err.detail


# tables -----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("sql,rule", [
    ("SELECT * FROM other_dataset.orders", "TABLE_NOT_ALLOWED"),
    ("SELECT * FROM `other-project.thelook_ecommerce.orders`", "TABLE_NOT_ALLOWED"),
    ("SELECT * FROM `bigquery-public-data.other_dataset.orders`", "TABLE_NOT_ALLOWED"),
    ("SELECT * FROM thelook_ecommerce.INFORMATION_SCHEMA.TABLES", "TABLE_NOT_ALLOWED"),
    ("SELECT * FROM `bigquery-public-data.thelook_ecommerce.INFORMATION_SCHEMA.COLUMNS`", "TABLE_NOT_ALLOWED"),
    ("SELECT * FROM `region-us`.INFORMATION_SCHEMA.JOBS", "TABLE_NOT_ALLOWED"),
    ("SELECT * FROM payments", "TABLE_NOT_ALLOWED"),
    ("SELECT * FROM `bigquery-public-data.thelook_ecommerce.orders_*`", "WILDCARD_TABLE"),
    ("SELECT * FROM thelook_ecommerce.order*", "WILDCARD_TABLE"),
    ("SELECT * FROM EXTERNAL_QUERY('c', 'select 1')", "DISALLOWED_FUNCTION"),
    ("SELECT * FROM orders FOR SYSTEM_TIME AS OF TIMESTAMP '2020-01-01'", "TABLE_NOT_ALLOWED"),
    ("SELECT * FROM orders TABLESAMPLE SYSTEM (10 PERCENT)", "TABLE_NOT_ALLOWED"),
    ("SELECT * FROM a.b.c.d", "TABLE_NOT_ALLOWED"),
    ("SELECT 1 FROM orders WHERE order_id IN (SELECT id FROM secret.table1)", "TABLE_NOT_ALLOWED"),
    ("SELECT (SELECT COUNT(*) FROM payments)", "TABLE_NOT_ALLOWED"),
])
def test_only_the_four_tables_of_the_configured_dataset_are_readable(guard, sql, rule):
    assert violation_rule(guard, sql) == rule


@pytest.mark.parametrize("sql", ["SELECT COUNT(*) FROM Orders", "SELECT COUNT(*) FROM THELOOK_ECOMMERCE.ORDERS"])
def test_table_names_match_without_regard_to_case_and_the_rewrite_uses_the_real_table(guard, sql):
    gq = accepted(guard, sql, ALICE)
    assert gq.tables == {"orders"} and f"`{DATASET}.orders`" in gq.sql


def test_a_table_the_model_hides_behind_a_cte_name_is_still_checked(guard):
    """The CTE called `payments` is a CTE. The reference inside its own body is the table, and it is refused."""
    assert violation_rule(guard, "WITH payments AS (SELECT * FROM payments) SELECT * FROM payments") == "TABLE_NOT_ALLOWED"
    accepted(guard, "WITH payments AS (SELECT 1 AS x) SELECT * FROM payments", ALICE)


# PII --------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "SELECT email FROM users",
    "SELECT CONCAT(first_name, ' ', last_name) AS full_name FROM users",
    "SELECT e FROM (SELECT email AS e FROM users)",
    "WITH t AS (SELECT email AS e FROM users) SELECT e FROM t",
    "SELECT COUNT(*) FROM users WHERE email LIKE '%@%'",
    "SELECT COUNT(*) FROM users ORDER BY email",
    "SELECT COUNT(*) FROM users GROUP BY email",
    "SELECT COUNT(*) FROM users HAVING MAX(email) > 'a'",
    "SELECT EMAIL FROM users",
    "SELECT `Email` FROM users",
    "SELECT users.`email` FROM users",
    "SELECT `users.email` FROM users",
    "SELECT u.Email FROM users u",
    "SELECT latitude, longitude FROM users",
    "SELECT street_address FROM users",
    "SELECT postal_code, COUNT(*) FROM users GROUP BY postal_code",
    "SELECT ST_ASTEXT(user_geom) FROM users",
    "SELECT * EXCEPT (email) FROM users",
    "SELECT TO_JSON_STRING(STRUCT(email)) FROM users",
    "SELECT COUNT(*) FROM users a JOIN users b ON a.email = b.email",
    "SELECT age AS email FROM users",
    "SELECT ＥＭＡＩＬ FROM users",
])
def test_pii_columns_are_blocked_through_every_clause_alias_quote_and_case(guard, sql):
    assert violation_rule(guard, sql) == "PII_COLUMN"
    assert violation_rule(guard, sql, CEO) == "PII_COLUMN"  # the CEO never sees PII either


def test_the_refusal_names_the_kind_of_request_and_no_column(guard):
    err = refused(guard, "SELECT email, first_name FROM users")
    assert "personal details" in err.user_message
    assert not [c for c in BLOCKED if re.search(rf"{c}", err.user_message.lower())]


# star and whole rows ----------------------------------------------------------------------------------------------

def test_select_star_expands_to_the_allowed_columns_only(guard):
    gq = accepted(guard, "SELECT * FROM products LIMIT 5", ALICE)
    inner = parse_output(gq.sql).find(exp.Subquery).this
    assert [p.alias_or_name for p in inner.expressions] == make_allowlist().allowed("products")
    assert mentions_blocked_name(gq.sql) == []


@pytest.mark.parametrize("sql", [
    "SELECT * FROM users",
    "SELECT u.* FROM users u",
    "SELECT TO_JSON_STRING(u) FROM users u",
    "SELECT TO_JSON_STRING(users) FROM users",
    "SELECT u FROM users u",
    "SELECT TO_JSON_STRING(t) FROM (SELECT id, age FROM users) t",
    "SELECT t.* FROM (SELECT id FROM users) t",
])
def test_a_star_or_a_whole_row_over_users_is_customer_rows_and_refused(guard, sql):
    """The view holds only allowed columns, so a star could never reach PII. But the row still carries `users.id` and
    the demographic columns, one customer per row, which is exactly what rule R1 refuses."""
    assert violation_rule(guard, sql) == "PER_CUSTOMER_ROWS"


def test_a_whole_row_that_holds_no_customer_data_is_fine(guard):
    accepted(guard, "SELECT TO_JSON_STRING(p) AS j FROM products p LIMIT 3", ALICE)
    gq = accepted(guard, "SELECT COUNT(*) AS n FROM users u WHERE TO_JSON_STRING(u) IS NOT NULL", ALICE)
    assert gq.demographic and gq.k_column == "__k_users" and "COUNT(DISTINCT u.id) AS __k_users" in gq.sql


def test_a_star_inside_a_subquery_is_a_demographic_touch(guard):
    gq = accepted(guard, "SELECT COUNT(*) AS n FROM (SELECT * FROM users)", ALICE)
    assert gq.demographic and gq.per_customer is False


# scope: the brand filter ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "three-table join", "cte", "scalar subquery", "union all", "IN subquery", "correlated EXISTS", "self join",
    "a CTE that reads the table it is called after",
])
def test_every_base_table_in_every_query_shape_gets_its_own_view_and_none_keeps_its_own_spelling(guard, name):
    sql, references = VALID[name]
    gq = accepted(guard, sql, ALICE)
    assert brand_filters(gq.sql) == references
    for ref in base_table_refs(parse_output(gq.sql)):
        assert ref.catalog == "bigquery-public-data" and ref.db == "thelook_ecommerce"  # only spelled in full: the views' own


def test_a_brand_the_model_writes_cannot_widen_the_view(guard):
    gq = accepted(guard, "SELECT COUNT(*) AS n FROM products WHERE brand = 'Other Brand'", ALICE)
    tree = parse_output(gq.sql)
    outer_where = tree.args["where"].sql(dialect="bigquery")
    assert "Other Brand" in outer_where and BRAND_FILTER not in outer_where  # the model's filter narrows, on top of the view
    (view,) = [s for s in tree.find_all(exp.Subquery) if BRAND_FILTER in s.sql(dialect="bigquery")]
    assert view.parent.parent is tree  # the view is the FROM source of the model's query


@pytest.mark.parametrize("predicate", ["brand = 'X' OR 1 = 1", "TRUE", "NOT (brand <> brand)", "brand IS NULL OR brand IS NOT NULL"])
def test_no_predicate_the_model_writes_can_remove_the_filter_inside_the_view(guard, predicate):
    gq = accepted(guard, f"SELECT COUNT(*) AS n FROM products WHERE {predicate}", ALICE)
    tree = parse_output(gq.sql)
    assert BRAND_FILTER not in tree.args["where"].sql(dialect="bigquery")
    assert BRAND_FILTER in tree.args["from_"].sql(dialect="bigquery")


def test_the_brand_names_are_a_parameter_and_never_in_the_sql_text(guard):
    gq = accepted(guard, "SELECT COUNT(*) AS n FROM order_items", ALICE)
    assert gq.params == (ArrayParam("brands", "STRING", ("Brand A", "Brand B")),)
    assert "Brand A" not in gq.sql and "Brand B" not in gq.sql and "@brands" in gq.sql


def test_a_hostile_brand_name_is_still_only_a_parameter_value():
    hostile = ("x') OR 1=1; DROP TABLE orders; --", "Levi's")
    user = type(ALICE)("eve", "Eve", "brand_manager", hostile, False, "hash-eve")
    gq = accepted(make_guard(), "SELECT COUNT(*) AS n FROM order_items", user)
    assert gq.params[0].values == hostile
    assert "DROP" not in gq.sql and "OR 1=1" not in gq.sql and "Levi" not in gq.sql


def test_a_brand_manager_with_one_brand_gets_that_brand_only(guard):
    gq = accepted(guard, "SELECT COUNT(*) AS n FROM orders", BOB)
    assert gq.params == (ArrayParam("brands", "STRING", ("Brand C",)),) and gq.scope_label == "your brands: Brand C"


def test_orders_are_scoped_per_item_and_num_of_item_counts_only_the_users_items(guard):
    """Q41: an order that holds an item of the user's brand shows, and its item count covers that brand's items only."""
    gq = accepted(guard, "SELECT SUM(num_of_item) AS items FROM orders", ALICE)
    assert "s.num_of_item AS num_of_item" in gq.sql and "GROUP BY oi.order_id" in gq.sql
    assert "o.num_of_item" not in gq.sql
    assert "o.num_of_item" in accepted(guard, "SELECT SUM(num_of_item) AS items FROM orders", CEO).sql


def test_users_are_scoped_to_customers_who_bought_the_users_brands(guard):
    gq = accepted(guard, "SELECT COUNT(*) AS n FROM users", ALICE)
    assert "u.id IN (SELECT oi.user_id FROM" in gq.sql and brand_filters(gq.sql) == 1


def test_the_views_use_no_correlated_subquery(guard):
    """BigQuery limits correlated subqueries. The views join and use IN (subquery) instead (impl)."""
    for table in ("orders", "order_items", "products", "users"):
        view = parse_output(accepted(guard, f"SELECT COUNT(*) AS n FROM {table}", ALICE).sql).find(exp.Subquery).this
        assert not any(isinstance(n, exp.Exists) for n in view.walk())
        for subquery in view.find_all(exp.Select):
            outer = {a.alias_or_name for a in view.find_all(exp.Table) if a.find_ancestor(exp.Select) is view}
            used = {c.table for c in subquery.find_all(exp.Column) if subquery is not view}
            assert not (used & outer - {a.alias_or_name for a in subquery.find_all(exp.Table)}), table


# modes ------------------------------------------------------------------------------------------------------------

def test_a_brand_manager_gets_the_brands_parameter_and_a_label_for_the_composer(guard):
    gq = accepted(guard, "SELECT COUNT(*) AS n FROM orders", ALICE)
    assert gq.params and gq.scope_label == "your brands: Brand A, Brand B" and gq.rewrites[0] == "SCOPE_BRANDS"


def test_the_ceo_gets_no_predicate_but_still_only_the_allowed_columns(guard):
    gq = accepted(guard, "SELECT * FROM products LIMIT 3", CEO)
    inner = parse_output(gq.sql).find(exp.Subquery).this
    assert [p.alias_or_name for p in inner.expressions] == make_allowlist().allowed("products")
    assert gq.params == () and brand_filters(gq.sql) == 0 and gq.rewrites[0] == "SCOPE_ALL"


@pytest.mark.parametrize("sql", ["SELECT COUNT(*) FROM orders", "this is not SQL", "DROP TABLE orders", ""])
def test_a_user_without_brands_gets_no_access_for_every_query_and_never_an_unscoped_one(guard, sql):
    err = refused(guard, sql, NOBODY)
    assert err.kind == ErrorKind.NO_ACCESS


def test_all_brands_without_the_ceo_role_is_no_access_either(guard):
    sneaky = type(ALICE)("mallory", "Mallory", "brand_manager", (), True, "hash-m")
    assert refused(guard, "SELECT COUNT(*) FROM orders", sneaky).kind == ErrorKind.NO_ACCESS


# customer rows and demographics (S7) ------------------------------------------------------------------------------

RANKED = "SELECT user_id, SUM(sale_price) AS spend FROM order_items GROUP BY user_id ORDER BY spend DESC LIMIT 10"


def test_a_customer_id_outside_a_ranked_list_is_refused_r1(guard):
    for sql in (
        "SELECT user_id FROM orders LIMIT 100",
        "SELECT order_id, user_id FROM orders",
        "SELECT user_id, sale_price FROM order_items ORDER BY sale_price DESC LIMIT 10",
        "SELECT user_id, SUM(sale_price) AS s FROM order_items GROUP BY user_id",  # no ORDER BY
        "SELECT user_id, SUM(sale_price) AS s FROM order_items GROUP BY user_id ORDER BY s DESC LIMIT 10 OFFSET 10",  # paging walks the list
        "SELECT user_id, SUM(sale_price) AS s FROM order_items GROUP BY ROLLUP(user_id) ORDER BY s DESC LIMIT 10",
        "SELECT user_id, sale_price FROM order_items",
        "SELECT MAX(user_id) AS m FROM order_items",
        "SELECT ARRAY_AGG(user_id) AS ids FROM order_items",
        "SELECT STRING_AGG(CAST(user_id AS STRING)) AS ids FROM order_items",
        "SELECT user_id, SUM(sale_price) OVER (PARTITION BY user_id) AS s FROM order_items",
        "SELECT user_id FROM orders UNION ALL SELECT user_id FROM order_items",
        "SELECT t.user_id FROM (SELECT user_id, SUM(sale_price) AS s FROM order_items GROUP BY user_id) t ORDER BY t.s DESC LIMIT 5",
        "SELECT FARM_FINGERPRINT(CAST(user_id AS STRING)) AS h FROM order_items LIMIT 10",
    ):
        assert violation_rule(guard, sql) == "PER_CUSTOMER_ROWS", sql


def test_a_ranked_spend_list_runs_and_is_marked_for_the_result_guard_r2(guard):
    gq = accepted(guard, RANKED, ALICE)
    assert gq.per_customer and gq.id_columns == ("user_id",) and gq.limit == 10 and not gq.demographic and gq.k_column is None


@pytest.mark.parametrize("sql,id_columns", [
    ("SELECT user_id, SUM(sale_price) AS spend FROM order_items GROUP BY 1 ORDER BY 2 DESC", ("user_id",)),
    ("SELECT user_id AS customer, SUM(sale_price) AS spend FROM order_items GROUP BY customer ORDER BY spend DESC", ("customer",)),
    ("SELECT oi.user_id, COUNT(*) AS items FROM order_items oi GROUP BY oi.user_id ORDER BY items DESC", ("user_id",)),
    ("SELECT u.id, SUM(oi.sale_price) AS spend FROM order_items oi JOIN users u ON u.id = oi.user_id GROUP BY u.id ORDER BY spend DESC", ("id",)),
    ("SELECT CONCAT('c', CAST(user_id AS STRING)) AS label, SUM(sale_price) AS spend FROM order_items GROUP BY user_id ORDER BY spend DESC", ("label",)),
    ("SELECT t.cid, SUM(t.s) AS spend FROM (SELECT user_id AS cid, sale_price AS s FROM order_items) t GROUP BY t.cid ORDER BY spend DESC", ("cid",)),
    ("SELECT o.user_id, COUNT(*) AS orders FROM orders o GROUP BY ALL ORDER BY orders DESC", ("user_id",)),
])
def test_ranked_lists_in_the_usual_spellings_pass_and_name_their_id_columns(guard, sql, id_columns):
    gq = accepted(guard, sql, ALICE)
    assert gq.per_customer and gq.id_columns == id_columns and gq.limit == 50


def test_a_ranked_list_is_clamped_to_the_rows_the_model_sees(guard):
    for sql in (RANKED.replace("LIMIT 10", "LIMIT 1000"), RANKED.replace(" LIMIT 10", "")):
        gq = accepted(guard, sql, ALICE)
        assert gq.limit == 50 and gq.sql.endswith("LIMIT 50")
    assert accepted(guard, RANKED, ALICE).sql.endswith("LIMIT 10")  # a smaller limit stays


def test_ranked_customer_lists_can_be_switched_off():
    assert violation_rule(make_guard(allow_ranked_customer_lists=False), RANKED) == "PER_CUSTOMER_ROWS"


def test_a_ranked_list_next_to_a_demographic_column_is_refused_r3(guard):
    for sql in (
        "SELECT oi.user_id, SUM(oi.sale_price) AS s FROM order_items oi JOIN users u ON u.id = oi.user_id WHERE u.state = 'CA' GROUP BY 1 ORDER BY 2 DESC LIMIT 5",
        "SELECT u.id, u.state, SUM(oi.sale_price) AS s FROM order_items oi JOIN users u ON u.id = oi.user_id GROUP BY u.id, u.state ORDER BY s DESC",
        "SELECT o.user_id, COUNT(*) AS n FROM orders o WHERE o.gender = 'F' GROUP BY o.user_id ORDER BY n DESC",
    ):
        assert violation_rule(guard, sql) == "PER_CUSTOMER_ROWS", sql


def test_a_demographic_column_without_aggregation_is_refused_r4(guard):
    for sql in (
        "SELECT age FROM users LIMIT 10",
        "SELECT DISTINCT state, city FROM users",
        "SELECT gender FROM orders LIMIT 5",
        "SELECT age, COUNT(*) OVER () FROM users",
        "SELECT state FROM users UNION ALL SELECT city FROM users",
        "SELECT u.created_at FROM users u",  # the sign-up timestamp is nearly a customer key (code review)
        "SELECT ARRAY_AGG(STRUCT(age, gender, state)) AS everyone FROM users",  # collecting is not aggregating (code review)
    ):
        assert violation_rule(guard, sql) == "DEMOGRAPHIC_ROW_LEVEL", sql


def test_a_demographic_group_gets_a_distinct_customer_count_r5(guard):
    gq = accepted(guard, "SELECT state, COUNT(*) AS n FROM users GROUP BY state", ALICE)
    assert gq.demographic and not gq.per_customer and gq.k_column == "__k_users" and gq.id_columns == ()
    assert "COUNT(DISTINCT users.id) AS __k_users" in gq.sql and "K_USERS_ADDED" in gq.rewrites


def test_the_count_uses_the_customer_id_of_the_table_in_reach(guard):
    gq = accepted(guard, "SELECT u.state, SUM(oi.sale_price) AS s FROM order_items oi JOIN users u ON u.id = oi.user_id GROUP BY u.state", ALICE)
    # Both sources hold customers, so the group size is the smaller of the two counts (code review).
    assert gq.k_column == "__k_users" and re.search(r"LEAST\(COUNT\(DISTINCT (oi\.user_id|u\.id)\), COUNT\(DISTINCT (oi\.user_id|u\.id)\)\) AS __k_users", gq.sql)
    gq = accepted(guard, "SELECT o.gender, COUNT(*) AS n FROM orders o GROUP BY o.gender", ALICE)
    assert "COUNT(DISTINCT o.user_id) AS __k_users" in gq.sql


def test_a_global_aggregate_over_a_demographic_filter_gets_the_count_too(guard):
    gq = accepted(guard, "SELECT AVG(sale_price) AS avg_price FROM order_items oi JOIN users u ON u.id = oi.user_id WHERE u.age > 60", ALICE)
    assert gq.demographic and gq.k_column == "__k_users"


def test_a_demographic_grouping_hidden_in_a_cte_has_no_count_and_the_result_is_withheld_later(guard):
    gq = accepted(guard, "WITH t AS (SELECT state, COUNT(*) AS c FROM users GROUP BY state) SELECT state, SUM(c) AS n FROM t GROUP BY state", ALICE)
    assert gq.demographic and gq.k_column is None and "__k_users" not in gq.sql.split("FROM (SELECT u.id")[0].split(" t ")[-1]
    assert "K_USERS_ADDED" not in gq.rewrites


def test_a_cte_that_keeps_the_customer_id_can_still_be_counted(guard):
    gq = accepted(guard, "WITH t AS (SELECT id, state FROM users) SELECT state, COUNT(*) AS n FROM t GROUP BY state", ALICE)
    assert gq.demographic and gq.k_column == "__k_users" and "COUNT(DISTINCT t.id) AS __k_users" in gq.sql


def test_every_branch_of_a_union_gets_the_count_or_none_does(guard):
    both = accepted(guard, "SELECT state AS g, COUNT(*) AS n FROM users GROUP BY state UNION ALL SELECT country, COUNT(*) FROM users GROUP BY country", ALICE)
    assert both.k_column == "__k_users" and both.sql.count("AS __k_users") == 2
    one = accepted(
        guard,
        "SELECT state AS g, COUNT(*) AS n FROM users GROUP BY state UNION ALL "
        "SELECT x, SUM(c) FROM (SELECT country AS x, COUNT(*) AS c FROM users GROUP BY country) GROUP BY x", ALICE)
    assert one.demographic and one.k_column is None and "__k_users" not in one.sql


def test_the_count_alias_never_collides_with_a_name_the_model_used(guard):
    gq = accepted(guard, "SELECT state, COUNT(*) AS __k_users FROM users GROUP BY state", ALICE)
    assert gq.k_column == "__k_users_" and "AS __k_users_" in gq.sql


def test_counting_customers_is_not_returning_them(guard):
    for sql in (
        "SELECT COUNT(DISTINCT user_id) AS buyers FROM order_items",
        "SELECT COUNT(user_id) AS n FROM orders",
        "SELECT APPROX_COUNT_DISTINCT(user_id) AS buyers FROM order_items",
        "SELECT t.n FROM (SELECT COUNT(DISTINCT id) AS n FROM users) t",
        "SELECT p.brand, COUNT(DISTINCT oi.user_id) AS buyers FROM order_items oi JOIN products p ON p.id = oi.product_id GROUP BY p.brand",
    ):
        gq = accepted(guard, sql, ALICE)
        assert not gq.per_customer and gq.id_columns == (), sql


def test_product_brand_and_time_groups_are_not_groups_of_people(guard):
    gq = accepted(guard, "SELECT p.brand, DATE_TRUNC(oi.created_at, MONTH) AS m, SUM(oi.sale_price) AS s FROM order_items oi JOIN products p ON p.id = oi.product_id GROUP BY 1, 2", ALICE)
    assert not gq.demographic and not gq.per_customer and gq.k_column is None


# repairable mistakes ----------------------------------------------------------------------------------------------

def test_an_unknown_column_is_the_models_mistake_and_names_the_column(guard):
    err = refused(guard, "SELECT revenue FROM orders")
    assert err.kind == ErrorKind.SQL_UNKNOWN_COLUMN and err.repairable and "revenue" in err.detail and err.rule_id is None


def test_a_real_column_that_is_not_in_the_allow_list_does_not_exist_for_the_agent(guard):
    assert refused(guard, "SELECT loyalty_tier FROM users").kind == ErrorKind.SQL_UNKNOWN_COLUMN
    assert refused(guard, "SELECT o.coupon FROM orders o").kind == ErrorKind.SQL_UNKNOWN_COLUMN


def test_an_ambiguous_column_asks_for_the_table_alias(guard):
    err = refused(guard, "SELECT status FROM orders o JOIN order_items oi ON o.order_id = oi.order_id")
    assert err.kind == ErrorKind.SQL_UNKNOWN_COLUMN and err.repairable and "table alias" in err.detail


def test_a_syntax_error_gives_the_position_and_never_the_quoted_text(guard):
    err = refused(guard, "SELECT 'secret literal' FROM orders WHERE x = = 2")
    assert err.kind == ErrorKind.SQL_SYNTAX and err.repairable
    assert "line 1" in err.detail and "secret" not in err.detail


@pytest.mark.parametrize("sql", ["SELECT FROM", "SELEC 1", "SELECT (1 FROM orders", "SELECT 'unterminated FROM orders", "SELECT * FROM"])
def test_every_parse_failure_is_a_repairable_syntax_error(guard, sql):
    err = refused(guard, sql)
    assert err.kind == ErrorKind.SQL_SYNTAX and err.repairable and "unterminated" not in err.detail


def test_absurdly_nested_sql_is_a_syntax_error_and_not_a_crash(guard):
    err = refused(guard, "SELECT " + "(" * 3000 + "1" + ")" * 3000)
    assert err.kind in (ErrorKind.SQL_SYNTAX, ErrorKind.INTERNAL)


# LIMIT ------------------------------------------------------------------------------------------------------------

def test_a_missing_limit_is_added(guard):
    gq = accepted(guard, "SELECT brand FROM products", ALICE)
    assert gq.limit == 1000 and gq.sql.endswith(" LIMIT 1000") and "LIMIT_ADDED" in gq.rewrites


def test_a_limit_that_is_too_big_is_clamped_and_a_smaller_one_stays(guard):
    big = accepted(guard, "SELECT brand FROM products LIMIT 1000000", ALICE)
    assert big.limit == 1000 and big.sql.endswith(" LIMIT 1000") and "LIMIT_CLAMPED" in big.rewrites
    small = accepted(guard, "SELECT brand FROM products LIMIT 7", ALICE)
    assert small.limit == 7 and small.sql.endswith(" LIMIT 7") and not [r for r in small.rewrites if r.startswith("LIMIT")]


def test_a_limit_that_is_not_a_plain_number_is_replaced(guard):
    gq = accepted(guard, "SELECT brand FROM products LIMIT 5 + 5", ALICE)
    assert gq.limit == 1000 and gq.sql.endswith(" LIMIT 1000")


def test_an_offset_is_kept(guard):
    gq = accepted(guard, "SELECT brand FROM products ORDER BY brand LIMIT 5000 OFFSET 20", ALICE)
    assert gq.sql.endswith("ORDER BY brand LIMIT 1000 OFFSET 20")


def test_a_union_gets_one_limit_on_the_whole_union_and_inner_limits_stay(guard):
    gq = accepted(guard, "SELECT brand FROM products UNION ALL SELECT category FROM products", ALICE)
    assert gq.sql.count("LIMIT") == 1 and gq.sql.endswith(" LIMIT 1000") and isinstance(parse_output(gq.sql), exp.Union)
    gq = accepted(guard, "SELECT x FROM (SELECT brand AS x FROM products LIMIT 5)", ALICE)
    assert gq.sql.count("LIMIT 5") == 1 and gq.sql.endswith(" LIMIT 1000")


def test_a_with_clause_does_not_hide_the_outer_limit(guard):
    gq = accepted(guard, "WITH a AS (SELECT brand FROM products LIMIT 3) SELECT * FROM a", ALICE)
    assert gq.sql.endswith(" LIMIT 1000") and "LIMIT 3" in gq.sql


def test_the_row_limit_is_the_configured_one():
    settings = Settings(bigquery=BigQueryConfig(sql_row_limit=200))
    small = SQLGuard(settings, make_allowlist())
    gq = accepted(small, "SELECT brand FROM products LIMIT 5000", ALICE)
    assert gq.limit == 200 and gq.sql.endswith(" LIMIT 200")


def test_a_parenthesised_query_gets_its_limit_outside_the_parentheses(guard):
    gq = accepted(guard, "(SELECT brand FROM products)", ALICE)
    assert gq.sql.endswith(") LIMIT 1000")


# determinism ------------------------------------------------------------------------------------------------------

def test_the_same_sql_in_other_spacing_case_or_comments_has_the_same_fingerprint(guard):
    variants = [
        "SELECT status, COUNT(*) AS n FROM orders GROUP BY status",
        "select status,count(*) as n from orders group by status",
        "SELECT   status ,\n  COUNT( * )  AS n\nFROM orders\nGROUP BY status;",
        "SELECT /* c1 */ status, COUNT(*) AS n -- c2\nFROM orders GROUP BY status",
    ]
    assert len({accepted(guard, sql, ALICE).fingerprint for sql in variants}) == 1


def test_a_different_query_has_a_different_fingerprint_and_the_user_does_not_change_it(guard):
    a = accepted(guard, "SELECT COUNT(*) AS n FROM orders", ALICE).fingerprint
    assert a != accepted(guard, "SELECT COUNT(*) AS n FROM order_items", ALICE).fingerprint
    assert a == accepted(guard, "SELECT COUNT(*) AS n FROM orders", CEO).fingerprint  # the original tree, before the rewrite
    assert re.fullmatch(r"[0-9a-f]{64}", a)


# the result for the other plans -----------------------------------------------------------------------------------

def test_the_guarded_query_carries_what_plans_06_and_07_read(guard):
    gq = accepted(guard, "SELECT p.brand, COUNT(*) AS n FROM order_items oi JOIN products p ON p.id = oi.product_id GROUP BY p.brand", ALICE)
    assert gq.tables == {"order_items", "products"} and gq.limit == 1000
    assert gq.rewrites == ("SCOPE_BRANDS", "LIMIT_ADDED") and gq.scope_label == "your brands: Brand A, Brand B"
    assert isinstance(gq.sql, str) and isinstance(gq.params, tuple) and hash(gq) is not None  # frozen and hashable


def test_a_guarded_query_cannot_be_changed(guard):
    gq = accepted(guard, "SELECT 1 AS one", ALICE)
    with pytest.raises(Exception):
        gq.sql = "DROP TABLE orders"  # type: ignore[misc]


# every rule ID has a refusal text ----------------------------------------------------------------------------------

def test_every_rule_id_the_guard_can_raise_has_a_plain_refusal_text():
    source = "".join(p.read_text(encoding="utf-8") for p in (REPO_ROOT / "src" / "guards").glob("*.py"))
    used = set(re.findall(r'violation\(\s*"([A-Z_]+)"', source))
    assert used and used <= set(REFUSALS), used - set(REFUSALS)
    assert "UNGUARDED_QUERY" in REFUSALS  # raised by the query runner
    for rule, text in REFUSALS.items():
        assert text.endswith(".") and "SELECT" not in text and not [c for c in BLOCKED if re.search(rf"{c}", text.lower())], rule
    assert refusal_text("NOT_A_RULE") == refusal_text("ANOTHER_ONE") and refusal_text("NOT_A_RULE")


def test_a_violation_carries_the_rule_and_the_refusal_text(guard):
    err = refused(guard, "DROP TABLE orders")
    assert err.kind == ErrorKind.GUARD_VIOLATION and err.rule_id == "NOT_SELECT" and err.user_message == REFUSALS["NOT_SELECT"]
    assert not err.repairable and not err.retryable  # a guard violation is never retried (3.11)


def test_a_violation_detail_holds_no_sql_text(guard):
    err = refused(guard, "SELECT * FROM payments WHERE secret = 'hunter2-canary'")
    assert "hunter2" not in err.detail and "hunter2" not in err.user_message


# a guard that crashes blocks --------------------------------------------------------------------------------------

def test_a_crash_inside_the_guard_is_an_internal_error_and_runs_nothing(monkeypatch):
    guard = make_guard()

    def boom(*args, **kwargs):
        raise RuntimeError("canary@example.test exploded")

    monkeypatch.setattr(guard, "_analyze", boom)
    err = refused(guard, "SELECT COUNT(*) FROM orders")
    assert err.kind == ErrorKind.INTERNAL and "canary" not in err.detail and "RuntimeError" in err.detail


def test_a_dataset_that_differs_from_the_allow_list_stops_the_guard_at_construction():
    other = Settings(bigquery=BigQueryConfig(dataset="some-project.another_dataset"))
    from src.errors import ConfigError

    with pytest.raises(ConfigError):
        SQLGuard(other, make_allowlist())


def test_the_agent_error_for_a_violation_survives_copy_and_pickle(guard):
    import copy
    import pickle

    err = refused(guard, "SELECT email FROM users")
    for clone in (copy.deepcopy(err), pickle.loads(pickle.dumps(err))):
        assert isinstance(clone, AgentError) and clone.rule_id == "PII_COLUMN" and clone.user_message == err.user_message


# the generated text: one canonical spelling ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["monthly revenue, last 365 days", "orders in the last 30 days", "date spine"])
def test_an_interval_of_one_unit_is_written_the_documented_way_without_quotes(guard, name):
    from tests.guard_support import ANALYTIC_QUERIES

    text = accepted(guard, ANALYTIC_QUERIES[name], ALICE).sql
    assert re.search(r"INTERVAL -?\d+ [A-Z]+", text) and not re.search(r"INTERVAL '-?\d+'", text)


def test_interval_forms_that_are_not_a_plain_integer_are_left_alone(guard):
    text = accepted(guard, "SELECT TIMESTAMP_ADD(created_at, INTERVAL -3 HOUR) AS a, INTERVAL '1-2' YEAR TO MONTH AS b FROM orders", CEO).sql
    assert "INTERVAL -3 HOUR" in text and "INTERVAL '1-2' YEAR TO MONTH" in text


def test_function_names_and_keywords_come_out_in_one_case(guard):
    a = accepted(guard, "select count(*) as n, Sum(sale_price) as s from order_items", ALICE).sql
    b = accepted(guard, "SELECT COUNT(*) AS n, SUM(sale_price) AS s FROM order_items", ALICE).sql
    assert a == b


def test_double_quoted_values_are_strings_and_stay_values(guard):
    text = accepted(guard, 'SELECT COUNT(*) AS n FROM orders WHERE status = "Returned"', ALICE).sql
    assert "status = 'Returned'" in text


def test_the_analytic_corpus_is_accepted_for_every_user_and_generates_stable_text(guard):
    from tests.guard_support import ANALYTIC_QUERIES

    for name, sql in ANALYTIC_QUERIES.items():
        for user in (ALICE, CEO):
            gq = accepted(guard, sql, user)
            assert generate_sql(parse_output(gq.sql)) == gq.sql, name
            assert mentions_blocked_name(gq.sql) == [], name


# regressions: lineage that lost the customer ID (found by testing the guard the way a red team would) ----------------------

@pytest.mark.parametrize("sql", [
    "SELECT x FROM UNNEST((SELECT ARRAY_AGG(user_id) FROM orders)) AS x",
    "SELECT x FROM UNNEST(ARRAY(SELECT user_id FROM order_items)) AS x",
    "SELECT * FROM UNNEST((SELECT ARRAY_AGG(user_id) FROM orders)) AS x",
    "SELECT s.id FROM UNNEST((SELECT ARRAY_AGG(STRUCT(id)) FROM users)) AS s",
    "SELECT TO_JSON_STRING(s) FROM UNNEST((SELECT ARRAY_AGG(STRUCT(id AS i)) FROM users)) AS s",
    "WITH ids AS (SELECT ARRAY_AGG(user_id) AS a FROM orders) SELECT x FROM ids, UNNEST(ids.a) AS x",
    "SELECT x FROM orders o, UNNEST([o.user_id]) AS x",
])
def test_customer_ids_do_not_escape_through_unnest(guard, sql):
    """An UNNEST source is not a table or a query, so lineage used to stop there and lose the ID."""
    assert violation_rule(guard, sql) == "PER_CUSTOMER_ROWS"


@pytest.mark.parametrize("sql,rule", [
    ("WITH users AS (SELECT 1 AS id) SELECT u.id FROM thelook_ecommerce.users u LIMIT 100", "PER_CUSTOMER_ROWS"),
    ("WITH orders AS (SELECT 1 AS user_id) SELECT o.user_id FROM `bigquery-public-data.thelook_ecommerce.orders` o", "PER_CUSTOMER_ROWS"),
    ("WITH users AS (SELECT 1 AS id, 2 AS state) SELECT u.state FROM thelook_ecommerce.users u", "DEMOGRAPHIC_ROW_LEVEL"),
    ("WITH users AS (SELECT 1 AS id) SELECT thelook_ecommerce.users.id FROM thelook_ecommerce.users", "PER_CUSTOMER_ROWS"),
])
def test_a_cte_with_a_tables_name_does_not_hide_the_table_from_the_lineage(guard, sql, rule):
    """The analysis copy used to spell the table without its dataset, so a CTE of the same name took its place and the
    lineage followed the CTE's constant instead of the real `users.id`."""
    assert violation_rule(guard, sql) == rule


def test_a_cte_with_a_tables_name_still_works_when_it_is_the_one_being_read(guard):
    gq = accepted(guard, "WITH users AS (SELECT 1 AS id) SELECT COUNT(*) AS n FROM users", ALICE)
    assert gq.tables == frozenset() and not gq.per_customer and BRAND_FILTER not in gq.sql
    gq = accepted(guard, "WITH orders AS (SELECT order_id FROM orders WHERE status = 'x') SELECT COUNT(*) AS n FROM orders", ALICE)
    assert gq.tables == {"orders"} and brand_filters(gq.sql) == 1


def test_counting_inside_an_unnest_is_still_counting(guard):
    gq = accepted(guard, "SELECT COUNT(*) AS n FROM UNNEST((SELECT ARRAY_AGG(DISTINCT user_id) FROM orders)) AS x", ALICE)
    assert not gq.per_customer and gq.id_columns == ()
