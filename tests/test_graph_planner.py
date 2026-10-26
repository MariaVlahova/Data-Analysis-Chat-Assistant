"""The Planner and the Evidence Check (plan 07, F3, decision 3.10): at most 4 sub-questions, a bounded replan (off by default),
and a partial answer that says what is missing."""
import pandas as pd
import pytest

from src.agent.nodes.planner import DateRangeOut, PlanOut, SubQuestionOut, normalize_plan
from src.errors import AgentError, ErrorKind
from src.guards.allowlist import load_allowlist
from src.config import Settings
from src.llm.routing import Step
from tests.agent_support import REVENUE_SQL, composer_reply, guard_reply, make_rig, plan_reply, sql_reply
from tests.guard_support import REPO_ROOT

ROWS = pd.DataFrame({"brand": ["Brand A", "Brand B"], "revenue": [1234.5, 999.0]})
BODY = "Brand A earned 1,234.5 and Brand B 999."
ALLOWLIST = load_allowlist(Settings(), REPO_ROOT / "schema_allowlist.yaml")


def sub(text, id="", depends=(), tables=()):
    return SubQuestionOut(id=id, text=text, depends_on=list(depends), tables=list(tables))


# the code that enforces the plan --------------------------------------------------------------------------------------

def test_the_plan_is_cut_to_four_sub_questions_and_the_cut_is_said():
    out = PlanOut(subquestions=[sub(f"Question {n}") for n in range(6)])
    plan = normalize_plan(out, allowlist=ALLOWLIST, limit=4)
    assert [s["id"] for s in plan["subquestions"]] == ["q1", "q2", "q3", "q4"]
    assert "run at most 4" in plan["notices"][0]


def test_ids_are_made_unique_and_a_dependency_can_only_point_backwards():
    out = PlanOut(subquestions=[
        sub("first", "a", depends=["b", "zzz"]),  # forward and unknown: dropped
        sub("second", "a", depends=["a"]),  # the same id again: still a valid earlier sub-question
        sub("third", "c", depends=["c", "a"]),  # itself: dropped
    ])
    plan = normalize_plan(out, allowlist=ALLOWLIST, limit=4)
    assert [s["id"] for s in plan["subquestions"]] == ["q1", "q2", "q3"]
    assert [s["depends_on"] for s in plan["subquestions"]] == [[], ["q1"], ["q1"]]  # no cycle is possible


def test_a_table_must_be_in_the_allow_list():
    plan = normalize_plan(PlanOut(subquestions=[sub("a", tables=["Order_Items", "secrets", "users"]), sub("b", tables=["nothing"])]), allowlist=ALLOWLIST, limit=4)
    assert plan["subquestions"][0]["tables"] == ["order_items", "users"]
    assert plan["subquestions"][1]["tables"] == ["order_items", "orders", "products", "users"]  # no usable table: the small whole schema


@pytest.mark.parametrize("start,end", [("2026-02-30", "2026-03-01"), ("2026-05-01", "2026-04-01"), ("last month", "today")])
def test_a_date_range_must_be_two_real_dates_in_order(start, end):
    plan = normalize_plan(PlanOut(subquestions=[sub("a")], date_range=DateRangeOut(start=start, end=end)), allowlist=ALLOWLIST, limit=4)
    assert plan["date_range"] is None


def test_a_valid_date_range_is_kept_and_an_empty_plan_falls_back_to_the_question():
    plan = normalize_plan(PlanOut(subquestions=[sub("a")], date_range=DateRangeOut(start="2026-09-01", end="2026-09-30", label="last month")), allowlist=ALLOWLIST, limit=4)
    assert plan["date_range"] == {"start": "2026-09-01", "end": "2026-09-30", "label": "last month"}
    empty = normalize_plan(PlanOut(subquestions=[sub("  ")]), allowlist=ALLOWLIST, limit=4, standalone="Why did revenue fall?")
    assert [s["text"] for s in empty["subquestions"]] == ["Why did revenue fall?"]


# the planner inside the graph -----------------------------------------------------------------------------------------

@pytest.fixture
def rig(project, fake_bigquery):
    fake_bigquery.add("revenue", df=ROWS)
    return make_rig(project, fake_bigquery=fake_bigquery)


def why(rig, *, plan=None, sql_replies=None, evidence=None, compose=BODY, question="Why did revenue change?"):
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply("why_question", question))
    rig.gateway.queue(Step.PLANNER, *(plan or [plan_reply("Revenue by brand")]))
    rig.gateway.queue(Step.SQL_WRITE, *(sql_replies or [sql_reply()]))
    rig.gateway.queue(Step.EVIDENCE_CHECK, *(evidence or [{"enough": True, "missing": []}]))
    if compose:
        rig.gateway.queue(Step.COMPOSER, composer_reply(compose, "Revenue"))


def test_the_planner_sees_names_dates_and_allowed_tables_but_no_identity(rig):
    why(rig)
    rig.ask("Why did revenue change?")
    call = rig.gateway.calls_for(Step.PLANNER)[0]
    text = call.system + call.contents
    assert "order_items" in call.contents and "sale_price" in call.contents  # table and column names with descriptions
    assert "last month: " in call.contents and "The data's latest date: 2026-10-22" in call.contents
    for private in ("Brand A", "Brand B", "alice", "Alice", "hash-alice"):
        assert private not in text
    assert "At most 4 sub-questions" in call.system


def test_a_plan_with_more_than_four_questions_runs_four_and_the_answer_says_so(rig):
    why(rig, plan=[plan_reply(*[f"Q{n}" for n in range(1, 7)])], sql_replies=[sql_reply()] * 4)
    result = rig.ask("Why did revenue change?")
    assert len(rig.gateway.calls_for(Step.SQL_WRITE)) == 4 and rig.last_row["subquestions"] == 4
    assert "run at most 4" in result.text


def test_the_plans_assumptions_and_date_range_are_stated_in_the_answer(rig):
    why(rig, plan=[plan_reply("Revenue by brand", assumptions=["Recent means the last 90 days."], date_range={"start": "2026-07-24", "end": "2026-10-22", "label": "last 90 days"})])
    result = rig.ask("Why did revenue change?")
    assert "Recent means the last 90 days." in result.text and "Period: 2026-07-24 to 2026-10-22 (last 90 days)." in result.text


def test_a_planner_that_cannot_plan_falls_back_to_one_question_and_says_so(rig):
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply("why_question", "Why did revenue change?"))
    rig.gateway.queue(Step.PLANNER, AgentError(ErrorKind.LLM_UNAVAILABLE))
    rig.gateway.queue(Step.SQL_WRITE, sql_reply())
    rig.gateway.queue(Step.EVIDENCE_CHECK, {"enough": True, "missing": []})
    rig.gateway.queue(Step.COMPOSER, composer_reply(BODY))
    result = rig.ask("Why did revenue change?")
    assert result.outcome == "answered" and "answered it as one question" in result.text and rig.last_row["subquestions"] == 1


def test_earlier_results_reach_a_sub_question_only_through_a_declared_dependency(rig):
    declared = {"subquestions": [
        {"id": "a", "text": "Revenue by brand", "depends_on": [], "tables": []},
        {"id": "b", "text": "Items for the top brand", "depends_on": ["a"], "tables": []},
    ], "assumptions": [], "date_range": None}
    undeclared = {"subquestions": [{**declared["subquestions"][0]}, {**declared["subquestions"][1], "depends_on": []}], "assumptions": [], "date_range": None}
    for plan, expected in ((declared, True), (undeclared, False)):
        rig.gateway.calls.clear()
        why(rig, plan=[plan], sql_replies=[sql_reply(), sql_reply()])
        rig.ask("Why did revenue change?")
        second = rig.gateway.calls_for(Step.SQL_WRITE)[1].contents
        assert ("1234.5" in second) is expected  # the Result Guard's rows, and only on a declared dependency
        assert "Brand B" not in rig.gateway.calls_for(Step.SQL_WRITE)[0].contents  # the first one has no earlier results


# the Evidence Check ---------------------------------------------------------------------------------------------------

def test_when_every_sub_question_failed_the_turn_ends_in_compose_failure(rig):
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply("why_question", "Why?"))
    rig.gateway.queue(Step.PLANNER, plan_reply("A", "B"))
    rig.gateway.queue(Step.SQL_WRITE, AgentError(ErrorKind.LLM_UNAVAILABLE), AgentError(ErrorKind.LLM_UNAVAILABLE))
    result = rig.ask("Why did my revenue change?")
    assert result.outcome == "failed" and "compose_failure" in rig.span_names()
    assert rig.calls(Step.EVIDENCE_CHECK) == 0 and rig.calls(Step.COMPOSER) == 0  # nothing to ask a model about


def test_with_no_replans_left_a_missing_list_gives_a_partial_answer_that_says_what_is_missing(rig):
    why(rig, evidence=[{"enough": False, "missing": ["orders per customer", "return rate"]}])
    result = rig.ask("Why did revenue change?")
    assert result.outcome == "partial", result  # max_replans is 0 in the prototype
    assert rig.calls(Step.PLANNER) == 1 and rig.last_row["replans"] == 0
    assert "I could not get all the evidence I wanted: orders per customer; return rate." in result.text and "Limited data" in result.text


def test_with_one_replan_the_planner_runs_once_more_and_the_new_questions_run(project, fake_bigquery):
    fake_bigquery.add("revenue", df=ROWS)
    rig = make_rig(project, fake_bigquery=fake_bigquery, config="loop:\n  max_replans: 1\n")
    why(rig, plan=[plan_reply("Revenue by brand"), plan_reply("Orders per customer")], sql_replies=[sql_reply(), sql_reply(REVENUE_SQL + " LIMIT 5")],
        evidence=[{"enough": False, "missing": ["orders per customer"]}, {"enough": False, "missing": ["still more"]}])
    result = rig.ask("Why did revenue change?")
    assert rig.calls(Step.PLANNER) == 2 and rig.last_row["replans"] == 1 and rig.last_row["subquestions"] == 2
    assert len(rig.gateway.calls_for(Step.SQL_WRITE)) == 2  # only the new sub-question ran again
    assert rig.calls(Step.EVIDENCE_CHECK) == 2
    assert result.outcome == "partial" and "still more" in result.text  # one replan only: then a partial answer
    again = rig.gateway.calls_for(Step.PLANNER)[1].contents
    assert "Missing evidence" in again and "orders per customer" in again and "q1: Revenue by brand (ok)" in again
    assert "Brand A" not in again and '{"brand":' not in again  # guarded summaries, never rows
    assert '"sum": 2233.5' in again  # ... but the counts and totals are there


def test_a_replan_is_not_started_when_the_plan_is_already_full(project, fake_bigquery):
    fake_bigquery.add("revenue", df=ROWS)
    rig = make_rig(project, fake_bigquery=fake_bigquery, config="loop:\n  max_replans: 2\n  max_subquestions: 1\n")
    why(rig, evidence=[{"enough": False, "missing": ["more"]}])
    result = rig.ask("Why did revenue change?")
    assert rig.calls(Step.PLANNER) == 1 and result.outcome == "partial"


def test_a_failing_evidence_check_falls_back_to_the_code_check(rig):
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply("why_question", "Why?"))
    rig.gateway.queue(Step.PLANNER, plan_reply("Revenue by brand"))
    rig.gateway.queue(Step.SQL_WRITE, sql_reply())
    rig.gateway.queue(Step.EVIDENCE_CHECK, AgentError(ErrorKind.LLM_UNAVAILABLE))
    rig.gateway.queue(Step.COMPOSER, composer_reply(BODY))
    assert rig.ask("Why did my revenue change?").outcome == "answered"


def test_the_evidence_prompt_holds_summaries_and_never_rows(rig):
    why(rig)
    rig.ask("Why did revenue change?")
    text = rig.gateway.calls_for(Step.EVIDENCE_CHECK)[0].contents
    assert "q1: ok" in text and '"rows": 2' in text and '"revenue": {"min": 999.0, "max": 1234.5, "sum": 2233.5}' in text
    assert "Brand A" not in text


def test_a_replan_is_not_started_when_it_would_use_the_call_kept_for_the_answer(project, fake_bigquery):
    fake_bigquery.add("revenue", df=ROWS)
    config = "loop:\n  max_replans: 1\nbudget:\n  simple: {calls: 4, seconds: 60}\n  why: {calls: 5, seconds: 60}\n"
    rig = make_rig(project, fake_bigquery=fake_bigquery, config=config)
    why(rig, evidence=[{"enough": False, "missing": ["orders per customer"]}])  # 4 calls are used when the Evidence Check answers
    result = rig.ask("Why did revenue change?")
    assert rig.calls(Step.PLANNER) == 1 and rig.last_row["replans"] == 0 and rig.last_row["llm_calls"] == 5
    assert result.outcome == "partial" and "Brand A earned" in result.text  # the Composer still had its call: no degraded table
    assert "straight from the data" not in result.text
