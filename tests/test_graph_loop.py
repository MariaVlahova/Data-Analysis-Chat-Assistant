"""The SQL loop (plan 07, decision 3.10 and 3.11, F30 to F34): repair, broaden, stop rules, typed failures, budget and chaos."""
import pandas as pd
import pytest

from src.errors import AgentError, ErrorKind
from src.llm.chaos import Chaos
from src.llm.fake import FakeClock
from src.llm.routing import Step
from tests.agent_support import REVENUE_SQL, composer_reply, guard_reply, make_rig, plan_reply, sql_reply

ROWS = pd.DataFrame({"brand": ["Brand A", "Brand B"], "revenue": [1234.5, 999.0]})
EMPTY = pd.DataFrame({"brand": pd.Series([], dtype=str), "revenue": pd.Series([], dtype=float)})
BAD_A = "SELECT oi.nopeColumnA FROM order_items oi"
BAD_B = "SELECT oi.nopeColumnB FROM order_items oi"
BAD_C = "SELECT oi.nopeColumnC FROM order_items oi"
BODY = "Brand A earned 1,234.5 and Brand B 999."


@pytest.fixture
def rig(project, fake_bigquery):
    fake_bigquery.add("revenue", df=ROWS)
    return make_rig(project, fake_bigquery=fake_bigquery)


def simple(rig, *, writes=(), repairs=(), last_repairs=(), compose=BODY):
    """Script the model replies of one simple question."""
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply(standalone="Revenue by brand"))
    rig.gateway.queue(Step.SQL_WRITE, *[sql_reply(sql) for sql in (writes or (REVENUE_SQL,))])
    rig.gateway.queue(Step.SQL_REPAIR, *[sql_reply(sql) for sql in repairs])
    rig.gateway.queue(Step.LAST_REPAIR, *[sql_reply(sql) for sql in last_repairs])
    if compose:
        rig.gateway.queue(Step.COMPOSER, composer_reply(compose))


# repair ---------------------------------------------------------------------------------------------------------------

def test_a_syntax_error_then_success_uses_one_repair(rig):
    simple(rig, writes=["SELECT FROM"], repairs=[REVENUE_SQL])
    result = rig.ask("revenue by brand")
    assert result.outcome == "answered", result
    assert rig.last_row["repairs"] == 1 and rig.calls(Step.SQL_REPAIR) == 1
    assert rig.last_row["llm_calls"] == 4  # input guard, writer, one repair, composer


def test_the_repair_prompt_has_the_failing_sql_the_error_and_the_exact_columns_never_blocked_ones(rig):
    simple(rig, writes=[BAD_A], repairs=[REVENUE_SQL])
    rig.ask("revenue by brand")
    repair = rig.gateway.calls_for(Step.SQL_REPAIR)[0].contents
    assert "nopeColumnA" in repair and "was not found" in repair  # the failing SQL and the normalized error
    assert "order_items(" in repair and "sale_price" in repair  # the exact column list of the allow-listed schema
    for blocked in ("email", "first_name", "last_name", "street_address", "latitude", "user_geom"):
        assert blocked not in repair


def test_three_failures_stop_at_two_repairs_and_the_last_repair_uses_the_last_repair_step(rig):
    simple(rig, writes=[BAD_A], repairs=[BAD_B], last_repairs=[BAD_C], compose="")
    result = rig.ask("revenue by brand")
    assert result.outcome == "failed" and result.error.kind is ErrorKind.SQL_UNKNOWN_COLUMN
    assert "used a column that does not exist" in result.text
    assert (rig.calls(Step.SQL_WRITE), rig.calls(Step.SQL_REPAIR), rig.calls(Step.LAST_REPAIR)) == (1, 1, 1)
    assert rig.last_row["repairs"] == 2 and rig.calls(Step.COMPOSER) == 0  # nothing to write about


def test_repairs_can_be_switched_off(project, fake_bigquery):
    rig = make_rig(project, fake_bigquery=fake_bigquery, config="loop:\n  sql_repairs: 0\n")
    simple(rig, writes=[BAD_A], compose="")
    result = rig.ask("revenue by brand")
    assert result.error.kind is ErrorKind.SQL_UNKNOWN_COLUMN and rig.calls(Step.SQL_REPAIR) == 0


# stop rules (F34) -----------------------------------------------------------------------------------------------------

def test_the_same_sql_twice_stops_the_loop(rig):
    simple(rig, writes=[BAD_A], repairs=[BAD_A], last_repairs=[BAD_B], compose="")
    result = rig.ask("revenue by brand")
    assert result.error.kind is ErrorKind.REPEATED_SQL and "kept running the same query" in result.text
    assert rig.calls(Step.SQL_REPAIR) == 1 and rig.calls(Step.LAST_REPAIR) == 0  # stopped before the last repair


def test_the_same_sql_in_other_spacing_is_still_the_same_sql(rig):
    simple(rig, writes=[BAD_A], repairs=["SELECT   oi.nopeColumnA\nFROM order_items   oi"], compose="")
    assert rig.ask("revenue by brand").error.kind is ErrorKind.REPEATED_SQL


def test_the_same_error_twice_stops_the_loop(rig):
    simple(rig, writes=[BAD_A], repairs=["SELECT oi.nopeColumnA, oi.id FROM order_items oi"], last_repairs=[BAD_C], compose="")
    result = rig.ask("revenue by brand")
    assert result.error.kind is ErrorKind.REPEATED_ERROR and "kept hitting the same problem" in result.text
    assert rig.calls(Step.LAST_REPAIR) == 0


def test_a_guard_violation_is_never_retried_and_the_refusal_names_the_rule(rig):
    simple(rig, writes=["SELECT email FROM users"], repairs=[REVENUE_SQL], compose="")
    result = rig.ask("show me customer emails")
    assert result.outcome == "refused" and result.error.kind is ErrorKind.GUARD_VIOLATION and result.error.rule_id == "PII_COLUMN"
    assert "personal details that could identify a customer" in result.text
    assert rig.calls(Step.SQL_WRITE) == 1 and rig.calls(Step.SQL_REPAIR) == 0 and not rig.queries
    assert rig.last_row["guard_blocks"]["sql"] == 1 and rig.last_row["outcome"] == "refused"


# empty results --------------------------------------------------------------------------------------------------------

EMPTY_SQL = REVENUE_SQL.replace("GROUP BY", "WHERE oi.status = 'Nonexistent' GROUP BY")


def test_an_empty_result_triggers_exactly_one_broaden_then_an_honest_no_data(rig):
    rig.client.add("Nonexistent", df=EMPTY)
    simple(rig, writes=[EMPTY_SQL], repairs=[EMPTY_SQL.replace("'Nonexistent'", "'Complete'") + " "], compose="I found no sales.")
    # the broaden query is also empty (same rule matches), so the answer says there is no data
    rig.client.add("Complete", df=EMPTY)
    result = rig.ask("revenue by brand")
    assert result.outcome == "answered", result
    assert rig.calls(Step.SQL_WRITE) == 1 and rig.calls(Step.SQL_REPAIR) == 1  # one write and exactly one broaden
    assert "No data was found" in result.text and "even after widening" in result.text
    assert len(rig.queries) == 2


def test_a_broaden_that_finds_rows_gives_an_ok_answer_that_says_what_was_widened(rig):
    rig.client.add("Nonexistent", df=EMPTY)
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply(standalone="Revenue by brand"))
    rig.gateway.queue(Step.SQL_WRITE, sql_reply(EMPTY_SQL, "Revenue counts items with the status Nonexistent."))
    rig.gateway.queue(Step.SQL_REPAIR, sql_reply(REVENUE_SQL, "I widened it to every status."))
    rig.gateway.queue(Step.COMPOSER, composer_reply(BODY))
    result = rig.ask("revenue by brand")
    assert result.outcome == "answered" and "I widened it to every status." in result.text
    assert "No data was found" not in result.text
    # the code says it too, so the user is told even when the model's reading does not mention it
    assert 'My first query for "Revenue by brand" found no data, so I widened the search.' in result.text


def test_a_broaden_that_repeats_the_empty_query_ends_in_no_data_without_another_run(rig):
    rig.client.add("Nonexistent", df=EMPTY)
    simple(rig, writes=[EMPTY_SQL], repairs=[EMPTY_SQL], compose="I found no sales.")
    result = rig.ask("revenue by brand")
    assert result.outcome == "answered" and "No data was found" in result.text
    assert len(rig.queries) == 1  # the same SQL was not run twice


def test_groups_hidden_for_being_small_are_not_broadened(rig):
    """Widening would not help: the rows exist, and the Result Guard hid them (S7)."""
    k = "__k_users"
    rig.client.add("GROUP BY u.state", df=pd.DataFrame({"state": ["X", "Y"], "revenue": [10.0, 20.0], k: [3, 4]}))
    sql = "SELECT u.state, SUM(oi.sale_price) AS revenue FROM order_items oi JOIN users u ON u.id = oi.user_id GROUP BY u.state"
    simple(rig, writes=[sql], repairs=[sql + " LIMIT 5"], compose="Nothing to show.")
    result = rig.ask("revenue by state")
    assert rig.calls(Step.SQL_REPAIR) == 0 and "2 groups with fewer than 10 customers were hidden" in result.text


# too expensive, too slow, outages -------------------------------------------------------------------------------------

def test_a_query_that_is_too_expensive_asks_the_user_to_narrow(project, fake_bigquery):
    fake_bigquery.add("revenue", df=ROWS, bytes_processed=2_000_000_000)  # over the 512 MiB cap per query
    rig = make_rig(project, fake_bigquery=fake_bigquery)
    simple(rig, compose="")
    result = rig.ask("revenue by brand")
    assert result.outcome == "failed" and result.error.kind is ErrorKind.QUERY_TOO_EXPENSIVE
    assert "narrower question" in result.text and not rig.queries  # no job started


def test_a_query_that_is_too_slow_asks_the_user_to_narrow(project, fake_bigquery):
    fake_bigquery.add("revenue", df=ROWS, result_errors=[TimeoutError("slow")])
    rig = make_rig(project, fake_bigquery=fake_bigquery)
    simple(rig, compose="")
    result = rig.ask("revenue by brand")
    assert result.error.kind is ErrorKind.QUERY_TIMEOUT and "took too long to run" in result.text and "narrower question" in result.text


def test_an_outage_ends_in_an_honest_message_with_no_loop(rig):
    rig.client.down = True
    simple(rig, compose="")
    result = rig.ask("revenue by brand")
    assert result.outcome == "failed" and result.error.kind is ErrorKind.BQ_UNAVAILABLE
    assert "data warehouse is not responding" in result.text
    assert rig.calls(Step.SQL_WRITE) == 1 and rig.calls(Step.SQL_REPAIR) == 0


def test_a_failing_sql_writer_ends_in_an_honest_message(rig):
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply(standalone="Revenue by brand"))
    rig.gateway.queue(Step.SQL_WRITE, AgentError(ErrorKind.LLM_UNAVAILABLE))
    result = rig.ask("revenue by brand")
    assert result.outcome == "failed" and result.error.kind is ErrorKind.LLM_UNAVAILABLE
    assert "language model is not responding" in result.text


def test_a_failed_sub_question_does_not_stop_the_others(rig):
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply("why_question", "Why?"))
    rig.gateway.queue(Step.PLANNER, plan_reply("First", "Second"))
    rig.gateway.queue(Step.SQL_WRITE, AgentError(ErrorKind.LLM_UNAVAILABLE), sql_reply())
    rig.gateway.queue(Step.EVIDENCE_CHECK, {"enough": True, "missing": []})
    rig.gateway.queue(Step.COMPOSER, composer_reply(BODY))
    result = rig.ask("Why?")
    assert result.outcome == "partial", result
    assert 'I could not get "First"' in result.text and "Limited data" in result.text and "Brand A earned" in result.text


# budget and time (S3, NFR3, NFR4) -------------------------------------------------------------------------------------

def test_a_turn_that_has_no_calls_left_for_the_sql_stops_with_an_honest_message(project, fake_bigquery):
    rig = make_rig(project, fake_bigquery=fake_bigquery, config="budget:\n  simple: {calls: 2, seconds: 60}\n")
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply(standalone="Revenue by brand"))
    result = rig.ask("revenue by brand")  # the last call is kept for the answer, so no SQL is written
    assert result.outcome == "failed" and result.error.kind is ErrorKind.BUDGET_EXHAUSTED
    assert "reached the limit of work" in result.text and rig.calls(Step.SQL_WRITE) == 0


def test_a_why_turn_that_runs_out_of_calls_still_answers_with_what_it_has(project, fake_bigquery):
    fake_bigquery.add("revenue", df=ROWS)
    rig = make_rig(project, fake_bigquery=fake_bigquery, config="budget:\n  simple: {calls: 4, seconds: 60}\n  why: {calls: 5, seconds: 60}\n")
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply("why_question", "Why?"))
    rig.gateway.queue(Step.PLANNER, plan_reply("One", "Two", "Three", "Four"))
    rig.gateway.queue(Step.SQL_WRITE, sql_reply(), sql_reply())
    rig.gateway.queue(Step.COMPOSER, composer_reply(BODY))
    result = rig.ask("Why?")
    assert result.outcome == "partial", result
    assert rig.last_row["llm_calls"] == 5  # exactly the limit: the answer's call was kept
    assert result.text.count("I did not get to") == 2 and "Limited data" in result.text
    assert rig.calls(Step.EVIDENCE_CHECK) == 0  # no call to spare: the code check stands alone


def test_a_why_turn_stays_within_its_budget_even_when_everything_goes_wrong(rig):
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply("why_question", "Why?"))
    rig.gateway.queue(Step.PLANNER, plan_reply("A", "B", "C", "D"))
    for step in (Step.SQL_WRITE, Step.SQL_REPAIR, Step.LAST_REPAIR):
        rig.gateway.always(step, sql_reply("SELECT oi.nopeColumnQ FROM order_items oi"))
    rig.gateway.always(Step.COMPOSER, composer_reply("I could not get the data."))
    rig.gateway.always(Step.EVIDENCE_CHECK, {"enough": True, "missing": []})
    rig.ask("Why?")
    assert rig.last_row["llm_calls"] <= rig.settings.budget.why.calls == 20


def test_when_the_composer_is_down_the_answer_is_built_from_the_guarded_figures(rig):
    simple(rig, compose="")
    rig.gateway.queue(Step.COMPOSER, AgentError(ErrorKind.LLM_UNAVAILABLE))
    result = rig.ask("revenue by brand")
    assert result.outcome == "partial" and "here are the figures I found, straight from the data" in result.text
    assert "| Brand A | 1234.5 |" in result.text and "Limited data" in result.text
    assert rig.last_row["error_kinds"] == ["LLM_UNAVAILABLE"]


def test_when_the_time_is_up_after_one_good_result_the_answer_is_built_from_it(project, fake_bigquery):
    fake_bigquery.add("revenue", df=ROWS)
    clock = FakeClock()
    rig = make_rig(project, fake_bigquery=fake_bigquery, clock=clock)
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply("why_question", "Why?"))
    rig.gateway.queue(Step.PLANNER, plan_reply("One", "Two"))
    # the first query is fine; the minute runs out while the second one is being written
    rig.gateway.queue(Step.SQL_WRITE, sql_reply(), lambda call: (clock.advance(61), sql_reply())[1])
    result = rig.ask("Why?")
    assert result.outcome == "partial", result
    assert "| Brand A | 1234.5 |" in result.text and 'I did not get to "Two"' in result.text
    assert "TURN_TIMEOUT" in rig.last_row["error_kinds"] and "straight from the data" in result.text  # no time left to ask the Composer
    assert rig.last_row["llm_calls"] == 4  # input guard, planner, two writers: the Composer call was refused, not counted


def test_the_time_so_far_is_shown_in_the_progress_after_ten_seconds(project, fake_bigquery):
    from src.agent.deps import TurnRuntime
    from src.llm.budget import TurnBudget

    clock, lines = FakeClock(), []
    runtime = TurnRuntime(TurnBudget(8, 60, 10**9, clock), lines.append)
    runtime.say("Querying...")
    clock.advance(12)
    runtime.say("Writing the answer...")
    assert lines == ["Querying...", "Writing the answer... (12 s)"]


# chaos (F37) ----------------------------------------------------------------------------------------------------------

def test_a_corrupted_sql_is_repaired_through_the_same_loop(project, fake_bigquery):
    fake_bigquery.add("revenue", df=ROWS)
    chaos = Chaos.scripted().corrupt_next_sql("syntax")
    rig = make_rig(project, fake_bigquery=fake_bigquery, chaos=chaos)
    simple(rig, writes=[REVENUE_SQL], repairs=[REVENUE_SQL])
    result = rig.ask("revenue by brand")
    assert result.outcome == "answered" and chaos.injected == ["sql:syntax"]
    assert rig.last_row["repairs"] == 1 and rig.calls(Step.SQL_REPAIR) == 1
    # the corrupted text went through the SQL Guard like any other: it was refused there, never run
    assert len(rig.queries) == 1


def test_a_bigquery_fault_is_retried_by_the_runner_and_the_turn_succeeds(project, fake_bigquery):
    fake_bigquery.add("revenue", df=ROWS)
    chaos = Chaos.scripted().fail_bq(times=2)
    rig = make_rig(project, fake_bigquery=fake_bigquery, chaos=chaos)
    simple(rig)
    assert rig.ask("revenue by brand").outcome == "answered" and chaos.injected == ["bq:unavailable"] * 2


def test_each_turn_tells_the_chaos_engine_it_began(project, fake_bigquery):
    fake_bigquery.add("revenue", df=ROWS)
    chaos = Chaos.scripted()
    begun = []
    chaos.begin_turn = lambda: begun.append(1)
    rig = make_rig(project, fake_bigquery=fake_bigquery, chaos=chaos)
    simple(rig)
    rig.ask("revenue by brand")
    simple(rig)
    rig.ask("revenue by brand")
    assert len(begun) == 2


# a failure of the widening query leaves the honest "no data" in place -----------------------------------------------------

def broaden_fails(rig, reply):
    rig.client.add("Nonexistent", df=EMPTY)
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply(standalone="Revenue by brand"))
    rig.gateway.queue(Step.SQL_WRITE, sql_reply(EMPTY_SQL, "Revenue counts items with the status Nonexistent."))
    rig.gateway.queue(Step.SQL_REPAIR, reply, sql_reply(BAD_B))  # the second reply is only used when the widening has to be repaired
    rig.gateway.queue(Step.LAST_REPAIR, sql_reply(BAD_C))
    rig.gateway.queue(Step.COMPOSER, composer_reply("I found no sales."))
    return rig.ask("revenue by brand")


@pytest.mark.parametrize("label,widening", [
    ("a refused query", sql_reply("SELECT email FROM users")),
    ("a writer that is down", AgentError(ErrorKind.LLM_UNAVAILABLE)),
    ("a query that never gets fixed", sql_reply(BAD_A)),
])
def test_when_the_widening_goes_wrong_the_answer_is_still_the_honest_no_data(rig, label, widening):
    result = broaden_fails(rig, widening)
    assert result.outcome == "answered", (label, result)
    assert 'No data was found for "Revenue by brand"' in result.text, label
    assert "personal details" not in result.text and "not responding" not in result.text and "column that does not exist" not in result.text
    assert "even after widening" not in result.text, label  # no wider query ran, and the answer does not claim one did
    assert "Revenue counts items with the status Nonexistent." in result.text  # the reading of the question that found nothing


def test_a_widening_query_that_is_too_expensive_is_still_no_data(project, fake_bigquery):
    fake_bigquery.add("revenue", df=ROWS, bytes_processed=2_000_000_000)
    fake_bigquery.add("Nonexistent", df=EMPTY)
    rig = make_rig(project, fake_bigquery=fake_bigquery)
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply(standalone="Revenue by brand"))
    rig.gateway.queue(Step.SQL_WRITE, sql_reply(EMPTY_SQL))
    rig.gateway.queue(Step.SQL_REPAIR, sql_reply(REVENUE_SQL))  # the wider query would scan far too much
    rig.gateway.queue(Step.COMPOSER, composer_reply("I found no sales."))
    result = rig.ask("revenue by brand")
    assert result.outcome == "answered" and 'No data was found for "Revenue by brand"' in result.text and "narrower question" not in result.text
    assert "even after widening" not in result.text  # the wider query was only costed, never run


def test_a_refusal_of_the_widening_does_not_make_a_two_part_turn_refuse_the_first_part(rig):
    rig.client.add("Nonexistent", df=EMPTY)
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply("why_question", "Why?"))
    rig.gateway.queue(Step.PLANNER, plan_reply("Revenue of odd orders", "Revenue by brand"))
    rig.gateway.queue(Step.SQL_WRITE, sql_reply(EMPTY_SQL), sql_reply())
    rig.gateway.queue(Step.SQL_REPAIR, sql_reply("SELECT email FROM users"))
    rig.gateway.queue(Step.EVIDENCE_CHECK, {"enough": True, "missing": []})
    rig.gateway.queue(Step.COMPOSER, composer_reply(BODY))
    result = rig.ask("Why?")
    assert "I did not run" not in result.text and "personal details" not in result.text
    assert 'No data was found for "Revenue of odd orders"' in result.text and "Brand A earned" in result.text


# the same error is the same mistake, not a similar name (review finding) ----------------------------------------------------------------

def test_two_unknown_columns_that_differ_only_in_a_number_are_two_mistakes_and_the_last_repair_is_made(rig):
    simple(rig, writes=["SELECT oi.col1 FROM order_items oi"], repairs=["SELECT oi.col2 FROM order_items oi"], last_repairs=[REVENUE_SQL])
    result = rig.ask("revenue by brand")
    assert result.outcome == "answered", result
    assert rig.last_row["repairs"] == 2 and rig.calls(Step.LAST_REPAIR) == 1


def test_the_signature_ignores_where_in_the_sql_and_what_is_quoted_but_not_the_name():
    from src.agent.nodes.sql_loop import error_signature

    kind = ErrorKind.SQL_UNKNOWN_COLUMN
    assert error_signature(kind, "Unrecognized name: revenue_2024 at [1:8]") == error_signature(kind, "Unrecognized name: revenue_2024 at [3:15]")
    assert error_signature(kind, "Unrecognized name: revenue_2024 at [1:8]") != error_signature(kind, "Unrecognized name: revenue_2025 at [1:8]")
    assert error_signature(kind, 'column "a" not found at line 1') == error_signature(kind, 'column "b" not found at line 7')
    assert error_signature(ErrorKind.SQL_SYNTAX, "x") != error_signature(kind, "x")


# a guard that fails on the widening is not worked around (review finding) --------------------------------------------------------------------

def test_a_guard_that_fails_on_the_widened_result_ends_the_turn_and_shows_nothing_of_it(rig, monkeypatch):
    """CLAUDE.md: a guard that fails raises and returns nothing; never catch it to show what we have. The honest "no data" of the first
    query stays for the failures of the widening that are not a guard's (a refusal, a cost, a writer that is down)."""
    rig.client.add("Nonexistent", df=EMPTY)
    real, calls = rig.agent.deps.result_guard.apply, []

    def apply(df, gq, total_rows):
        calls.append(1)
        if len(calls) > 1:
            raise AgentError(ErrorKind.RESULT_GUARD_FAILED)
        return real(df, gq, total_rows)

    monkeypatch.setattr(rig.agent.deps.result_guard, "apply", apply)
    simple(rig, writes=[EMPTY_SQL], repairs=[REVENUE_SQL], compose="")
    result = rig.ask("revenue by brand")
    assert len(calls) == 2 and result.outcome == "failed" and result.error.kind is ErrorKind.RESULT_GUARD_FAILED
    assert "No data was found" not in result.text and "Brand A" not in result.text
