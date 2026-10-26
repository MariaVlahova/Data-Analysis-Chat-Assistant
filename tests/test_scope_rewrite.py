"""The scope rewrite and the guard's end-to-end invariants (plan 05): the views, the post-condition, a fuzz test,
the trace, and the hand-off to `SafeQueryRunner`."""
import json
import logging
import random

import pandas as pd
import pytest
import sqlglot
from sqlglot import exp
from sqlglot.tokens import TokenType

from src.errors import AgentError, ErrorKind
from src.guards.scope_views import Rewritten, ScopeViews, base_table_refs
from src.observability import Tracer
from src.observability.metrics import read_runs
from tests.fake_bigquery import FakeRunner
from tests.guard_support import (
    ALICE, BLOCKED, BRAND_FILTER, CEO, DATASET, accepted, make_allowlist, make_guard, make_settings, refused,
)


def brand_filters(sql: str) -> int:
    return sql.count(BRAND_FILTER)


@pytest.fixture(scope="module")
def views() -> ScopeViews:
    return ScopeViews(make_allowlist(), DATASET)


@pytest.fixture(scope="module")
def guard():
    return make_guard()


def parse(sql: str) -> exp.Expression:
    return sqlglot.parse_one(sql, read="bigquery")


# the view templates -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["brands", "all"])
@pytest.mark.parametrize("table", ["orders", "order_items", "products", "users"])
def test_every_view_projects_the_allowed_columns_and_nothing_else(views, table, mode):
    view = views.view(table, mode)
    assert [p.alias_or_name for p in view.expressions] == make_allowlist().allowed(table)
    assert not [star for star in view.find_all(exp.Star) if not isinstance(star.parent, exp.Count)]  # no `*` except COUNT(*)


@pytest.mark.parametrize("table,brands,all_modes", [("orders", 3, 1), ("order_items", 2, 1), ("products", 1, 1), ("users", 3, 1)])
def test_the_views_read_the_tables_they_need_and_only_the_brand_views_hold_the_filter(views, table, brands, all_modes):
    assert views.reference_count(table, "brands") == brands and views.reference_count(table, "all") == all_modes
    assert views.view(table, "brands").sql(dialect="bigquery").count(BRAND_FILTER) == 1
    assert BRAND_FILTER not in views.view(table, "all").sql(dialect="bigquery") and "@" not in views.view(table, "all").sql(dialect="bigquery")


def test_a_view_names_its_tables_in_full_and_never_a_brand(views):
    for table in ("orders", "order_items", "products", "users"):
        sql = views.view(table, "brands").sql(dialect="bigquery")
        assert f"`{DATASET}." in sql and "Brand" not in sql
        for ref in base_table_refs(views.view(table, "brands")):
            assert (ref.catalog, ref.db) == ("bigquery-public-data", "thelook_ecommerce")


def test_a_view_is_a_fresh_copy_each_time(views):
    first = views.view("products", "brands")
    first.set("where", None)
    assert BRAND_FILTER in views.view("products", "brands").sql(dialect="bigquery")


# the rewrite ------------------------------------------------------------------------------------------------------

def rewrite(views: ScopeViews, sql: str, mode: str = "brands") -> tuple[exp.Expression, Rewritten]:
    tree = parse(sql)
    return tree, views.rewrite(base_table_refs(tree), mode)


def test_a_table_without_an_alias_keeps_its_name_so_orders_dot_status_still_resolves(views):
    out = rewrite(views, "SELECT orders.status FROM orders")[0].sql(dialect="bigquery")
    assert out.endswith(") AS orders") and out.startswith("SELECT orders.status FROM (SELECT")


def test_a_users_alias_is_kept_exactly(views):
    out = rewrite(views, "SELECT O.status FROM thelook_ecommerce.orders AS O")[0].sql(dialect="bigquery")
    assert out.endswith(") AS O")
    out = rewrite(views, "SELECT `my alias`.status FROM orders AS `my alias`")[0].sql(dialect="bigquery")
    assert out.endswith(") AS `my alias`")


def test_a_fully_qualified_name_without_alias_is_referred_to_by_its_last_part(views):
    out = rewrite(views, "SELECT users.state FROM `bigquery-public-data.thelook_ecommerce.users`")[0].sql(dialect="bigquery")
    assert out.endswith(") AS `users`")  # the table name the model wrote, with the quotes it wrote it in


@pytest.mark.parametrize("sql", [
    "SELECT thelook_ecommerce.orders.order_id FROM thelook_ecommerce.orders",
    "SELECT `bigquery-public-data.thelook_ecommerce.orders`.order_id FROM `bigquery-public-data.thelook_ecommerce.orders`",
    "SELECT `bigquery-public-data`.thelook_ecommerce.orders.order_id FROM `bigquery-public-data`.thelook_ecommerce.orders",
    "SELECT thelook_ecommerce.orders.* FROM thelook_ecommerce.orders",
])
def test_a_column_that_names_an_unaliased_table_by_its_path_still_resolves_after_the_rewrite(views, sql):
    """Once the table is a view called `orders`, only `orders.order_id` resolves; the path in front of it would not."""
    tree, _ = rewrite(views, sql)
    for column in tree.find_all(exp.Column):
        assert column.args.get("db") is None and column.args.get("catalog") is None, tree.sql(dialect="bigquery")


def test_a_path_is_kept_on_a_column_of_a_table_that_has_an_alias(views):
    """With an alias the table is called by it. The path was never valid there, and is left for BigQuery to refuse."""
    tree, _ = rewrite(views, "SELECT thelook_ecommerce.orders.order_id FROM thelook_ecommerce.orders AS o")
    assert any(c.args.get("db") is not None for c in tree.find_all(exp.Column))


def test_the_views_own_table_references_are_not_rewritten_again(views):
    tree, done = rewrite(views, "SELECT * FROM orders o JOIN users u ON u.id = o.user_id")
    assert len(done.views) == 2 and done.tables == ("orders", "users")  # one per reference the model wrote, not one per table inside a view
    assert len(base_table_refs(tree)) == views.reference_count("orders", "brands") + views.reference_count("users", "brands")
    views.verify(tree, done)


def test_every_reference_in_every_clause_is_replaced(views):
    tree, done = rewrite(
        views,
        "WITH c AS (SELECT order_id FROM orders) SELECT (SELECT COUNT(*) FROM users) AS n, o.order_id "
        "FROM orders o JOIN order_items oi USING (order_id) WHERE o.order_id IN (SELECT id FROM products) "
        "UNION ALL SELECT 1, order_id FROM c",
    )
    views.verify(tree, done)
    assert len(done.views) == 5 and tree.sql(dialect="bigquery").count(BRAND_FILTER) == 5


def test_a_cte_is_not_a_table_so_it_is_not_replaced(views):
    tree, done = rewrite(views, "WITH orders AS (SELECT 1 AS order_id) SELECT * FROM orders")
    assert done.views == () and done.tables == ()
    views.verify(tree, done)


# the post-condition (fail closed) ---------------------------------------------------------------------------------

def test_the_post_condition_catches_a_table_the_rewrite_skipped(views):
    tree = parse("SELECT * FROM orders o JOIN users u ON u.id = o.user_id")
    done = views.rewrite(base_table_refs(tree)[:1], "brands")  # deliberately leave the second one alone
    with pytest.raises(AgentError) as err:
        views.verify(tree, done)
    assert err.value.kind == ErrorKind.GUARD_VIOLATION and err.value.rule_id == "SCOPE_REWRITE_INCOMPLETE"
    assert "outside the scope views" in err.value.detail


def test_a_lookalike_view_the_model_wrote_is_not_a_view(views):
    """Only the nodes `rewrite` made count. A subquery that merely looks like a view does not."""
    tree = parse(f"SELECT * FROM (SELECT p.id FROM `{DATASET}.products` AS p WHERE p.brand IN UNNEST(@brands)) AS products")
    with pytest.raises(AgentError) as err:
        views.verify(tree, Rewritten((), ()))
    assert err.value.rule_id == "SCOPE_REWRITE_INCOMPLETE"


@pytest.mark.parametrize("comment", ["/* sqlglot.meta scope_view */", "/* sqlglot.meta scope_view=true */", "/* sqlglot.meta k=v */"])
def test_a_comment_in_the_sql_cannot_make_a_table_count_as_inside_a_view(views, comment):
    """sqlglot copies `/* sqlglot.meta key=value */` into node metadata. The post-condition does not read metadata."""
    tree = parse(f"SELECT COUNT(*) FROM orders {comment}")
    assert tree.find(exp.Table).meta  # the comment really did put metadata on the node
    with pytest.raises(AgentError) as err:
        views.verify(tree, Rewritten((), ()))
    assert err.value.rule_id == "SCOPE_REWRITE_INCOMPLETE"


def test_the_guard_forgets_metadata_that_came_from_comments(guard):
    """It could otherwise change what the generator writes: a function name, for one."""
    sql = "SELECT COUNT(*) AS n /* sqlglot.meta bigquery_name=EXTERNAL_QUERY */ FROM orders"
    assert "EXTERNAL_QUERY" in parse(sql).sql(dialect="bigquery", normalize_functions="upper")  # what an unguarded generator did
    gq = accepted(guard, sql, ALICE)
    assert "EXTERNAL_QUERY" not in gq.sql and gq.sql.startswith("SELECT COUNT(*) AS n FROM (")
    assert brand_filters(gq.sql) == 1


def test_the_guard_does_not_let_a_comment_forge_a_view_either(guard):
    gq = accepted(guard, "SELECT COUNT(*) AS n FROM orders /* sqlglot.meta scope_view */", ALICE)
    assert brand_filters(gq.sql) == 1


def test_the_post_condition_checks_that_all_the_views_are_still_there(views):
    tree, done = rewrite(views, "SELECT * FROM orders")
    other = exp.Subquery(this=parse("SELECT 1"), alias=exp.TableAlias(this=exp.to_identifier("x")))
    with pytest.raises(AgentError) as err:
        views.verify(tree, Rewritten(done.tables + ("orders",), done.views + (other,)))  # one more view than the tree holds
    assert err.value.rule_id == "SCOPE_REWRITE_INCOMPLETE" and "expected 2" in err.value.detail


def test_a_view_that_was_swapped_for_a_copy_is_no_longer_the_view(views):
    """Identity, not shape: a node that looks the same but is not the one `rewrite` made does not count."""
    tree, done = rewrite(views, "SELECT * FROM orders")
    (view,) = done.views
    view.replace(view.copy())
    with pytest.raises(AgentError):
        views.verify(tree, done)


def test_an_unapproved_table_at_rewrite_time_is_a_bug_and_is_refused(views):
    tree = parse("SELECT * FROM payments")
    with pytest.raises(AgentError) as err:
        views.rewrite(base_table_refs(tree), "brands")
    assert err.value.rule_id == "SCOPE_REWRITE_INCOMPLETE"


# the text that runs is read back and checked once more -----------------------------------------------------------------

def test_the_output_check_catches_a_table_the_rewrite_left_in_the_text(guard):
    with pytest.raises(AgentError) as err:
        guard._verify_output("SELECT COUNT(*) FROM orders", "brands", 0)  # a bare table spelling, no views
    assert err.value.rule_id == "SCOPE_REWRITE_INCOMPLETE" or err.value.rule_id is None


def test_the_output_check_catches_a_missing_or_extra_brand_parameter(guard):
    sql = accepted(guard, "SELECT COUNT(*) AS n FROM products", ALICE).sql
    guard._verify_output(sql, "brands", 1)
    with pytest.raises(AgentError) as err:
        guard._verify_output(sql, "all", 1)  # brand filter present for a user who should have none
    assert err.value.rule_id == "SCOPE_REWRITE_INCOMPLETE"
    with pytest.raises(AgentError):
        guard._verify_output(sql.replace("@brands", "ARRAY['x']"), "brands", 1)


def test_the_output_check_catches_a_blocked_name_that_reached_the_text(guard):
    with pytest.raises(AgentError) as err:
        guard._verify_output(f"SELECT email FROM `{DATASET}.users` AS u", "all", 1)
    assert err.value.rule_id == "PII_COLUMN"


def test_the_output_check_refuses_two_statements_and_unreadable_text(guard):
    with pytest.raises(AgentError) as err:
        guard._verify_output("SELECT 1; SELECT 2", "all", 0)
    assert err.value.rule_id == "SCOPE_REWRITE_INCOMPLETE"
    with pytest.raises(AgentError) as err:
        guard._verify_output("SELECT FROM WHERE", "all", 0)
    assert err.value.kind == ErrorKind.SQL_SYNTAX and err.value.repairable


# fuzz: 200 mutated queries never leak a blocked name or an unscoped table ------------------------------------------------

# (SQL, number of base-table references, expected to be refused whatever the spelling)
ACCEPTABLE = [
    ("SELECT status, COUNT(*) AS n FROM orders GROUP BY status", 1),
    ("SELECT p.brand, SUM(oi.sale_price) AS rev FROM order_items oi JOIN products p ON oi.product_id = p.id GROUP BY p.brand ORDER BY rev DESC LIMIT 10", 2),
    ("WITH m AS (SELECT DATE_TRUNC(created_at, MONTH) AS mth, SUM(sale_price) AS rev FROM order_items GROUP BY mth) SELECT mth, rev FROM m ORDER BY mth", 1),
    ("SELECT user_id, SUM(sale_price) AS spend FROM order_items GROUP BY user_id ORDER BY spend DESC LIMIT 20", 1),
    ("SELECT state, COUNT(*) AS n FROM users GROUP BY state", 1),
    ("SELECT COUNT(*) AS n FROM orders o WHERE EXISTS (SELECT 1 FROM order_items oi WHERE oi.order_id = o.order_id)", 2),
    ("SELECT brand AS label FROM products UNION ALL SELECT category AS label FROM products", 2),
    ("SELECT * FROM products", 1),
]
MUST_REFUSE = [
    "SELECT email FROM users",
    "SELECT CONCAT(first_name, ' ', last_name) AS n FROM users",
    "SELECT e FROM (SELECT email AS e FROM users)",
    "SELECT * FROM users",
    "SELECT user_id FROM orders",
    "SELECT age FROM users",
    "DROP TABLE orders",
    "SELECT 1; SELECT 2",
    "SELECT * FROM payments",
    "SELECT TO_JSON_STRING(u) FROM users u",
    "SELECT latitude FROM users WHERE longitude > 0",
]
SEPARATORS = [" ", " ", "  ", "\n", "\t", " /* note */ ", " -- note\n", "\n\n"]
VARIANTS_PER_QUERY = 11


def mutate(sql: str, rng: random.Random) -> str:
    """The same query in another spelling: random case, backticks around plain names, odd spacing and comments."""
    tokens = sqlglot.tokenize(sql, read="bigquery")
    pieces = []
    for i, token in enumerate(tokens):
        raw = sql[token.start:token.end + 1]
        quoted = raw[:1] in "'\"`"
        if not quoted and raw.replace("_", "a").isalpha():
            nxt = sql[token.end + 1:token.end + 2]
            if token.token_type == TokenType.VAR and nxt != "(" and rng.random() < 0.3:
                raw = f"`{raw}`"
            elif rng.random() < 0.5:
                raw = "".join(c.upper() if rng.random() < 0.5 else c.lower() for c in raw)
        pieces.append(raw)
    return "".join(piece + rng.choice(SEPARATORS) for piece in pieces).strip()


def test_two_hundred_mutated_queries_never_leak_a_blocked_name_or_an_unscoped_table(guard):
    rng = random.Random(20261023)
    checked = accepted_count = 0
    for sql, references in ACCEPTABLE:
        for _ in range(VARIANTS_PER_QUERY):
            variant = mutate(sql, rng)
            for user in (ALICE, CEO):
                checked += 1
                try:
                    gq = guard.check(variant, user)
                except AgentError:
                    continue  # refused is always safe; the invariants are about what is let through
                accepted_count += 1
                tree = parse(gq.sql)
                names = {p.lower() for i in tree.find_all(exp.Identifier, exp.Var) for p in i.name.split(".")}
                assert not names & set(BLOCKED), variant
                assert gq.sql.count(BRAND_FILTER) == (references if user is ALICE else 0), variant
                for ref in base_table_refs(tree):
                    assert (ref.catalog, ref.db) == ("bigquery-public-data", "thelook_ecommerce"), variant
                assert gq.limit <= 1000 and ("LIMIT" in gq.sql), variant
    for sql in MUST_REFUSE:
        for _ in range(VARIANTS_PER_QUERY):
            variant = mutate(sql, rng)
            for user in (ALICE, CEO):
                checked += 1
                with pytest.raises(AgentError):
                    guard.check(variant, user)
    assert checked >= 400 and accepted_count >= 0.4 * len(ACCEPTABLE) * VARIANTS_PER_QUERY * 2  # not a vacuous test


def test_the_mutator_really_changes_the_spelling_and_most_variants_still_run(guard):
    rng = random.Random(1)
    sql = "SELECT status, COUNT(*) AS n FROM orders GROUP BY status"
    variants = {mutate(sql, rng) for _ in range(20)}
    assert len(variants) > 10 and sql not in variants
    runs = 0
    for variant in variants:
        try:
            guard.check(variant, ALICE)
            runs += 1
        except AgentError:
            pass
    assert runs >= len(variants) // 2


# the trace -----------------------------------------------------------------------------------------------------------

def test_decisions_reach_the_trace_and_a_block_is_counted_but_a_models_mistake_is_not(tmp_path):
    guard = make_guard()
    tracer = Tracer(tmp_path / "traces", tmp_path / "runs.jsonl")
    with tracer.start_turn("hash0001", "conv-1", "t-1") as turn:
        guard.check("SELECT COUNT(*) AS n FROM orders", ALICE)
        refused(guard, "SELECT email FROM users")
        refused(guard, "SELECT revenue FROM orders")  # unknown column: the model's to repair, not a guard decision
        refused(guard, "SELECT 1 AS one")  # reads no table: a guard decision, counted as a block (plan 12, A2)
    spans = [json.loads(line) for line in tracer.trace_path(turn.trace_id).read_text(encoding="utf-8").splitlines()]
    guard_spans = [s for s in spans if s["name"] == "guard.sql"]
    assert len(guard_spans) == 4 and {s["kind"] for s in guard_spans} == {"guard"}
    decisions = [(e["attrs"]["outcome"], e["attrs"]["rule"]) for s in guard_spans for e in s["events"] if e["name"] == "guard.decision"]
    assert decisions == [("rewrite", "SCOPE_BRANDS"), ("block", "PII_COLUMN"), ("block", "NO_DATA_TABLE")]
    assert [s["status"] for s in guard_spans] == ["ok", "error", "error", "ok"] or guard_spans[1]["error_kind"] == "GUARD_VIOLATION"
    assert guard_spans[2]["error_kind"] == "SQL_UNKNOWN_COLUMN"
    assert read_runs(tracer.metrics_file)[0][0]["guard_blocks"]["sql"] == 2  # the PII column and the query with no table
    text = json.dumps(guard_spans)
    assert "SELECT" not in text and "Brand A" not in text  # no SQL and no brand name in the guard's spans


def test_the_trace_names_the_rule_of_a_refusal_and_the_fingerprint_of_a_pass(tmp_path):
    guard = make_guard()
    tracer = Tracer(tmp_path / "traces", tmp_path / "runs.jsonl")
    with tracer.start_turn("hash0001", "conv-1", "t-1") as turn:
        gq = guard.check("SELECT COUNT(*) AS n FROM orders", ALICE)
    (span,) = [json.loads(l) for l in tracer.trace_path(turn.trace_id).read_text(encoding="utf-8").splitlines() if '"guard.sql"' in l]
    assert span["attrs"]["fingerprint"] == gq.fingerprint and span["attrs"]["tables"] == ["orders"] and span["attrs"]["limit"] == 1000


def test_a_crash_in_the_guard_is_recorded_as_a_block(tmp_path, monkeypatch):
    guard = make_guard()
    monkeypatch.setattr(guard, "_analyze", lambda root: 1 / 0)
    tracer = Tracer(tmp_path / "traces", tmp_path / "runs.jsonl")
    with tracer.start_turn("hash0001", "conv-1", "t-1") as turn:
        refused(guard, "SELECT COUNT(*) FROM orders")
    spans = [json.loads(line) for line in tracer.trace_path(turn.trace_id).read_text(encoding="utf-8").splitlines()]
    (decision,) = [e for s in spans for e in s["events"] if e["name"] == "guard.decision"]
    assert decision["attrs"]["rule"] == "GUARD_ERROR" and read_runs(tracer.metrics_file)[0][0]["guard_blocks"]["sql"] == 1


def test_sqlglots_own_warning_with_a_snippet_of_the_query_does_not_reach_the_logs(caplog):
    with caplog.at_level(logging.DEBUG):
        refused(make_guard(), "EXECUTE IMMEDIATE 'secret-canary-literal'")
    assert "secret-canary-literal" not in caplog.text


# hand-off to the query runner -----------------------------------------------------------------------------------------

def test_a_guarded_query_runs_through_the_safe_runner_with_the_brands_as_a_parameter(fake_bigquery, guard):
    from src.data_access.safe_runner import SafeQueryRunner

    fake_bigquery.add("UNNEST(@brands)", df=pd.DataFrame({"n": [7]}), bytes_processed=3_000_000)
    safe = SafeQueryRunner(make_settings(), FakeRunner(fake_bigquery))
    gq = guard.check("SELECT COUNT(*) AS n FROM orders", ALICE)
    outcome = safe.run(gq)
    assert int(outcome.df["n"].iloc[0]) == 7 and len(fake_bigquery.dry_jobs) == 1 and len(fake_bigquery.real_jobs) == 1
    for job in fake_bigquery.jobs:
        assert job.sql == gq.sql
        (param,) = job.config.query_parameters
        assert (param.name, param.values) == ("brands", ["Brand A", "Brand B"])


def test_the_ceo_runs_without_parameters(fake_bigquery, guard):
    from src.data_access.safe_runner import SafeQueryRunner

    fake_bigquery.add("FROM (SELECT o.order_id", df=pd.DataFrame({"n": [9]}))
    safe = SafeQueryRunner(make_settings(), FakeRunner(fake_bigquery))
    safe.run(guard.check("SELECT COUNT(*) AS n FROM orders", CEO))
    assert all(job.config.query_parameters == [] for job in fake_bigquery.jobs)


def test_the_original_text_never_runs(fake_bigquery):
    """The runner sees the guard's text, not the model's. Even a query with a comment attack arrives rewritten."""
    from src.data_access.safe_runner import SafeQueryRunner

    fake_bigquery.add("COUNT(*)", df=pd.DataFrame({"n": [1]}))
    safe = SafeQueryRunner(make_settings(), FakeRunner(fake_bigquery))
    gq = make_guard().check("SELECT COUNT(*) AS n FROM orders /* ' OR 1=1 -- */", ALICE)
    safe.run(gq)
    assert all("OR 1=1" not in job.sql for job in fake_bigquery.jobs)


def test_a_refused_query_leaves_no_job_at_bigquery(fake_bigquery, guard):
    """The path a question takes is guard, then runner. A refusal raises before the runner is called, and the runner
    on its own refuses anything the guard did not issue. Either way BigQuery sees nothing, not even a dry run."""
    from src.data_access.safe_runner import SafeQueryRunner

    safe = SafeQueryRunner(make_settings(), FakeRunner(fake_bigquery))
    for sql in ("SELECT email FROM users", "SELECT * FROM users", "DROP TABLE orders", "SELECT 1; SELECT 2", "SELECT * FROM payments"):
        with pytest.raises(AgentError):
            safe.run(guard.check(sql, ALICE))
        with pytest.raises(AgentError) as err:
            safe.run(sql)  # the text itself, as if someone skipped the guard
        assert err.value.rule_id == "UNGUARDED_QUERY"
    assert fake_bigquery.jobs == []
