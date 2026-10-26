"""Regressions for the third code review of the SQL Guard (plan 05): one group per finding.

Each was reproduced against the guard before it was fixed. Earlier rounds: `test_sql_guard_review.py`,
`test_sql_guard_review2.py`, `test_guarded_query_seal.py`.
"""
import dataclasses
import re
import time

import pytest
import sqlglot
from sqlglot import exp
from sqlglot.dialects.bigquery import BigQuery

from src.errors import AgentError, ConfigError, ErrorKind
from src.guards import lineage, sql_guard
from src.guards.lineage import COLLECTING, reduces
from src.guards.refusals import NO_RANKED_LISTS_TEXT, REFUSALS
from src.guards.sql_guard import _called_names, _is_builtin_path
from src.observability import Tracer
from src.observability.metrics import read_runs
from tests.guard_support import ALICE, CEO, REPO_ROOT, accepted, make_allowlist, make_guard, refused, violation_rule


@pytest.fixture(scope="module")
def guard():
    return make_guard()


def parse(sql: str) -> exp.Expression:
    return sqlglot.parse_one(sql, read="bigquery")


def k_expression(sql: str) -> exp.Expression:
    """The expression behind `__k_users` in the SQL that runs."""
    (alias,) = [a for a in parse(sql).find_all(exp.Alias) if a.alias == "__k_users"]
    return alias.this


def counted(sql: str) -> list[str]:
    """What `__k_users` counts, as `alias.column` strings."""
    return sorted(c.sql() for c in k_expression(sql).find_all(exp.Column))


# 1. the group size comes from the sources that hold the customers ----------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "SELECT u.state, COUNT(DISTINCT u.id) AS n FROM order_items oi CROSS JOIN users u GROUP BY u.state",
    "SELECT u.state, COUNT(*) AS n FROM order_items oi JOIN users u ON oi.id > 0 GROUP BY u.state",
    "SELECT u.state, COUNT(*) AS n FROM (SELECT user_id FROM order_items) oi, users u GROUP BY u.state",
    "SELECT u.state, COUNT(*) AS n FROM order_items oi JOIN users u ON TRUE WHERE u.age > 30 GROUP BY u.state",
])
def test_the_group_size_counts_the_users_too_not_just_the_first_source_with_an_id(guard, sql):
    """Counting only `order_items.user_id` gave the buyers of the whole join as the size of every state, so a state with
    one customer looked like a group of thousands."""
    gq = accepted(guard, sql, ALICE)
    assert gq.k_column == "__k_users" and isinstance(k_expression(gq.sql), exp.Least)
    assert "u.id" in counted(gq.sql)


def test_every_source_that_holds_customers_is_counted_and_the_smallest_count_wins(guard):
    gq = accepted(guard, "SELECT u.state, SUM(oi.sale_price) AS s FROM users u JOIN order_items oi ON oi.user_id = u.id GROUP BY u.state", ALICE)
    assert counted(gq.sql) == ["oi.user_id", "u.id"] and isinstance(k_expression(gq.sql), exp.Least)


def test_a_single_source_is_a_plain_count(guard):
    gq = accepted(guard, "SELECT o.gender, COUNT(*) AS n FROM orders o GROUP BY o.gender", ALICE)
    assert isinstance(k_expression(gq.sql), exp.Count) and counted(gq.sql) == ["o.user_id"]


@pytest.mark.parametrize("sql", [
    "WITH t AS (SELECT state FROM users) SELECT t.state, COUNT(*) AS n FROM t CROSS JOIN order_items oi GROUP BY t.state",
    "SELECT state, COUNT(*) AS n FROM (SELECT state FROM users) GROUP BY state",
    "SELECT state, COUNT(*) AS n FROM (SELECT state, id FROM users) GROUP BY state",
])
def test_a_source_with_demographic_data_and_no_id_to_count_means_no_count_at_all(guard, sql):
    """The customers behind `t.state` cannot be counted, and counting some other source would overstate the group."""
    gq = accepted(guard, sql, ALICE)
    assert gq.demographic and gq.k_column is None and "__k_users" not in gq.sql


def test_a_demographic_filter_inside_a_subquery_still_counts_the_outer_customers(guard):
    gq = accepted(guard, "SELECT COUNT(*) AS n FROM order_items oi WHERE oi.user_id IN (SELECT id FROM users WHERE age > 60)", ALICE)
    assert gq.demographic and counted(gq.sql) == ["oi.user_id"]


def test_the_required_sources_are_in_the_analysis_and_the_others_are_not_required():
    from sqlglot.optimizer.qualify import qualify

    allowlist = make_allowlist()
    schema = {"thelook_ecommerce": {t: {c: "UNKNOWN" for c in allowlist.allowed(t)} for t in allowlist.table_names()}}
    tree = qualify(
        parse("SELECT u.state, COUNT(*) AS n FROM thelook_ecommerce.users u JOIN thelook_ecommerce.order_items oi ON oi.user_id = u.id GROUP BY u.state"),
        dialect="bigquery", schema=schema, quote_identifiers=False, identify=False,
    )
    (sources,) = lineage.analyze(tree, allowlist).k_sources
    assert sorted(sources) == [("oi", "user_id", False), ("u", "id", True)]


# 2. aggregates that hand individual values back --------------------------------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "SELECT ARRAY_AGG(STRUCT(age, gender, state, city, country, created_at)) AS everyone FROM users",
    "SELECT state, ARRAY_AGG(age) AS ages FROM users GROUP BY state",
    "SELECT STRING_AGG(CONCAT(city, '|', CAST(age AS STRING))) AS rows FROM users",
    "SELECT state, ANY_VALUE(age) AS an_age FROM users GROUP BY state",
    "SELECT state, MAX_BY(age, created_at) AS newest FROM users GROUP BY state",
    "SELECT state, MIN_BY(city, age) AS youngest FROM users GROUP BY state",
    "SELECT APPROX_TOP_COUNT(age, 5) AS common FROM users",
    "SELECT ARRAY_CONCAT_AGG([age]) AS ages FROM users",
    "WITH t AS (SELECT ARRAY_AGG(age) AS a FROM users) SELECT ANY_VALUE(a) AS a FROM t",
    "SELECT state, COUNT(*) AS n, ARRAY_AGG(DISTINCT city) AS cities FROM users GROUP BY state",
])
def test_collecting_customer_attributes_is_not_aggregating_them(guard, sql):
    """ARRAY_AGG, STRING_AGG, ANY_VALUE, MAX_BY and the like return the customers' own values. One row with k of 10 or more
    that holds every customer's age and city is row-level data, so it is R4, not R5."""
    assert violation_rule(guard, sql) == "DEMOGRAPHIC_ROW_LEVEL"


@pytest.mark.parametrize("sql", [
    "SELECT p.brand, ARRAY_AGG(DISTINCT p.category) AS categories FROM products p GROUP BY p.brand",
    "SELECT STRING_AGG(name) AS names FROM products",
    "SELECT status, ANY_VALUE(order_id) AS an_order FROM orders GROUP BY status",
    "SELECT state, COUNT(*) AS n, AVG(age) AS avg_age, MIN(age) AS youngest, APPROX_QUANTILES(age, 4) AS quartiles FROM users GROUP BY state",
])
def test_collecting_other_data_and_reducing_customer_data_still_work(guard, sql):
    accepted(guard, sql, ALICE)


def test_which_aggregates_reduce_and_which_collect():
    def of(sql):
        return parse(f"SELECT {sql} FROM t").expressions[0]

    for reducing in ("SUM(x)", "COUNT(DISTINCT x)", "AVG(x)", "MIN(x)", "MAX(x)", "COUNTIF(x > 1)", "APPROX_QUANTILES(x, 4)", "STDDEV(x)", "LOGICAL_OR(x)"):
        assert reduces(of(reducing)), reducing
    for collecting in ("ARRAY_AGG(x)", "STRING_AGG(x)", "ANY_VALUE(x)", "MAX_BY(x, y)", "ARRAY_CONCAT_AGG(x)", "APPROX_TOP_COUNT(x, 3)"):
        assert not reduces(of(collecting)), collecting
        assert isinstance(of(collecting), COLLECTING), collecting


# 3. lineage must not blow up -----------------------------------------------------------------------------------------------------

def chain_of_whole_rows(depth: int) -> str:
    ctes = ["c0 AS (SELECT order_id AS a, num_of_item AS b FROM orders)"]  # a query must read a table (plan 12, A2)
    ctes += [f"c{i} AS (SELECT TO_JSON_STRING(c{i - 1}) AS a, TO_JSON_STRING(c{i - 1}) AS b FROM c{i - 1})" for i in range(1, depth)]
    return "WITH " + ", ".join(ctes) + f" SELECT a FROM c{depth - 1}"


def test_a_long_chain_of_whole_row_references_is_analysed_in_linear_time(guard):
    """Each level used the one before twice, and every use was worked out again: four times as long every two levels.
    Depth 18 took 12 seconds, and a model-written query of 1.5 KB could take hours."""
    started = time.time()
    for depth in (10, 20, 30, 40):
        accepted(guard, chain_of_whole_rows(depth), ALICE)
    assert time.time() - started < 5


def test_the_work_is_counted_and_a_query_that_needs_too_much_is_refused(monkeypatch):
    monkeypatch.setattr(lineage, "MAX_LINEAGE_STEPS", 40)
    err = refused(make_guard(), chain_of_whole_rows(12))
    assert err.kind == ErrorKind.SQL_SYNTAX and err.repairable and "too deeply nested" in err.detail


def test_a_real_question_needs_a_tiny_fraction_of_the_allowed_work(guard):
    steps = []
    original = lineage._Lineage.__init__

    def counting_init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        steps.append(self)

    lineage._Lineage.__init__ = counting_init
    try:
        accepted(guard, chain_of_whole_rows(40), ALICE)
    finally:
        lineage._Lineage.__init__ = original
    assert steps and steps[0]._steps < lineage.MAX_LINEAGE_STEPS / 10


def test_the_function_check_does_not_write_out_a_subtree_per_call(monkeypatch):
    """It used to render every call with all its arguments, to read a name the tree already has: the cost grew with how deeply
    calls were nested. Now the generator runs for the whole query a fixed number of times."""
    calls = []
    original = sql_guard._to_sql
    monkeypatch.setattr(sql_guard, "_to_sql", lambda tree: calls.append(type(tree).__name__) or original(tree))
    guard = make_guard()
    shallow = len(calls)
    accepted(guard, "SELECT " + "ABS(" * 30 + "1" + ")" * 30 + " AS x FROM orders", ALICE)
    assert len(calls) - shallow <= 3, calls


# 4. grouped inside a CTE or subquery ----------------------------------------------------------------------------------------------

REPAIRABLE_SHAPES = [
    "WITH s AS (SELECT u.country, SUM(oi.sale_price) AS revenue FROM order_items oi JOIN users u ON u.id = oi.user_id GROUP BY u.country) "
    "SELECT country, revenue, revenue / SUM(revenue) OVER () AS share FROM s ORDER BY revenue DESC",
    "SELECT state, n FROM (SELECT state, COUNT(*) AS n FROM users GROUP BY state) ORDER BY n DESC LIMIT 10",
    "WITH a AS (SELECT state, COUNT(*) AS n FROM users GROUP BY state), b AS (SELECT state, n FROM a WHERE n > 5) SELECT state FROM b",
    "SELECT t.state FROM (SELECT state, COUNT(*) AS n FROM users GROUP BY state) t JOIN (SELECT 1 AS one) o ON TRUE",
]


@pytest.mark.parametrize("sql", REPAIRABLE_SHAPES)
def test_a_grouping_step_followed_by_a_plain_select_is_a_repairable_shape_not_a_refusal(guard, sql):
    """The Result Guard needs the group size next to the groups. One SELECT can carry it, so the model is told to write
    one. A violation is never retried, and this is not row-level data."""
    err = refused(guard, sql)
    assert err.kind == ErrorKind.SQL_SYNTAX and err.repairable and err.rule_id is None
    assert "same SELECT" in err.detail and "window" in err.detail


def test_the_rewrite_the_message_asks_for_is_accepted_with_its_count(guard):
    one_select = (
        "SELECT u.country, SUM(oi.sale_price) AS revenue, SUM(oi.sale_price) / SUM(SUM(oi.sale_price)) OVER () AS share "
        "FROM order_items oi JOIN users u ON u.id = oi.user_id GROUP BY u.country ORDER BY revenue DESC"
    )
    gq = accepted(guard, one_select, ALICE)
    assert gq.demographic and gq.k_column == "__k_users"


@pytest.mark.parametrize("sql", [
    "SELECT age FROM users LIMIT 10",  # no grouping anywhere
    "WITH t AS (SELECT state, ARRAY_AGG(age) AS a FROM users GROUP BY state) SELECT * FROM t",  # the group hands the values back
    "WITH s AS (SELECT state, COUNT(*) AS n FROM users GROUP BY state) SELECT s.state, oi.id FROM s JOIN order_items oi ON TRUE",  # reads a table too
    "SELECT state, (SELECT COUNT(*) FROM orders) AS total FROM users",  # a grouped subquery is not a grouped source
])
def test_row_level_demographic_data_is_still_a_refusal(guard, sql):
    assert violation_rule(guard, sql) == "DEMOGRAPHIC_ROW_LEVEL"


def test_the_repairable_shape_does_not_count_as_a_guard_block(tmp_path):
    guard = make_guard()
    tracer = Tracer(tmp_path / "traces", tmp_path / "runs.jsonl")
    with tracer.start_turn("hash0001", "conv-1", "t-1"):
        refused(guard, REPAIRABLE_SHAPES[1])
        refused(guard, "SELECT age FROM users LIMIT 10")
    assert read_runs(tracer.metrics_file)[0][0]["guard_blocks"]["sql"] == 1  # only the row-level one


# 5. the sign-up timestamp ------------------------------------------------------------------------------------------------------------

def test_the_sign_up_timestamp_is_a_quasi_identifier():
    allowlist = make_allowlist()
    assert "created_at" in allowlist.quasi_identifiers("users") and "created_at" in allowlist.allowed("users")
    assert "created_at" not in allowlist.quasi_identifiers("orders")  # an order's timestamp is not a customer's


def test_per_customer_rows_keyed_by_the_sign_up_timestamp_are_refused(guard):
    """It is different for nearly every customer, so it works as a customer key that R1, R2 and the fake IDs never see."""
    assert violation_rule(guard, "SELECT u.created_at FROM users u") == "DEMOGRAPHIC_ROW_LEVEL"
    assert violation_rule(guard, "SELECT u.created_at, u.state FROM users u") == "DEMOGRAPHIC_ROW_LEVEL"


def test_grouping_by_the_sign_up_timestamp_gets_a_group_size_so_one_customer_groups_are_hidden(guard):
    gq = accepted(guard, "SELECT u.created_at, SUM(oi.sale_price) AS spend FROM users u JOIN order_items oi ON oi.user_id = u.id GROUP BY u.created_at", ALICE)
    assert gq.demographic and gq.k_column == "__k_users" and not gq.per_customer


def test_sign_ups_per_month_still_work_and_carry_a_group_size(guard):
    gq = accepted(guard, "SELECT DATE_TRUNC(created_at, MONTH) AS month, COUNT(*) AS signups FROM users GROUP BY month ORDER BY month", ALICE)
    assert gq.demographic and gq.k_column == "__k_users"


def test_order_timestamps_are_not_demographic(guard):
    gq = accepted(guard, "SELECT DATE(created_at) AS day, COUNT(*) AS orders FROM orders GROUP BY day", ALICE)
    assert not gq.demographic and gq.k_column is None


# 6. SAFE. is a prefix, not a namespace ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "SELECT SAFE.ds.fn(1) FROM products",
    "SELECT SAFE.`proj`.ds.fn(1) FROM products",
    "SELECT SAFE.`my-project`.ds.fn(1) FROM products",
    "SELECT SAFE.mydataset.myfunc(name) FROM products",
])
def test_a_stored_function_behind_the_safe_prefix_is_still_a_stored_function(guard, sql):
    assert violation_rule(guard, sql) == "DISALLOWED_FUNCTION"


def test_safe_in_front_of_a_built_in_function_still_works(guard):
    accepted(guard, "SELECT SAFE.DIVIDE(SUM(sale_price), COUNT(*)) AS a, SAFE_CAST(MAX(sale_price) AS INT64) AS b FROM order_items", ALICE)


@pytest.mark.parametrize("path,builtin", [
    (["COUNT"], True), (["NET", "HOST"], True), (["net", "host"], True), (["SAFE", "DIVIDE"], True), (["SAFE", "NET", "HOST"], True),
    (["HLL_COUNT", "INIT"], True), ([], True),
    (["SAFE", "ds", "fn"], False), (["SAFE", "SAFE", "fn"], False), (["ds", "fn"], False), (["proj", "ds", "fn"], False), (["NET", "a", "b"], False),
])
def test_what_a_dotted_call_may_look_like(path, builtin):
    assert _is_builtin_path(path) is builtin


# 7. a parenthesised join ------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("sql,aliases", [
    ("SELECT COUNT(*) AS n FROM (orders o JOIN order_items oi ON oi.order_id = o.order_id)", ["o", "oi"]),
    ("SELECT COUNT(*) AS n FROM (orders o LEFT JOIN order_items oi ON oi.order_id = o.order_id) WHERE oi.id IS NULL", ["o", "oi"]),
    ("SELECT COUNT(*) AS n FROM (orders o JOIN (order_items oi JOIN products p ON p.id = oi.product_id) ON oi.order_id = o.order_id)", ["o", "oi", "p"]),
    ("SELECT p.brand, COUNT(*) AS n FROM (order_items oi JOIN products p ON p.id = oi.product_id) GROUP BY p.brand", ["oi", "p"]),
])
def test_a_parenthesised_join_runs_with_a_view_for_every_table_and_keeps_its_joins(guard, sql, aliases):
    for user, filters in ((ALICE, len(aliases)), (CEO, 0)):
        gq = accepted(guard, sql, user)
        assert gq.sql.count("UNNEST(@brands)") == filters  # one view, so one brand filter, per table the model wrote
        for alias in aliases:  # every table is still there under the name the model gave it
            assert f") AS {alias}" in gq.sql, (alias, gq.sql)
        assert gq.sql.count(" JOIN ") >= len(aliases) - 1  # and the joins the model wrote are still there


def test_the_lineage_sees_through_a_parenthesised_join(guard):
    assert violation_rule(guard, "SELECT oi.user_id FROM (orders o JOIN order_items oi ON oi.order_id = o.order_id) LIMIT 5") == "PER_CUSTOMER_ROWS"
    gq = accepted(guard, "SELECT u.state, COUNT(*) AS n FROM (users u JOIN order_items oi ON oi.user_id = u.id) GROUP BY u.state", ALICE)
    assert gq.demographic and gq.k_column == "__k_users" and counted(gq.sql) == ["oi.user_id", "u.id"]


def test_the_table_options_that_would_be_dropped_are_still_refused(guard):
    for sql in (
        "SELECT COUNT(*) FROM orders TABLESAMPLE SYSTEM (10 PERCENT)",
        "SELECT COUNT(*) FROM orders FOR SYSTEM_TIME AS OF TIMESTAMP '2020-01-01'",
    ):
        assert violation_rule(guard, sql) == "TABLE_NOT_ALLOWED", sql


def test_an_option_on_the_parentheses_of_a_join_stays_where_the_model_put_it(guard):
    """It is on the parenthesised join, not on a table, so the rewrite does not touch it."""
    gq = accepted(guard, "SELECT COUNT(*) AS n FROM (products p JOIN order_items oi ON TRUE) TABLESAMPLE SYSTEM (10 PERCENT)", ALICE)
    assert "TABLESAMPLE SYSTEM (10 PERCENT)" in gq.sql and gq.sql.count("UNNEST(@brands)") == 2


# 8. `authentic` never raises -----------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("change", [
    {"seal": "é" * 64}, {"seal": None}, {"seal": 12345}, {"seal": b"bytes"}, {"seal": ""},
    {"sql": b"SELECT 1"}, {"sql": None}, {"params": ("not an ArrayParam",)}, {"limit": object()}, {"tables": {object()}}, {"rewrites": [object()]},
])
def test_authentic_is_false_for_anything_that_cannot_be_checked_and_never_raises(change):
    gq = make_guard().check("SELECT COUNT(*) AS n FROM orders", ALICE)
    forged = dataclasses.replace(gq, **change)
    assert forged.authentic is False


def test_the_runner_refuses_such_a_query_as_unguarded_and_counts_it(fake_bigquery, tmp_path):
    from src.data_access.safe_runner import SafeQueryRunner
    from tests.fake_bigquery import FakeRunner
    from tests.guard_support import make_settings

    gq = make_guard().check("SELECT COUNT(*) AS n FROM orders", ALICE)
    tracer = Tracer(tmp_path / "traces", tmp_path / "runs.jsonl")
    safe = SafeQueryRunner(make_settings(), FakeRunner(fake_bigquery))
    with tracer.start_turn("hash0001", "conv-1", "t-1"):
        for forged in (dataclasses.replace(gq, seal="é" * 64), dataclasses.replace(gq, sql=b"SELECT 1")):
            with pytest.raises(AgentError) as err:
                safe.run(forged)
            assert err.value.rule_id == "UNGUARDED_QUERY" and err.value.kind == ErrorKind.GUARD_VIOLATION
    assert fake_bigquery.jobs == [] and read_runs(tracer.metrics_file)[0][0]["guard_blocks"]["sql"] == 2


# 9. the refusal text promises only what is on offer ---------------------------------------------------------------------------------------------

RANKED = "SELECT user_id, SUM(sale_price) AS s FROM order_items GROUP BY user_id ORDER BY s DESC LIMIT 5"


def test_with_ranked_lists_switched_off_the_refusal_does_not_offer_one():
    err = refused(make_guard(allow_ranked_customer_lists=False), RANKED)
    assert err.rule_id == "PER_CUSTOMER_ROWS" and err.user_message == NO_RANKED_LISTS_TEXT
    assert "ranked" not in err.user_message.lower() and "top customers" not in err.user_message.lower()


def test_with_ranked_lists_on_the_refusal_of_other_customer_rows_offers_one(guard):
    err = refused(guard, "SELECT user_id FROM orders LIMIT 100")
    assert err.user_message == REFUSALS["PER_CUSTOMER_ROWS"] and "ranked list" in err.user_message


# 10. the deny list is checked on what runs --------------------------------------------------------------------------------------------------------

def test_a_function_the_generator_names_differently_from_the_tree_is_still_denied():
    """sqlglot's node for GENERATE_UUID() is called UUID, so a name-based check on the tree misses a denial of it. The text
    that will run says GENERATE_UUID."""
    configured = make_guard(denied_functions=["GENERATE_UUID"])
    assert violation_rule(configured, "SELECT GENERATE_UUID() AS id FROM products") == "DISALLOWED_FUNCTION"
    accepted(make_guard(), "SELECT GENERATE_UUID() AS id FROM products", ALICE)


@pytest.mark.parametrize("text,names", [
    ("SELECT NET.HOST(u), COUNT(*) FROM t", {"NET.HOST", "COUNT"}),
    ("SELECT SAFE.DIVIDE(a, b) FROM t", {"SAFE.DIVIDE"}),
    ("SELECT `my-project.ds.f`(1)", {"my-project.ds.f"}),
    ("SELECT `my-project`.`ds`.`f`(1)", {"my-project.ds.f"}),
    ("SELECT ML.PREDICT(MODEL m, TABLE t)", {"ML.PREDICT"}),
    ("SELECT ABS(ROUND(x, 2)) FROM a.b WHERE c IN (1, 2)", {"ABS", "ROUND", "IN"}),
    ("SELECT a.b FROM t", set()),
    ("SELECT 'NET.HOST(x)' AS s FROM t", set()),  # inside a string
])
def test_the_names_before_a_parenthesis_are_found_in_one_pass_over_the_tokens(text, names):
    assert _called_names(text) == names


# 11. the messages say what the pin says -------------------------------------------------------------------------------------------------------------

def test_the_version_messages_match_the_pin(monkeypatch):
    class Old:
        __version__ = "27.0.0"

        @staticmethod
        def parse_one(*args, **kwargs):
            return exp.Select(expressions=[exp.Literal.number(1)], **{"from": exp.From(this=exp.to_table("a"))})

    monkeypatch.setattr(sql_guard, "sqlglot", Old)
    with pytest.raises(ConfigError) as err:
        make_guard()
    pin = next(l for l in (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines() if l.startswith("sqlglot"))
    assert "30.24" in err.value.fix and "below 31" in err.value.fix and ">=30.24,<31" in pin.replace(" ", "")
    plan = (REPO_ROOT / "docs" / "plans" / "05-sql-guard" / "plan.md").read_text(encoding="utf-8")
    assert ">=30.0" not in plan and "sqlglot>=30.24,<31" in plan.replace(" ", "")


# 12. the generator is checked by what it does ------------------------------------------------------------------------------------------------------

def test_an_empty_closure_cell_does_not_stop_the_search_for_the_helper_list():
    def outer():
        def inner():
            return later  # noqa: F821 - `later` is assigned below the return, so its cell is empty
        return inner
        later = [lambda: 1]  # noqa: F841

    function = outer()
    with pytest.raises(ValueError):
        function.__closure__[0].cell_contents  # the premise: reading the cell raises
    assert sql_guard._transform_list(function) is None


def test_a_generator_that_still_rewrites_group_by_is_refused_whatever_the_helper_is_called(monkeypatch):
    """The probe checks the behaviour. Here the helper is present twice, once under its real name (removed) and once under
    another (kept), as if a later sqlglot added a second rewrite of the same kind."""
    chain = sql_guard._transform_list(BigQuery.Generator.TRANSFORMS[exp.Select])
    swap = next(item for item in chain if item.__name__ == "_alias_ordered_group")

    def second_swap(expression):
        return swap(expression)

    from sqlglot import transforms

    monkeypatch.setitem(BigQuery.Generator.TRANSFORMS, exp.Select, transforms.preprocess([*chain, second_swap]))
    sql_guard._bigquery_dialect.cache_clear()
    try:
        with pytest.raises(ConfigError) as err:
            make_guard()
        assert "rewrites a GROUP BY" in err.value.message
    finally:
        monkeypatch.undo()
        sql_guard._bigquery_dialect.cache_clear()
    make_guard()


# 13. the span is in the shared table ------------------------------------------------------------------------------------------------------------------

def test_the_guard_span_is_listed_in_plan_02_where_the_other_plans_look_for_span_names(tmp_path):
    table = (REPO_ROOT / "docs" / "plans" / "02-observability" / "plan.md").read_text(encoding="utf-8")
    assert "`guard.sql`" in table
    tracer = Tracer(tmp_path / "traces", tmp_path / "runs.jsonl")
    with tracer.start_turn("hash0001", "conv-1", "t-1") as turn:
        make_guard().check("SELECT COUNT(*) AS n FROM orders", ALICE)
    assert '"guard.sql"' in tracer.trace_path(turn.trace_id).read_text(encoding="utf-8")
