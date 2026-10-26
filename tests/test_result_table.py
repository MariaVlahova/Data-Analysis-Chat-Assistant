"""The figures of a simple answer, built by code (plan 12, D1; T5, CAP0, CAP1, CAP3a).

The real-model run of 2026-10-25 failed three golden cases on their key figures, and the first kind of failure was a summary: the Composer wrote a range ("from $850.96 to $3,864.28")
where the question asked for each month. A simple turn with one result now ends with that result as a table the code builds from the guarded rows, so a short or summarising
draft cannot leave out the figures that were asked for. These tests are about when the table is there, how long it is, and that it is the guarded rows and nothing else.
"""
import pandas as pd
import pytest

from src.agent.envelope import render_table, result_table
from src.guards.output_guard import OutputGuard
from src.llm.routing import Step
from src.llm.fake import FakeGateway
from tests.agent_support import REVENUE_SQL, composer_reply, guard_reply, make_rig, plan_reply, sql_reply

MONTHS = pd.DataFrame({"brand": [f"2025-{month:02d}" for month in range(1, 13)], "revenue": [850.96 + 250 * month for month in range(12)]})
RANGE = "Revenue ranged from 850.96 to 3,600.96 over the year."  # the minimum and the maximum of the result: grounded, and no month in it


def turn(rig, body=RANGE, *, question="What was the monthly revenue of my brands?"):
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply(standalone=question))
    rig.gateway.queue(Step.SQL_WRITE, sql_reply(REVENUE_SQL))
    rig.gateway.queue(Step.COMPOSER, composer_reply(body))
    return rig.ask(question)


def rig_for(project, fake_bigquery, df=MONTHS, *, user=None, **options):
    fake_bigquery.add("revenue", df=df)
    return make_rig(project, *([user] if user else []), fake_bigquery=fake_bigquery, **options)


def test_a_composer_that_writes_only_a_range_still_shows_every_months_figure_from_the_table(project, fake_bigquery):
    rig = rig_for(project, fake_bigquery)
    result = turn(rig)
    assert "ranged from 850.96 to 3,600.96" in result.text
    for month, revenue in zip(MONTHS["brand"], MONTHS["revenue"]):
        assert f"| {month} | " in result.text, month
    assert "| 3100.96 |" in result.text and "| 850.96 |" in result.text
    assert result.outcome == "answered"


def test_the_table_comes_after_the_body_and_before_the_notes(project, fake_bigquery):
    result = turn(rig_for(project, fake_bigquery))
    text = result.text
    assert text.index(RANGE) < text.index("| brand | revenue |") < text.index("**Notes**") if "**Notes**" in text else text.index(RANGE) < text.index("| brand | revenue |")
    assert text.count("| brand | revenue |") == 1


def test_a_result_of_30_rows_shows_20_and_says_10_more(project, fake_bigquery):
    many = pd.DataFrame({"brand": [f"Brand {n:02d}" for n in range(30)], "revenue": [100.0 + n for n in range(30)]})
    result = turn(rig_for(project, fake_bigquery, many), "Revenue ranged from 100 to 129.")
    shown = [line for line in result.text.splitlines() if line.startswith("| Brand ")]
    assert len(shown) == 20 and shown[0].startswith("| Brand 00") and shown[-1].startswith("| Brand 19")
    assert "10 more rows are not shown." in result.text and "Brand 20" not in result.text


def test_the_number_of_rows_is_a_setting(project, fake_bigquery):
    rig = rig_for(project, fake_bigquery, config="composer:\n  result_table_rows: 5\n")
    result = turn(rig)
    assert len([line for line in result.text.splitlines() if line.startswith("| 2025-")]) == 5
    assert "7 more rows are not shown." in result.text


def test_the_table_is_built_from_the_guarded_rows_so_the_numeric_check_has_nothing_to_remove_from_it(project, fake_bigquery):
    rig = rig_for(project, fake_bigquery)
    result = turn(rig, "Revenue was 9,999.99 in March.")  # an invented figure: its sentence goes, the table stays
    assert "9,999.99" not in result.text and "| 2025-03 | 1350.96 |" in result.text
    assert "Some figures could not be verified" in result.text and result.outcome == "partial"


def test_the_output_guard_checks_the_table_with_the_rest_of_the_answer(project, fake_bigquery, monkeypatch):
    seen: list[str] = []
    original = OutputGuard.check
    monkeypatch.setattr(OutputGuard, "check", lambda self, text: seen.append(text) or original(self, text))
    result = turn(rig_for(project, fake_bigquery))
    assert len(seen) == 1 and "| 2025-12 |" in seen[0] and seen[0] == result.text


def test_a_why_turn_has_no_code_built_table(project, fake_bigquery):
    rig = rig_for(project, fake_bigquery)
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply("why_question", "Why did revenue change?"))
    rig.gateway.queue(Step.PLANNER, plan_reply("Revenue by month"))
    rig.gateway.queue(Step.SQL_WRITE, sql_reply(REVENUE_SQL))
    rig.gateway.queue(Step.EVIDENCE_CHECK, {"enough": True, "missing": []})
    rig.gateway.queue(Step.COMPOSER, composer_reply(RANGE))
    result = rig.ask("Why did my revenue change?")
    assert RANGE in result.text and "| brand | revenue |" not in result.text  # its figures stay in the text: several tables would bury the answer


def test_a_report_turn_has_no_code_built_table_either(project, fake_bigquery):
    rig = rig_for(project, fake_bigquery)
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply("report_request", "A report of my revenue"))
    rig.gateway.queue(Step.PLANNER, plan_reply("Revenue by month"))
    rig.gateway.queue(Step.SQL_WRITE, sql_reply(REVENUE_SQL))
    rig.gateway.queue(Step.EVIDENCE_CHECK, {"enough": True, "missing": []})
    rig.gateway.queue(Step.COMPOSER, composer_reply(RANGE, "Revenue report"))
    assert "| brand | revenue |" not in rig.ask("Write a report of my revenue").text


def test_a_result_with_no_data_has_no_table(project, fake_bigquery):
    rig = rig_for(project, fake_bigquery, pd.DataFrame({"brand": [], "revenue": []}))
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply(standalone="Revenue by brand"))
    rig.gateway.queue(Step.SQL_WRITE, sql_reply(REVENUE_SQL))
    rig.gateway.queue(Step.SQL_REPAIR, sql_reply(REVENUE_SQL.replace("DESC", "ASC")))
    rig.gateway.queue(Step.COMPOSER, composer_reply("There is no revenue in this period."))
    result = rig.ask("What was my revenue by brand?")
    assert "no revenue in this period" in result.text and "| brand |" not in result.text


def test_the_answer_when_the_composer_cannot_run_has_its_tables_once(project, fake_bigquery):
    from src.errors import AgentError, ErrorKind

    rig = rig_for(project, fake_bigquery)
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply(standalone="Revenue by brand"))
    rig.gateway.queue(Step.SQL_WRITE, sql_reply(REVENUE_SQL))
    rig.gateway.queue(Step.COMPOSER, AgentError(ErrorKind.LLM_UNAVAILABLE))
    result = rig.ask("What was my revenue by brand?")
    assert result.text.count("| brand | revenue |") == 1 and "straight from the data" in result.text


def test_the_composer_is_told_a_table_follows_on_a_simple_turn_and_not_on_a_why_turn(project, fake_bigquery):
    rig = rig_for(project, fake_bigquery)
    turn(rig)
    assert "adds a table with the rows of the result after your text" in rig.gateway.calls_for(Step.COMPOSER)[0].contents
    why = rig_for(project, fake_bigquery)
    why.gateway.queue(Step.INPUT_GUARD, guard_reply("why_question", "Why did revenue change?"))
    why.gateway.queue(Step.PLANNER, plan_reply("Revenue by month"))
    why.gateway.queue(Step.SQL_WRITE, sql_reply(REVENUE_SQL))
    why.gateway.queue(Step.EVIDENCE_CHECK, {"enough": True, "missing": []})
    why.gateway.queue(Step.COMPOSER, composer_reply(RANGE))
    why.ask("Why did my revenue change?")
    assert "adds a table" not in why.gateway.calls_for(Step.COMPOSER)[0].contents


# the pieces ----------------------------------------------------------------------------------------------------------------------

def guarded(rows: int) -> dict:
    return {"columns": ["brand", "revenue"], "rows": [{"brand": f"B{n}", "revenue": float(n)} for n in range(rows)], "total_rows": rows}


def state_with(guarded_result, *, kind="simple", status="ok", subquestions=1) -> dict:
    plan = {"subquestions": [{"id": f"q{n}", "text": "x", "depends_on": [], "tables": []} for n in range(1, subquestions + 1)]}
    return {"turn_kind": kind, "plan": plan, "results": {"q1": {"status": status, "guarded": guarded_result}}}


def test_the_table_is_built_only_for_a_simple_turn_with_one_result_that_has_data():
    assert result_table(state_with(guarded(3)), 20).startswith("| brand | revenue |")
    assert result_table(state_with(guarded(3), kind="why"), 20) == "" and result_table(state_with(guarded(3), kind="report"), 20) == ""
    assert result_table(state_with(guarded(3), status="empty"), 20) == "" and result_table(state_with(guarded(3), status="failed"), 20) == ""
    assert result_table(state_with(guarded(3), subquestions=2), 20) == ""
    assert result_table(state_with(None), 20) == "" and result_table({"turn_kind": "simple"}, 20) == ""


def test_the_footer_counts_the_rows_left_out_of_the_table_and_the_default_footer_the_rows_of_the_query():
    table = render_table(guarded(30), 20, more_note=True)
    assert table.endswith("_10 more rows are not shown._") and table.count("\n| B") == 20
    assert "more rows" not in render_table(guarded(20), 20, more_note=True)
    assert render_table(guarded(30), 10).endswith("_Showing 10 of 30 rows._")  # the fallback's footer is unchanged


# order items with no brand (plan 12, C1, A23) -----------------------------------------------------------------------------------

def test_the_ceos_answer_labels_an_unbranded_group_and_says_what_it_is_and_a_managers_never_has_one(project, fake_bigquery):
    from tests.guard_support import CEO

    with_empty_brand = pd.DataFrame({"brand": ["Brand A", None], "revenue": [1234.5, 999.0]})
    ceo = rig_for(project, fake_bigquery, with_empty_brand, user=CEO)
    result = turn(ceo, "Brand A earned 1,234.5.", question="What was the revenue of each brand?")
    assert "| (no brand) | 999 |" in result.text
    assert "Some order items have no brand. Brand managers do not see them; they are counted here." in result.text
    # a brand manager's query never returns such a row: the scope views leave those items out, so there is nothing to label and nothing to explain
    alice = rig_for(project, fake_bigquery, pd.DataFrame({"brand": ["Brand A"], "revenue": [1234.5]}))
    manager = turn(alice, "Brand A earned 1,234.5.", question="What was the revenue of each brand?")
    assert "(no brand)" not in manager.text and "have no brand" not in manager.text
