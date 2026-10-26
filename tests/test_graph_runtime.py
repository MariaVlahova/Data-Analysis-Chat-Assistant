"""The agent runtime (plan 07): the error boundary, the one-run lock, history and the standalone rewrite, progress, and a chaos
run through the real gateway."""
import json

import pandas as pd
import pytest

from src.errors import AgentError, ErrorKind
from src.llm.chaos import Chaos
from src.llm.fake import FakeClock, FakeGenaiClient, reply_for
from src.llm.gateway import LLMGateway
from src.llm.routing import Step
from tests.agent_support import composer_reply, guard_reply, make_rig, plan_reply, sql_reply

ROWS = pd.DataFrame({"brand": ["Brand A", "Brand B"], "revenue": [1234.5, 999.0]})
BODY = "Brand A earned 1,234.5 and Brand B 999."
SECRET = "secret detail: password=hunter2"


@pytest.fixture
def rig(project, fake_bigquery):
    fake_bigquery.add("revenue", df=ROWS)
    return make_rig(project, fake_bigquery=fake_bigquery)


def simple(rig, question="Revenue by brand", body=BODY):
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply(standalone=question))
    rig.gateway.queue(Step.SQL_WRITE, sql_reply())
    rig.gateway.queue(Step.COMPOSER, composer_reply(body))


# the error boundary ---------------------------------------------------------------------------------------------------

def broken(step):
    """A rig where the model call of `step` raises something that is not an AgentError."""
    def arrange(rig):
        rig.gateway.queue(Step.INPUT_GUARD, guard_reply("why_question", "Why?") if step is not Step.INPUT_GUARD else RuntimeError(SECRET))
        if step is Step.INPUT_GUARD:
            return
        rig.gateway.queue(Step.PLANNER, RuntimeError(SECRET) if step is Step.PLANNER else plan_reply("Revenue by brand"))
        rig.gateway.queue(Step.SQL_WRITE, RuntimeError(SECRET) if step is Step.SQL_WRITE else sql_reply())
        rig.gateway.queue(Step.EVIDENCE_CHECK, RuntimeError(SECRET) if step is Step.EVIDENCE_CHECK else {"enough": True, "missing": []})
        rig.gateway.queue(Step.COMPOSER, RuntimeError(SECRET) if step is Step.COMPOSER else composer_reply(BODY))

    return arrange


@pytest.mark.parametrize("step", [Step.INPUT_GUARD, Step.PLANNER, Step.SQL_WRITE, Step.EVIDENCE_CHECK, Step.COMPOSER])
def test_an_exception_in_a_model_step_ends_in_a_friendly_message_with_a_trace_id_and_the_chat_goes_on(rig, step):
    broken(step)(rig)
    result = rig.ask("Why did my revenue change?")
    assert result.outcome == "failed" and result.error.kind is ErrorKind.INTERNAL and "Something went wrong on my side" in result.text
    assert result.error.trace_id == rig.last_trace_id  # the ID the user gets is the stored one
    stored = json.dumps(rig.spans()) + json.dumps(rig.last_row)
    assert "hunter2" not in stored and "secret detail" not in stored and "RuntimeError" in stored  # the class, never the text
    assert any(s["status"] == "error" and s["error_kind"] == "INTERNAL" for s in rig.spans())
    assert rig.last_row["outcome"] == "failed" and rig.last_row["error_kinds"]
    simple(rig)  # the next question works
    assert rig.ask("Revenue by brand").outcome == "answered"


@pytest.mark.parametrize("where,patch", [
    ("policy_context", lambda rig: setattr(rig.agent.deps, "load_user", lambda: (_ for _ in ()).throw(RuntimeError(SECRET)))),
    ("context_assembly", lambda rig: setattr(rig.agent.deps, "today", lambda: (_ for _ in ()).throw(RuntimeError(SECRET)))),
    ("sql_loop", lambda rig: setattr(rig.agent.deps.runner, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError(SECRET)))),
    ("output_guard", lambda rig: setattr(rig.agent.deps.output_guard, "check", lambda text: (_ for _ in ()).throw(RuntimeError(SECRET)))),
])
def test_an_exception_in_a_code_step_is_handled_the_same_way(rig, where, patch):
    patch(rig)
    simple(rig)
    result = rig.ask("Revenue by brand")
    assert result.outcome == "failed" and result.text and re_trace_id(result.error.trace_id)
    names = rig.span_names()
    assert where in names and "compose_failure" in names and "finish" in names  # the boundary routed to compose_failure
    assert "hunter2" not in json.dumps(rig.spans())


def re_trace_id(value):
    import re
    return bool(re.fullmatch(r"[0-9a-f]{8}", value or ""))


def test_the_failing_step_is_named_in_the_trace(rig):
    broken(Step.SQL_WRITE)(rig)
    rig.ask("Why did my revenue change?")
    failing = [s for s in rig.spans() if s["status"] == "error"]
    assert {"llm.call", "sql_loop"} <= {s["name"] for s in failing} or "sql_loop" in {s["name"] for s in failing}


def test_a_failure_in_the_composer_step_drops_the_draft_it_was_holding(rig):
    simple(rig)
    original = rig.agent.deps.output_guard.check

    def boom(text):
        raise RuntimeError(SECRET)

    rig.agent.deps.output_guard.check = boom
    rig.ask("Revenue by brand")
    rig.agent.deps.output_guard.check = original
    assert rig.agent.deps._runtimes == {}  # the run is over and its scratch space with it


# one active run per conversation (A19) --------------------------------------------------------------------------------

def test_a_second_run_in_the_same_conversation_is_refused_while_one_is_active(rig):
    lock = rig.agent._lock_for(rig.conversation_id)
    assert lock.acquire(blocking=False)
    try:
        with pytest.raises(AgentError) as busy:
            rig.ask("Revenue by brand")
        assert "still working on your previous question" in busy.value.user_message and busy.value.retryable
        simple(rig)
        assert rig.ask("Revenue by brand", conversation_id="conv-other").outcome == "answered"  # another conversation is not blocked
    finally:
        lock.release()
    simple(rig)
    assert rig.ask("Revenue by brand").outcome == "answered"


def test_the_lock_is_released_when_a_turn_fails(rig):
    rig.gateway.queue(Step.INPUT_GUARD, RuntimeError(SECRET))
    rig.ask("Revenue by brand")
    simple(rig)
    assert rig.ask("Revenue by brand").outcome == "answered"


# history and the standalone rewrite -----------------------------------------------------------------------------------

def values(rig):
    return rig.agent.graph.get_state({"configurable": {"thread_id": rig.conversation_id}}).values


def test_a_follow_up_is_rewritten_so_the_later_steps_need_no_history(rig):
    simple(rig, "Revenue by brand this year")
    rig.ask("Revenue by brand this year")
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply(standalone="Revenue by brand last year"))
    rig.gateway.queue(Step.SQL_WRITE, sql_reply("SELECT 1 AS x FROM order_items oi LIMIT 1", "Last year."))
    rig.client.add("SELECT 1 AS x", df=pd.DataFrame({"x": [1]}))
    rig.gateway.queue(Step.COMPOSER, composer_reply("One row."))
    rig.ask("and for last year?")
    guard = rig.gateway.calls_for(Step.INPUT_GUARD)[1].contents
    assert "Revenue by brand this year" in guard and "Brand A earned" in guard  # the Input Guard sees the earlier exchange, as data
    assert guard.count("<untrusted_data") == 2
    writer = rig.gateway.calls_for(Step.SQL_WRITE)[1].contents
    composer = rig.gateway.calls_for(Step.COMPOSER)[1].contents
    assert "Revenue by brand last year" in writer  # the writer needs no history: it gets the rewritten question only
    assert "and for last year?" not in writer and "Revenue by brand this year" not in writer and "Brand A earned" not in writer
    assert "Revenue by brand last year" in composer and "and for last year?" in composer  # the Composer also sees the user's own words (overrides)


def test_history_keeps_the_last_six_exchanges_with_each_answer_cut(rig):
    for n in range(8):
        simple(rig, f"Revenue question {n}")
        rig.ask(f"Revenue question {n}")
    history = values(rig)["history"]
    assert [h["user"] for h in history] == [f"Revenue question {n}" for n in range(2, 8)]
    assert all(len(h["assistant"]) <= 600 for h in history)
    simple(rig)
    rig.ask("Revenue by brand")
    assert len(rig.gateway.calls_for(Step.INPUT_GUARD)[-1].contents.split("assistant:")) - 1 == 6


def test_a_refused_or_failed_turn_is_not_kept_in_the_history(rig):
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply("out_of_scope", in_scope=False))
    rig.ask("a poem")
    rig.gateway.queue(Step.INPUT_GUARD, RuntimeError(SECRET))
    rig.ask("anything")
    assert values(rig).get("history", []) == []
    simple(rig)
    rig.ask("Revenue by brand")
    assert [h["user"] for h in values(rig)["history"]] == ["Revenue by brand"]


def test_a_new_conversation_starts_with_no_history(rig):
    simple(rig)
    rig.ask("Revenue by brand")
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply("out_of_scope", in_scope=False))
    rig.ask("hi", conversation_id="conv-new")
    assert "Earlier messages" not in rig.gateway.calls_for(Step.INPUT_GUARD)[1].contents


# progress ----------------------------------------------------------------------------------------------------------------

def test_a_simple_turn_shows_short_progress_lines_and_never_the_answer(rig):
    simple(rig)
    result = rig.ask("Revenue by brand")
    assert rig.progress == ["Understanding your question...", "Querying the data...", "Checking the results...", "Writing the answer..."]
    assert not any("Brand A" in line for line in rig.progress) and BODY not in " ".join(rig.progress) and result.outcome == "answered"


def test_a_why_turn_shows_the_planning_and_which_sub_question_is_running(rig):
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply("why_question", "Why?"))
    rig.gateway.queue(Step.PLANNER, plan_reply("One", "Two"))
    rig.gateway.queue(Step.SQL_WRITE, sql_reply(), sql_reply())
    rig.gateway.queue(Step.EVIDENCE_CHECK, {"enough": True, "missing": []})
    rig.gateway.queue(Step.COMPOSER, composer_reply(BODY))
    rig.ask("Why did my revenue change?")
    assert rig.progress[:4] == ["Understanding your question...", "Planning...", "Querying (1 of 2)...", "Querying (2 of 2)..."]
    assert rig.progress[-2:] == ["Checking the results...", "Writing the answer..."]


# the whole graph under chaos, through the real gateway ------------------------------------------------------------------

def test_a_model_fault_is_retried_by_the_real_gateway_and_the_turn_still_answers(project, fake_bigquery):
    fake_bigquery.add("revenue", df=ROWS)
    clock = FakeClock()
    client = FakeGenaiClient()
    for text in (
        json.dumps({"in_scope": True, "injection_suspected": False, "intent": "data_question", "standalone_question": "Revenue by brand", "selector": None}),
        json.dumps(sql_reply()), json.dumps(composer_reply(BODY)),
    ):
        client.respond(reply_for(text), times=1)
    chaos = Chaos.scripted().fail_llm("rate_limit", times=1).fail_llm("unavailable", times=1, step=Step.COMPOSER)
    from src.config import load_settings

    settings = load_settings(project / "config.yaml", load_env_file=False)
    gateway = LLMGateway(settings, client=client, chaos=chaos, clock=clock, sleep=clock.sleep, jitter=lambda: 0.5)
    rig = make_rig(project, fake_bigquery=fake_bigquery, chaos=chaos, gateway=gateway)
    result = rig.ask("Revenue by brand")
    assert result.outcome == "answered", result
    assert chaos.injected == ["llm:rate_limit", "llm:unavailable"]
    assert rig.last_row["llm_calls"] == 5 and clock.sleeps  # two faulted attempts were charged, and the gateway waited before retrying
    assert any(s["attrs"].get("chaos_fault") for s in rig.spans() if s["name"] == "llm.call")


def test_the_chaos_engine_also_reaches_the_query_runner_and_the_sql_writer_output(project, fake_bigquery):
    fake_bigquery.add("revenue", df=ROWS)
    chaos = Chaos.scripted().corrupt_next_sql("unknown_column").fail_bq()
    rig = make_rig(project, fake_bigquery=fake_bigquery, chaos=chaos)
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply(standalone="Revenue by brand"))
    rig.gateway.queue(Step.SQL_WRITE, sql_reply())
    rig.gateway.queue(Step.SQL_REPAIR, sql_reply())
    rig.gateway.queue(Step.COMPOSER, composer_reply(BODY))
    result = rig.ask("Revenue by brand")
    assert result.outcome == "answered" and chaos.injected == ["sql:unknown_column", "bq:unavailable"]


# a simple question's one sub-question is one line (review finding) ------------------------------------------------------------------------

def test_the_sub_question_of_a_simple_turn_is_one_short_line_even_when_the_rewrite_is_empty(project, fake_bigquery):
    import pandas as pd

    from tests.agent_support import composer_reply, guard_reply, make_rig, sql_reply
    from src.llm.routing import Step

    fake_bigquery.add("revenue", df=pd.DataFrame({"brand": ["Brand A"], "revenue": [1234.5]}))
    rig = make_rig(project, fake_bigquery=fake_bigquery)
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply(standalone=""))  # the model gave no rewrite: the message itself is the question
    rig.gateway.queue(Step.SQL_WRITE, sql_reply())
    rig.gateway.queue(Step.COMPOSER, composer_reply("Brand A earned 1,234.5."))
    rig.ask("revenue by brand\n\nfor last month\n\n\nplease " + "and more " * 60)
    plan = rig.agent.graph.get_state({"configurable": {"thread_id": rig.conversation_id}}).values["plan"]
    text = plan["subquestions"][0]["text"]
    assert "\n" not in text and text.startswith("revenue by brand for last month please and more") and len(text) <= 300
