"""The revenue definition in code (plan 12, D2): a plain sum of sale prices must leave out cancelled and returned items, or the query goes back to the SQL repair step.

The golden run of 2026-10-25 counted returned items as revenue in one sub-question of a report, because the same query also counted returns. The prompt had the
definition; nothing in code checked the SQL. These tests are about what the check reads (the question and the tree), what it leaves alone, and what the graph does
with a query it refuses: `SQL_DEFINITION` is the model's own mistake, so it is repaired and counted, never refused like a guard violation.
"""
import re

import pandas as pd
import pytest
import yaml

from src.agent.definition_check import DefinitionCheck, build_definition_check, excluded_statuses, unfiltered_sums
from src.errors import AgentError, ErrorKind
from src.llm.routing import Step
from tests.agent_support import composer_reply, guard_reply, make_rig, plan_reply, sql_reply
from tests.guard_support import REPO_ROOT

GOOD = (
    "SELECT p.brand, SUM(oi.sale_price) AS revenue FROM order_items oi JOIN products p ON p.id = oi.product_id "
    "WHERE oi.status NOT IN ('Cancelled', 'Returned') GROUP BY p.brand ORDER BY revenue DESC"
)
ONLY_CANCELLED = GOOD.replace("NOT IN ('Cancelled', 'Returned')", "!= 'Cancelled'")
STILL_WRONG = ONLY_CANCELLED + ", p.brand"  # another text, the same mistake
ROWS = pd.DataFrame({"brand": ["Brand A", "Brand B"], "revenue": [1234.5, 999.0]})
REVENUE = "What was the revenue of my brands?"


# what the tree says ------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "SELECT SUM(oi.sale_price) FROM order_items oi WHERE oi.status NOT IN ('Cancelled', 'Returned')",
    "SELECT SUM(oi.sale_price) FROM order_items oi WHERE oi.status NOT IN ('returned', 'CANCELLED')",
    "SELECT SUM(oi.sale_price) FROM order_items oi WHERE oi.status <> 'Cancelled' AND oi.status <> 'Returned'",
    "SELECT SUM(oi.sale_price) FROM order_items oi WHERE oi.status != 'Cancelled' AND oi.status != 'Returned' AND oi.created_at >= TIMESTAMP '2025-01-01'",
    "SELECT SUM(oi.sale_price) FROM order_items oi WHERE oi.status IN ('Complete', 'Shipped', 'Processing')",
    "SELECT SUM(oi.sale_price) FROM order_items oi WHERE oi.status != 'Cancelled' AND oi.returned_at IS NULL",
    "SELECT SUM(oi.sale_price) FROM order_items oi WHERE NOT (oi.status IN ('Cancelled', 'Returned'))",
    "SELECT SUM(oi.sale_price) FROM order_items oi WHERE LOWER(oi.status) NOT IN ('cancelled', 'returned')",
    "SELECT COALESCE(SUM(oi.sale_price), 0) AS revenue FROM order_items oi WHERE oi.status NOT IN ('Cancelled', 'Returned')",
    "SELECT ROUND(SUM(oi.sale_price), 2) AS revenue FROM order_items oi WHERE oi.status NOT IN ('Cancelled', 'Returned')",
    "SELECT SUM(oi.sale_price) FROM orders o JOIN order_items oi ON oi.order_id = o.order_id AND oi.status NOT IN ('Cancelled', 'Returned')",
    "SELECT SUM(oi.sale_price) FROM orders o JOIN order_items oi ON oi.order_id = o.order_id WHERE o.status NOT IN ('Cancelled', 'Returned')",
    "SELECT SUM(sale_price) FROM order_items WHERE status NOT IN ('Cancelled', 'Returned')",
    "SELECT SUM(SUM(oi.sale_price)) OVER () FROM order_items oi WHERE oi.status NOT IN ('Cancelled', 'Returned')",
])
def test_a_sum_that_leaves_out_both_statuses_is_not_flagged(sql):
    assert unfiltered_sums(sql) == []


@pytest.mark.parametrize("sql,missing", [
    ("SELECT SUM(oi.sale_price) FROM order_items oi", "cancelled or returned"),
    ("SELECT SUM(oi.sale_price) FROM order_items oi WHERE oi.status != 'Cancelled'", "returned"),
    ("SELECT SUM(oi.sale_price) FROM order_items oi WHERE oi.status NOT IN ('Cancelled')", "returned"),
    ("SELECT SUM(oi.sale_price) FROM order_items oi WHERE oi.status = 'Complete' OR oi.status = 'Shipped'", "cancelled or returned"),  # an OR is not counted
    ("SELECT SUM(oi.sale_price) FROM order_items oi WHERE oi.status <> 'Cancelled' OR oi.status <> 'Returned'", "cancelled or returned"),
    ("SELECT SUM(oi.sale_price) FROM order_items oi WHERE oi.status IS NOT NULL", "cancelled or returned"),
    ("SELECT SUM(oi.sale_price) FROM order_items oi WHERE oi.returned_at IS NOT NULL AND oi.status != 'Cancelled'", "returned"),
    ("SELECT (SELECT SUM(oi.sale_price) FROM order_items oi WHERE oi.status != 'Cancelled') AS a", "returned"),  # a subquery has its own SELECT
    ("SELECT p.brand, SUM(oi.sale_price) FROM order_items oi JOIN products p ON p.id = oi.product_id GROUP BY p.brand", "cancelled or returned"),
])
def test_a_sum_that_counts_either_status_is_flagged_with_what_is_missing(sql, missing):
    (reason,) = unfiltered_sums(sql)
    assert missing in reason


@pytest.mark.parametrize("sql", [
    "SELECT SUM(CASE WHEN oi.status = 'Returned' THEN oi.sale_price END) AS returned_value FROM order_items oi",
    "SELECT SUM(IF(oi.status = 'Returned', oi.sale_price, 0)) AS returned_value FROM order_items oi",
    "SELECT SUM(oi.sale_price * 2) FROM order_items oi",
    "SELECT SUM(oi.retail_price) FROM products oi",
    "SELECT SUM(p.retail_price) AS list_value FROM products p",
    "SELECT COUNT(*) AS n FROM order_items oi",
    "WITH good AS (SELECT * FROM order_items WHERE status NOT IN ('Cancelled', 'Returned')) SELECT SUM(g.sale_price) FROM good g",
    "SELECT SUM(t.sale_price) FROM (SELECT sale_price FROM order_items WHERE status NOT IN ('Cancelled', 'Returned')) t",
    "SELECT * FROM",  # unreadable: the SQL Guard has the say on that
])
def test_a_sum_that_is_not_the_plain_revenue_sum_is_left_alone(sql):
    assert unfiltered_sums(sql) == []


def test_the_statuses_a_select_leaves_out_are_read_from_its_where_and_its_join_conditions_only():
    import sqlglot
    from sqlglot import exp

    def out(sql):
        return excluded_statuses(sqlglot.parse_one(sql, read="bigquery").find(exp.Select))

    assert out("SELECT 1 FROM order_items oi WHERE oi.status = 'Complete'") == {"cancelled", "returned"}
    assert out("SELECT 1 FROM order_items oi WHERE oi.status = 'Returned'") == {"cancelled"}
    assert out("SELECT 1 FROM order_items oi WHERE oi.status NOT IN ('Returned', 'Shipped')") == {"returned"}
    assert out("SELECT 1 FROM order_items oi WHERE oi.status IN (SELECT status FROM order_items)") == set()
    assert out("SELECT 1 FROM order_items oi HAVING oi.status != 'Cancelled'") == set()


# what the question says ----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("question", [
    "What was my revenue in 2025?",
    "Revenue per brand for Q1 2025",
    "total sales by month",
    "What is the turnover of Nike?",
    "Count revenue as the sale price of items that were not cancelled or returned.",
    "Sales without returns, by brand",
    "How much did we make, excluding cancelled and returned items?",  # no revenue word: the definition is in the question's own words
    "top brands by revenue, net of returns",
])
def test_the_check_applies_to_a_question_about_revenue(question):
    assert DefinitionCheck().applies(question)


@pytest.mark.parametrize("question", [
    "What was my revenue including returns?",
    "gross revenue by brand",
    "What is the value of the returned items by brand?",
    "Which products have the highest return rate?",
    "Revenue lost to returns",
    "How many orders were cancelled?",
    "Which brands have the most customers?",
    "Revenue with cancelled items included",
])
def test_the_check_does_not_apply_to_a_question_that_asks_for_something_else(question):
    assert not DefinitionCheck().applies(question)


def test_the_glossary_aliases_of_revenue_count_and_the_built_in_terms_stay():
    check = build_definition_check(["revenue", "sales", "turnover", "takings"])
    assert check.applies("our takings in March") and check.applies("revenue in March")
    assert build_definition_check([]).applies("revenue in March")  # an emptied glossary does not switch the check off


def test_the_error_names_the_definition_and_the_fix_and_is_repairable_not_retryable():
    with pytest.raises(AgentError) as err:
        DefinitionCheck().check(REVENUE, ONLY_CANCELLED)
    error = err.value
    assert error.kind is ErrorKind.SQL_DEFINITION and error.repairable and not error.retryable and error.rule_id is None
    assert "without cancelled or returned items" in error.detail and "NOT IN ('Cancelled', 'Returned')" in error.detail and "returned" in error.detail
    DefinitionCheck().check(REVENUE, GOOD)  # fine
    DefinitionCheck().check("Which brands have the most customers?", ONLY_CANCELLED)  # not about revenue: nothing to check


# the reference SQL of the repository --------------------------------------------------------------------------------------------

MACROS = {  # as `Truth._values` fills them: a brand and the brands come with their quotes, a date without (the SQL writes the quotes round it)
    "brands": "('A', 'B')", "brand": "'A'", "other_brand": "'B'", "latest_date": "2026-10-22", "this_month_start": "2026-10-01",
    "last_month_start": "2026-09-01", "last_month": "September 2026", "brands_text": "A, B",
}


def expand(sql: str) -> str:
    return re.sub(r"\{([a-z_]+)\}", lambda match: MACROS.get(match.group(1), match.group(0)), sql)


def repository_queries() -> list[tuple[str, str, str]]:
    """(where, the question the SQL answers, the SQL) for the reference SQL of every golden case and trajectory case and for every seed trio."""
    found = []
    for name in ("golden_cases", "trajectories"):
        for case in yaml.safe_load((REPO_ROOT / "eval" / "datasets" / f"{name}.yaml").read_text(encoding="utf-8")):
            for index, turn in enumerate(case.get("turns") or [{"say": case.get("question"), "reference_sql": case.get("reference_sql")}], 1):
                if turn.get("reference_sql"):
                    found.append((f"{name}:{case['id']}#{index}", turn.get("say") or turn.get("question") or case.get("question") or "", turn["reference_sql"]))
    for path in sorted((REPO_ROOT / "golden" / "seed").glob("*.yaml")):
        trio = yaml.safe_load(path.read_text(encoding="utf-8"))
        found.append((f"seed:{trio['id']}", trio["question"] + " " + trio.get("interpretation_notes", ""), trio["sql"]))
    return found


def test_the_repository_has_enough_reference_sql_for_this_test_to_mean_something():
    queries = repository_queries()
    assert len(queries) >= 25
    applied = [where for where, question, _ in queries if DefinitionCheck().applies(question)]
    assert len(applied) >= 5, applied  # the check really looked at some of them


@pytest.mark.parametrize("where,question,sql", repository_queries(), ids=lambda value: value if isinstance(value, str) and len(value) < 60 else None)
def test_no_reference_sql_of_a_golden_case_a_trajectory_or_a_seed_trio_is_flagged(where, question, sql):
    import sqlglot

    text = expand(sql)
    assert sqlglot.parse_one(text, read="bigquery") is not None  # it was read, so a pass is not a parse failure
    DefinitionCheck().check(question, text)


# in the graph --------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def rig(project, fake_bigquery):
    fake_bigquery.add("revenue", df=ROWS)
    return make_rig(project, fake_bigquery=fake_bigquery)


def simple(rig, *sql):
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply(standalone=REVENUE))
    rig.gateway.queue(Step.SQL_WRITE, sql_reply(sql[0]))
    if len(sql) > 1:
        rig.gateway.queue(Step.SQL_REPAIR, *(sql_reply(text) for text in sql[1:]))
    rig.gateway.queue(Step.COMPOSER, composer_reply("Brand A earned 1,234.5 and Brand B 999."))
    return rig.ask(REVENUE)


def test_a_revenue_query_that_counts_returns_is_sent_back_once_and_the_repaired_query_runs(rig):
    result = simple(rig, ONLY_CANCELLED, GOOD)
    assert result.outcome == "answered" and "1234.5" in result.text
    assert rig.calls(Step.SQL_REPAIR) == 1 and len(rig.queries) == 1  # the wrong query never reached the data
    repair = rig.gateway.calls_for(Step.SQL_REPAIR)[0].contents
    assert "without cancelled or returned items" in repair and ONLY_CANCELLED.split(" WHERE ")[1][:30] in repair  # the definition and the failing SQL
    assert "'Cancelled'" in rig.queries[0].sql and "'Returned'" in rig.queries[0].sql  # the repaired query: the guard writes the filter as NOT status IN (...)
    assert rig.last_row["repairs"] == 1  # it counts against loop.sql_repairs
    assert "sql.definition_failed" in [event["name"] for span in rig.spans() for event in span["events"]]


def test_the_same_mistake_after_the_repair_leaves_a_gap_line_and_no_figure(project, fake_bigquery):
    fake_bigquery.add("revenue", df=ROWS)
    fake_bigquery.add("returned", df=pd.DataFrame({"brand": ["Brand A"], "returns": [5.0]}))
    rig = make_rig(project, fake_bigquery=fake_bigquery)
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply("report_request", "A report of my revenue and returns"))
    rig.gateway.queue(Step.PLANNER, plan_reply("Revenue by brand", "Returned items by brand"))
    returned = "SELECT p.brand, COUNT(*) AS returns FROM order_items oi JOIN products p ON p.id = oi.product_id WHERE oi.status = 'Returned' GROUP BY p.brand"
    rig.gateway.queue(Step.SQL_WRITE, sql_reply(ONLY_CANCELLED), sql_reply(returned))
    rig.gateway.queue(Step.SQL_REPAIR, sql_reply(STILL_WRONG), sql_reply(STILL_WRONG + " "))
    rig.gateway.queue(Step.EVIDENCE_CHECK, {"enough": True, "missing": []})
    rig.gateway.queue(Step.COMPOSER, composer_reply("Brand A had returned items."))
    result = rig.ask("A report of my revenue and returns")
    assert result.outcome == "partial"
    assert 'I could not get "Revenue by brand".' in result.text  # a missing figure, said so
    assert "1234.5" not in result.text and "999" not in result.text  # and not a wrong one
    assert all("status != 'Cancelled'" not in query.sql and "'Cancelled'" not in query.sql.split("WHERE")[-1] for query in rig.queries)  # the wrong query never ran
    assert len(rig.queries) == 1  # only the returns query ran


def test_a_question_that_asks_for_returns_is_not_held_to_the_revenue_definition(rig):
    returned_only = "SELECT p.brand, SUM(oi.sale_price) AS revenue FROM order_items oi JOIN products p ON p.id = oi.product_id WHERE oi.status = 'Returned' GROUP BY p.brand"
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply(standalone="What is the value of the returned items by brand?"))
    rig.gateway.queue(Step.SQL_WRITE, sql_reply(returned_only))
    rig.gateway.queue(Step.COMPOSER, composer_reply("Brand A earned 1,234.5 and Brand B 999."))
    result = rig.ask("What is the value of the returned items by brand?")
    assert result.outcome == "answered" and rig.calls(Step.SQL_REPAIR) == 0 and len(rig.queries) == 1


def test_a_guard_violation_in_the_same_query_is_still_never_retried(rig):
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply(standalone=REVENUE))
    rig.gateway.queue(Step.SQL_WRITE, sql_reply("SELECT u.email, SUM(oi.sale_price) AS revenue FROM order_items oi JOIN users u ON u.id = oi.user_id GROUP BY u.email"))
    result = rig.ask(REVENUE)
    assert result.outcome == "refused" and result.error.rule_id == "PII_COLUMN"
    assert rig.calls(Step.SQL_REPAIR) == 0 and not rig.queries  # the guard comes first, and its verdict is final (decision 3.11)


def test_without_the_check_the_query_that_counts_returns_runs(rig, monkeypatch):
    monkeypatch.setattr(DefinitionCheck, "check", lambda self, question, sql: None)
    result = simple(rig, ONLY_CANCELLED)
    assert result.outcome == "answered" and rig.calls(Step.SQL_REPAIR) == 0
    assert len(rig.queries) == 1 and "'Returned'" not in rig.queries[0].sql  # the query that counts returned items ran


def test_the_error_kind_is_one_of_the_repairable_ones_in_the_loop():
    from src.agent.nodes.sql_loop import SYNTAX_KINDS

    assert ErrorKind.SQL_DEFINITION in SYNTAX_KINDS and AgentError(ErrorKind.SQL_DEFINITION).repairable
