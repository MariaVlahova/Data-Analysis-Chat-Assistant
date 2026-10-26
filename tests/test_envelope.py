"""The answer envelope (plan 07, F6, F32, F33, Q35): the code renders how the question was read, the scope, the notices and the
gaps, so they are in the answer even when the Composer leaves them out."""
from datetime import date

import pandas as pd
import pytest

from src.agent.envelope import (
    DEGRADED_INTRO, LIMITED_DATA_NOTE, fallback_body, gap_line, is_partial, notes_lines, reading_lines, render_answer, render_table,
)
from src.agent.prompts.locked_rules import date_hints
from src.llm.routing import Step
from tests.agent_support import composer_reply, guard_reply, make_rig, sql_reply

GUARDED = {"columns": ["state", "revenue"], "rows": [{"state": "X", "revenue": 10.5}, {"state": "Y", "revenue": 20.0}], "total_rows": 2,
           "notices": ["2 groups with fewer than 10 customers were hidden."], "summary": {}}


def state(**changes):
    base = {
        "scope_label": "your brands: Brand A", "turn_kind": "why",
        "plan": {"subquestions": [{"id": "q1", "text": "Revenue by state", "depends_on": [], "tables": []},
                                  {"id": "q2", "text": "Orders per customer", "depends_on": [], "tables": []}],
                 "assumptions": ["Recent means the last 90 days."], "date_range": {"start": "2026-07-24", "end": "2026-10-22", "label": "last 90 days"}, "notices": []},
        "results": {"q1": {"status": "ok", "interpretation": "Revenue is the sum of item prices.", "guarded": GUARDED}, "q2": {"status": "ok", "interpretation": "Revenue is the sum of item prices.", "guarded": None}},
        "context": {"freshness_note": "The data ends on 2026-10-22, so \"recent\" means up to that date."}, "evidence": {"enough": True, "missing": []},
    }
    return {**base, **changes}


# how I read your question ---------------------------------------------------------------------------------------------

def test_the_reading_lines_hold_assumptions_interpretations_the_period_and_the_freshness_note_without_repeats():
    assert reading_lines(state()) == [
        "Recent means the last 90 days.", "Revenue is the sum of item prices.", "Period: 2026-07-24 to 2026-10-22 (last 90 days).",
        "The data ends on 2026-10-22, so \"recent\" means up to that date.",
    ]


def test_the_envelope_has_the_reading_the_scope_the_body_and_the_notes_in_that_order():
    text, notes = render_answer(state(), "The body.")
    assert text.index("How I read your question") < text.index("Scope: your brands: Brand A") < text.index("The body.") < text.index("Notes")
    assert notes == ["2 groups with fewer than 10 customers were hidden."]


def test_a_report_gets_its_title_as_a_heading_and_other_turns_do_not():
    assert "## Q1 report" in render_answer(state(turn_kind="report"), "Body.", title="Q1 report")[0]
    assert "## Q1 report" not in render_answer(state(turn_kind="why"), "Body.", title="Q1 report")[0]


def test_a_turn_with_nothing_to_say_has_no_empty_sections():
    text, notes = render_answer({"turn_kind": "simple", "plan": {"subquestions": []}, "results": {}}, "Just the body.")
    assert text == "Just the body." and notes == []


# the notes -------------------------------------------------------------------------------------------------------------

def sub(text="Orders per customer"):
    return {"id": "q2", "text": text}


@pytest.mark.parametrize("status,expected", [
    ("empty", "No data was found for \"Orders per customer\"."),
    ("blocked", "I did not run \"Orders per customer\". That asks for personal details"),
    ("too_expensive", "I did not get \"Orders per customer\". That question would scan too much data. Try a narrower question"),
    ("too_slow", "I did not get \"Orders per customer\". That question took too long to run. Try a narrower question"),
    ("failed", "I could not get \"Orders per customer\". The data warehouse is not responding right now."),
    ("skipped", "I did not get to \"Orders per customer\" within the limits for one question."),
])
def test_every_status_that_is_not_ok_is_said_in_plain_words(status, expected):
    result = {"status": status, "notice": "That asks for personal details that could identify a customer.", "error_kind": "BQ_UNAVAILABLE"}
    assert gap_line(sub(), result).startswith(expected)


def test_an_ok_result_has_no_gap_line_and_an_unknown_error_kind_is_still_honest():
    assert gap_line(sub(), {"status": "ok"}) is None
    assert gap_line(sub(), {"status": "failed", "error_kind": "SOMETHING_NEW"}).startswith("I could not get")


def test_the_notes_carry_notices_gaps_missing_evidence_and_the_labels_without_repeats():
    shown = state(
        results={"q1": {"status": "ok", "guarded": GUARDED}, "q2": {"status": "too_slow"}},
        evidence={"enough": False, "missing": ["orders per customer", "return rate"]},
    )
    shown["plan"]["notices"] = ["I planned 6 sub-questions but run at most 4 for one question, so I ran the first 4."]
    notes = notes_lines(shown, numeric_removed=True, partial=True)
    assert notes == [
        "2 groups with fewer than 10 customers were hidden.",
        "I planned 6 sub-questions but run at most 4 for one question, so I ran the first 4.",
        "I did not get \"Orders per customer\". That question took too long to run. Try a narrower question, for example a shorter time range or one brand or category.",
        "I could not get all the evidence I wanted: orders per customer; return rate.",
        "Some figures could not be verified against the data and were left out.",
        LIMITED_DATA_NOTE,
    ]


def test_a_gap_or_missing_evidence_makes_the_answer_partial_but_an_honest_no_data_does_not():
    assert not is_partial(state())
    assert is_partial(state(results={"q1": {"status": "blocked"}}))
    assert is_partial(state(evidence={"enough": False, "missing": ["x"]}))
    assert not is_partial(state(results={"q1": {"status": "empty"}, "q2": {"status": "ok"}}))


# the code-built answer -------------------------------------------------------------------------------------------------

def test_the_fallback_answer_is_a_table_of_the_guarded_figures_and_says_it_is_degraded():
    body = fallback_body(state(results={"q1": {"status": "ok", "guarded": GUARDED}, "q2": {"status": "failed"}}))
    assert body.startswith(DEGRADED_INTRO) and "**Revenue by state**" in body
    assert "| state | revenue |\n|---|---|\n| X | 10.5 |\n| Y | 20 |" in body
    assert fallback_body(state(results={"q1": {"status": "empty"}})) is None


def test_table_cells_are_readable_and_cannot_break_the_table():
    table = render_table({"columns": ["a", "b", "c", "d"], "rows": [{"a": 2026, "b": 12345, "c": "x|y\nz", "d": 1234567.891}], "total_rows": 60})
    assert "| 2026 | 12,345 | x/y z | 1,234,567.89 |" in table  # a year keeps no separator; a big number gets one
    assert "_Showing 1 of 60 rows._" in table


# in the graph ---------------------------------------------------------------------------------------------------------

@pytest.fixture
def rig(project, fake_bigquery):
    return make_rig(project, fake_bigquery=fake_bigquery)


def test_the_reading_the_scope_and_the_notices_are_in_the_answer_even_when_the_composer_leaves_them_out(rig):
    k = "__k_users"
    rig.client.add("GROUP BY u.state", df=pd.DataFrame({"state": ["X", "Y", "Z"], "revenue": [100.0, 200.0, 300.0], k: [50, 3, 4]}))
    sql = (
        "SELECT u.state, SUM(oi.sale_price) AS revenue FROM order_items oi JOIN users u ON u.id = oi.user_id "
        "WHERE oi.status NOT IN ('Cancelled', 'Returned') GROUP BY u.state"
    )
    rig.agent.deps.today = lambda: date(2026, 11, 20)  # a month after the data ends
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply(standalone="Revenue by state"))
    rig.gateway.queue(Step.SQL_WRITE, sql_reply(sql, "Revenue is the sum of item sale prices, all statuses."))
    rig.gateway.queue(Step.COMPOSER, composer_reply("State X earned 100."))  # says nothing of how it read the question, the scope or the hidden groups
    result = rig.ask("revenue by state")
    assert "Revenue is the sum of item sale prices, all statuses." in result.text
    assert "Scope: your brands: Brand A, Brand B" in result.text
    assert "2 groups with fewer than 10 customers were hidden." in result.text
    assert "The data ends on 2026-10-22" in result.text  # A5: the data ends before today, and the answer says so


def test_the_sql_writer_gets_the_resolved_dates_of_the_data_and_the_relevant_definitions_only(rig):
    rig.agent.deps.today = lambda: date(2026, 11, 20)
    rig.client.add("churn", df=pd.DataFrame({"churned": [12]}))
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply(standalone="How many customers churned last month?"))
    rig.gateway.queue(Step.SQL_WRITE, sql_reply("SELECT COUNT(DISTINCT oi.user_id) AS churned FROM order_items oi"))
    rig.gateway.queue(Step.COMPOSER, composer_reply("12 customers churned."))
    rig.client.add("COUNT(DISTINCT", df=pd.DataFrame({"churned": [12]}))
    rig.ask("How many customers churned last month?")
    prompt = rig.gateway.calls_for(Step.SQL_WRITE)[0]
    assert "last month: 2026-09-01 to 2026-09-30" in prompt.contents  # the data's calendar, not the clock
    assert "reference date (today, for the data): 2026-10-22" in prompt.contents
    assert "churn: a customer who placed no order in the 90 days" in prompt.contents and "last month: the previous calendar month" in prompt.contents
    assert "Never call CURRENT_DATE() or CURRENT_TIMESTAMP()" in prompt.system
    assert "dates and times: UTC" in prompt.contents  # "month" and "last month" are in the question
    assert "money: US dollars" not in prompt.contents  # nothing about revenue in this question: only the relevant definitions


# resolving relative words ------------------------------------------------------------------------------------------------

def test_relative_dates_are_resolved_against_the_reference_date():
    hints = date_hints(date(2026, 1, 15))
    assert hints["last month"] == "2025-12-01 to 2025-12-31" and hints["last quarter"] == "2025-10-01 to 2025-12-31"
    assert hints["this month so far"] == "2026-01-01 to 2026-01-15" and hints["last year"] == "2025-01-01 to 2025-12-31"
    assert hints["last 30 days"] == "2025-12-17 to 2026-01-15"
    leap = date_hints(date(2024, 3, 31))
    assert leap["last month"] == "2024-02-01 to 2024-02-29" and leap["this quarter so far"] == "2024-01-01 to 2024-03-31"


def test_a_widened_search_is_said_by_the_code_for_an_empty_and_for_an_ok_result():
    assert gap_line(sub(), {"status": "empty"}, widened=True) == 'No data was found for "Orders per customer", even after widening the search.'
    shown = state(attempts={"q1": {"broaden_used": 1, "broaden_ran": 1}, "q2": {"broaden_used": 0, "broaden_ran": 0}})
    assert 'My first query for "Revenue by state" found no data, so I widened the search.' in " ".join(notes_lines(shown))
    assert "widened" not in " ".join(notes_lines(state(attempts={"q1": {"broaden_used": 0, "broaden_ran": 0}})))
    # asked for is not done: a widening whose writer failed was never run, and the note must not say it was
    assert "widened" not in " ".join(notes_lines(state(attempts={"q1": {"broaden_used": 1, "broaden_ran": 0}})))


def test_groups_hidden_for_being_small_are_not_called_no_data_and_make_the_answer_partial():
    hidden = {"status": "empty", "guarded": {"suppressed_groups": 3, "rows": [], "notices": []}}
    line = gap_line(sub(), hidden, widened=True)
    assert "every group was too small to show" in line and "No data" not in line and "widening" not in line
    assert is_partial(state(results={"q1": hidden, "q2": {"status": "ok"}}))
