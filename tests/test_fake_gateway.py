"""FakeGateway (plan 04): the scripted gateway that plans 07 and 09 test against. No network, honest call counts."""
import json

import pytest
from pydantic import BaseModel

from src.errors import AgentError, ErrorKind
from src.llm.budget import TurnBudget
from src.llm.fake import FakeCall, FakeClock, FakeGateway
from src.llm.routing import Step
from src.observability import Tracer


class Plan(BaseModel):
    sql: str


def budget(calls: int = 8) -> TurnBudget:
    return TurnBudget(calls=calls, seconds=60, bytes_=10**9, clock=FakeClock())


def gen(gateway: FakeGateway, step: Step = Step.SQL_WRITE, *, schema=Plan, budget_=None, **kw):
    return gateway.generate(step, system="s", contents="c", schema=schema, budget=budget_ or budget(), **kw)


def test_replies_come_back_in_the_order_queued_per_step():
    gateway = FakeGateway().queue(Step.SQL_WRITE, Plan(sql="a"), {"sql": "b"}, '{"sql": "c"}').queue(Step.COMPOSER, "hello")
    assert [gen(gateway).parsed.sql for _ in range(3)] == ["a", "b", "c"]
    answer = gen(gateway, Step.COMPOSER, schema=None)
    assert answer.text == "hello" and answer.parsed is None and answer.model_used == "fake-model"


def test_an_exception_or_an_exception_class_is_raised():
    gateway = FakeGateway().queue(Step.PLANNER, AgentError(ErrorKind.LLM_UNAVAILABLE), TimeoutError)
    with pytest.raises(AgentError) as first:
        gen(gateway, Step.PLANNER)
    assert first.value.kind is ErrorKind.LLM_UNAVAILABLE
    with pytest.raises(TimeoutError):
        gen(gateway, Step.PLANNER)


def test_a_function_sees_the_call_and_can_answer_from_it():
    gateway = FakeGateway().always(Step.SQL_WRITE, lambda call: {"sql": call.contents.upper()})
    assert gen(gateway).parsed.sql == "C"
    assert isinstance(gateway.calls[0], FakeCall) and gateway.calls[0].system == "s"


def test_always_answers_when_the_queue_is_empty():
    gateway = FakeGateway().queue(Step.SQL_WRITE, {"sql": "first"}).always(Step.SQL_WRITE, {"sql": "rest"})
    assert [gen(gateway).parsed.sql for _ in range(3)] == ["first", "rest", "rest"]


def test_a_step_with_no_scripted_reply_fails_loudly():
    with pytest.raises(AssertionError, match="composer"):
        gen(FakeGateway(), Step.COMPOSER)


def test_a_reply_that_does_not_match_the_schema_is_bad_output():
    gateway = FakeGateway().queue(Step.SQL_WRITE, {"wrong": 1}, "not json")
    for _ in range(2):
        with pytest.raises(AgentError) as caught:
            gen(gateway)
        assert caught.value.kind is ErrorKind.LLM_BAD_OUTPUT


def test_it_charges_the_budget_so_call_limits_hold_in_graph_tests():
    gateway = FakeGateway().always(Step.SQL_WRITE, {"sql": "x"})
    turn = budget(calls=2)
    gen(gateway, budget_=turn)
    gen(gateway, budget_=turn)
    with pytest.raises(AgentError) as caught:
        gen(gateway, budget_=turn)
    assert caught.value.kind is ErrorKind.BUDGET_EXHAUSTED and len(gateway.calls) == 3
    assert len(gateway.calls_for(Step.SQL_WRITE)) == 3 and gateway.calls_for(Step.COMPOSER) == []


def test_it_counts_the_call_in_the_turn_metrics_and_writes_a_span(tmp_path):
    tracer = Tracer(tmp_path / "traces", tmp_path / "runs.jsonl")
    gateway = FakeGateway().always(Step.SQL_WRITE, {"sql": "x"})
    with tracer.start_turn("h", "c", "t-1") as turn:
        gen(gateway, prompt={"prompt_version": "sql_write@1+abcd1234"})
    names = [json.loads(line)["name"] for line in tracer.trace_path(turn.trace_id).read_text(encoding="utf-8").splitlines()]
    assert "llm.call" in names and turn.row["llm_calls"] == 1 and turn.row["models"] == ["fake-model"]
    assert turn.row["prompt_versions"] == ["sql_write@1+abcd1234"]


def test_parallel_limit_does_not_cap_a_fake():
    assert FakeGateway().parallel_limit(3) == 3 and FakeGateway().parallel_limit(0) == 1
