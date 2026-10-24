"""What a turn leaves behind for the next one (plan 07): the history, the title of "save that", and the messages kept in state."""
import pandas as pd
import pytest
from google.api_core import exceptions as api_exc

from src.agent.envelope import answer_body
from src.data_access.errors_map import SETUP_MESSAGE as BQ_SETUP_MESSAGE
from src.errors import AgentError, ErrorKind
from src.llm.gateway import SETUP_MESSAGE as LLM_SETUP_MESSAGE
from src.llm.routing import Step
from tests.agent_support import REVENUE_SQL, composer_reply, guard_reply, make_rig, plan_reply, sql_reply

ROWS = pd.DataFrame({"brand": ["Brand A", "Brand B"], "revenue": [1234.5, 999.0]})
BODY = "Brand A earned 1,234.5 and Brand B 999."
THREAD = lambda rig: {"configurable": {"thread_id": rig.conversation_id}}  # noqa: E731


@pytest.fixture
def rig(project, fake_bigquery):
    fake_bigquery.add("revenue", df=ROWS)
    return make_rig(project, fake_bigquery=fake_bigquery)


def state_of(rig):
    return rig.agent.graph.get_state(THREAD(rig)).values


def answer(rig, question="revenue by brand", *, standalone="Revenue by brand", interpretation="Revenue is the sum of item sale prices.", body=BODY, title=""):
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply(standalone=standalone))
    rig.gateway.queue(Step.SQL_WRITE, sql_reply(REVENUE_SQL, interpretation))
    rig.gateway.queue(Step.COMPOSER, composer_reply(body, title))
    return rig.ask(question)


# the history keeps the body, not the envelope ----------------------------------------------------------------------------------

def test_the_history_keeps_what_the_answer_said_not_the_reading_the_scope_and_the_notes(rig):
    long_reading = "I read revenue as the sum of item sale prices, without cancelled or returned items. " * 3
    result = answer(rig, interpretation=long_reading)
    assert result.text.startswith("**How I read your question**") and "*Scope: your brands" in result.text  # the envelope is there for the user
    kept = state_of(rig)["history"][-1]["assistant"]
    assert kept == BODY  # and not in what the next turn's Input Guard reads
    assert "How I read" not in kept and "Scope" not in kept and "Notes" not in kept


def test_a_follow_up_is_rewritten_from_the_body_of_the_last_answer(rig):
    answer(rig, interpretation="Revenue is the sum of item sale prices. " * 6)
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply(standalone="Revenue by brand for last year"))
    rig.gateway.queue(Step.SQL_WRITE, sql_reply())
    rig.gateway.queue(Step.COMPOSER, composer_reply(BODY))
    rig.ask("and for last year?")
    second_guard_prompt = rig.gateway.calls_for(Step.INPUT_GUARD)[1].contents
    assert "Brand A earned 1,234.5" in second_guard_prompt and "How I read your question" not in second_guard_prompt


def test_answer_body_strips_only_the_envelope():
    text = "**How I read your question**\n- a\n- b\n\n*Scope: your brands: X*\n\n## Title\n\nPart one.\n\nPart two.\n\n**Notes**\n- n"
    assert answer_body(text) == "## Title\n\nPart one.\n\nPart two."
    assert answer_body("Deleted 1 report.") == "Deleted 1 report."  # a tool's message has no envelope
    assert answer_body("**Notes**\n- only") == "**Notes**\n- only"  # never ends up with nothing


# the title of a saved answer ----------------------------------------------------------------------------------------------------

def test_a_saved_answer_without_a_title_is_named_after_the_question(rig):
    answer(rig, standalone="Revenue by brand")
    assert state_of(rig)["last_answer"]["title"] == "Revenue by brand"


def test_a_question_with_personal_data_never_becomes_the_title(rig):
    """The question never went through the Output Guard. The title is stored and listed again, so it is checked here."""
    answer(rig, "revenue for jane.doe@example.com", standalone="Revenue by brand for jane.doe@example.com")
    assert state_of(rig)["last_answer"]["title"] == "Saved answer"
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply("save_report"))
    saved = rig.ask("save that")
    assert saved.outcome == "action_done" and 'Saved "Saved answer"' in saved.text  # not held back by the Output Guard
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply("list_reports"))
    listed = rig.ask("list my reports")
    assert listed.outcome == "action_done" and "1. Saved answer" in listed.text and "jane.doe" not in listed.text


def test_the_phone_number_is_found_before_the_title_is_cut(rig):
    long_start = "Revenue by brand and by month for every product category we sell, " * 2
    answer(rig, standalone=long_start + "call +1 415 555 0132")  # the number lies past the cut
    assert state_of(rig)["last_answer"]["title"] == "Saved answer"


# the message of a failure is the one the error carried ----------------------------------------------------------------------------

def test_a_setup_problem_with_the_data_warehouse_is_not_told_as_a_transient_one(project, fake_bigquery):
    fake_bigquery.add("revenue", df=ROWS, result_errors=[api_exc.Forbidden("Access Denied: User does not have bigquery.jobs.create permission")])
    rig = make_rig(project, fake_bigquery=fake_bigquery)
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply(standalone="Revenue by brand"))
    rig.gateway.queue(Step.SQL_WRITE, sql_reply())
    result = rig.ask("revenue by brand")
    assert result.outcome == "failed" and result.error.kind is ErrorKind.BQ_UNAVAILABLE
    assert result.text == BQ_SETUP_MESSAGE and "try again in a minute" not in result.text


def test_a_partial_answer_names_the_setup_problem_of_the_part_that_failed(project, fake_bigquery):
    fake_bigquery.add("revenue", df=ROWS, result_errors=[api_exc.Forbidden("Access Denied: no permission")])
    rig = make_rig(project, fake_bigquery=fake_bigquery)
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply("why_question", "Why?"))
    rig.gateway.queue(Step.PLANNER, plan_reply("First", "Second"))
    rig.gateway.queue(Step.SQL_WRITE, sql_reply(), sql_reply())
    rig.gateway.queue(Step.EVIDENCE_CHECK, {"enough": True, "missing": []})
    rig.gateway.queue(Step.COMPOSER, composer_reply(BODY))
    result = rig.ask("Why?")
    assert result.outcome == "partial" and 'I could not get "First". ' + BQ_SETUP_MESSAGE in result.text
    assert "not responding" not in result.text


def test_a_transient_failure_still_gets_the_standard_words(rig):
    rig.client.down = True
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply(standalone="Revenue by brand"))
    rig.gateway.queue(Step.SQL_WRITE, sql_reply())
    assert "data warehouse is not responding" in rig.ask("revenue by brand").text


# the Input Guard keeps a setup message ---------------------------------------------------------------------------------------------

def test_a_missing_model_key_is_told_as_a_setup_problem_not_as_try_again(rig):
    rig.gateway.queue(Step.INPUT_GUARD, AgentError(ErrorKind.LLM_UNAVAILABLE, LLM_SETUP_MESSAGE, detail="GEMINI_API_KEY is not set", retryable=False))
    result = rig.ask("revenue by brand")
    assert result.outcome == "failed" and result.error.kind is ErrorKind.INPUT_GUARD_FAILED
    assert result.text == LLM_SETUP_MESSAGE and "try again" not in result.text.lower() and not result.error.retryable


def test_a_passing_model_failure_is_still_the_standard_input_guard_message(rig):
    rig.gateway.queue(Step.INPUT_GUARD, AgentError(ErrorKind.LLM_UNAVAILABLE))
    result = rig.ask("revenue by brand")
    assert result.error.kind is ErrorKind.INPUT_GUARD_FAILED and "I couldn't safely check your message" in result.text


# the SQL kept in state is masked before it is cut ---------------------------------------------------------------------------------

def test_a_literal_in_the_kept_sql_is_masked_before_the_text_is_cut(rig):
    """An e-mail typed by the user can end up in a SQL literal. Cut first, it could leave half an address that no pattern matches."""
    from src.agent.nodes.sql_loop import MAX_SQL_CHARS
    from tests.guard_support import ALICE

    def with_padding(pad):
        return REVENUE_SQL.replace("GROUP BY", f"WHERE oi.status != '{'x' * pad}jane.doe@example.com' GROUP BY")

    guard = rig.agent.deps.sql_guard
    at = guard.check(with_padding(0), ALICE).sql.index("jane.doe")
    sql = with_padding(MAX_SQL_CHARS - 5 - at)  # the address starts 5 characters before the cut: "jane." is what a cut would leave
    assert guard.check(sql, ALICE).sql.index("jane.doe") == MAX_SQL_CHARS - 5
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply(standalone="Revenue by brand"))
    rig.gateway.queue(Step.SQL_WRITE, sql_reply(sql))
    rig.gateway.queue(Step.COMPOSER, composer_reply(BODY))
    rig.ask("revenue by brand")
    kept = state_of(rig)["results"]["q1"]["final_sql"]
    assert kept and "jane" not in kept and len(kept) <= MAX_SQL_CHARS


# a period the code resolved may be restated by the Composer ---------------------------------------------------------------------------

def test_a_period_the_code_resolved_may_be_restated_in_the_body(rig):
    from src.agent.prompts.locked_rules import date_hints

    deps = rig.agent.deps
    start, end = date_hints(deps.catalog.reference_date(deps.today()))["last month"].split(" to ")
    result = answer(rig, "revenue by brand last month?", standalone="Revenue by brand last month", body=f"Brand A earned 1,234.5 between {start} and {end}. Brand B earned 999.")
    assert f"between {start} and {end}" in result.text, result.text
    assert result.outcome == "answered" and "could not be verified" not in result.text


def test_a_date_nobody_resolved_is_still_removed(rig):
    result = answer(rig, "revenue by brand last month?", standalone="Revenue by brand last month", body="Brand A earned 1,234.5 in March 2011. Brand B earned 999.")
    assert "March 2011" not in result.text and "Brand B earned 999" in result.text and "could not be verified" in result.text
