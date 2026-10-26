"""LLMGateway (plan 04): routing, retry with jitter, circuit breaker, fallback, budget, bad output, blocks, chaos, spans.

The real gateway runs against `FakeGenaiClient` (SDK-shaped replies and errors) and a `FakeClock`, so the retry,
breaker and rate-limit code is the real code and no test sleeps or touches the network.
"""
import json
import logging
from contextlib import contextmanager

import httpx
import pytest
from google.genai import errors as genai_errors
from pydantic import BaseModel

from src.config import LlmBreaker, LlmConfig, LlmRetry, RateLimit, Secrets, Settings
from src.errors import AgentError, ErrorKind
from src.llm.chaos import Chaos, LlmFault
from src.llm.fake import FakeClock, FakeGenaiClient, rate_limited, reply_for, sdk_error, unavailable
from src.llm.gateway import REASK_NOTE, SETUP_MESSAGE, LLMGateway
from src.llm.routing import Step
from src.llm.budget import TurnBudget
from src.observability import Tracer, prompt_attrs

LITE, FLASH = "gemini-3.5-flash-lite", "gemini-3.8-flash"
CANARY = "canary@example.test"
GOOD = '{"sql": "SELECT 1", "interpretation": "all orders"}'


class Answer(BaseModel):
    sql: str
    interpretation: str


def make_settings(**llm_options) -> Settings:
    return Settings(llm=LlmConfig(**llm_options))


def make_gateway(settings: Settings | None = None, client: FakeGenaiClient | None = None, *, jitter=lambda: 1.0, **options):
    clock = options.pop("clock", None) or FakeClock()
    client = client or FakeGenaiClient([LITE, FLASH])
    gateway = LLMGateway(settings or make_settings(), client=client, clock=clock, sleep=clock.sleep, jitter=jitter, **options)
    return gateway, client, clock


def make_budget(clock: FakeClock, calls: int = 8, seconds: float = 60) -> TurnBudget:
    return TurnBudget(calls=calls, seconds=seconds, bytes_=10**9, clock=clock)


def ask(gateway: LLMGateway, clock: FakeClock, step: Step = Step.SQL_WRITE, *, schema=Answer, budget=None, **options):
    return gateway.generate(step, system="Write SQL.", contents="revenue by brand", schema=schema,
                            budget=budget or make_budget(clock), **options)


@contextmanager
def in_turn(tmp_path):
    """Run inside a traced turn. After the block, `.row` is the metrics row and `.spans` the stored spans."""
    tracer = Tracer(tmp_path / "traces", tmp_path / "runs.jsonl")
    holder = type("Recorded", (), {})()
    with tracer.start_turn("hash-1", "conv-1", "t-1") as turn:
        yield holder
    holder.row = turn.row
    holder.spans = [json.loads(line) for line in tracer.trace_path(turn.trace_id).read_text(encoding="utf-8").splitlines()]
    holder.calls = [s for s in holder.spans if s["name"] == "llm.call"]


# the happy path ---------------------------------------------------------------------------------

def test_a_structured_call_returns_parsed_json_with_tokens():
    gateway, client, clock = make_gateway()
    client.reply(GOOD, tokens_in=120, tokens_out=30, thoughts=20)
    result = ask(gateway, clock)
    assert result.parsed == Answer(sql="SELECT 1", interpretation="all orders")
    assert result.text == GOOD and result.model_used == FLASH
    assert (result.tokens_in, result.tokens_out) == (120, 50)  # thinking tokens are billed as output
    assert result.attempts == 1 and result.fallback_used is False


def test_the_request_carries_the_step_settings_the_schema_the_timeout_and_no_sdk_retries():
    gateway, client, clock = make_gateway()
    client.reply(GOOD)
    ask(gateway, clock)
    request = client.requests[0]
    config = request.config
    assert request.model == FLASH and request.system == "Write SQL." and request.contents == "revenue by brand"
    assert config.temperature == 0 and config.max_output_tokens == 2048
    assert config.response_mime_type == "application/json" and config.response_schema is Answer
    assert config.http_options.timeout == 30_000  # llm.request_timeout_s
    assert config.http_options.retry_options.attempts == 1  # the SDK does not retry: one place counts the calls


def test_the_attempt_timeout_is_cut_to_the_time_the_turn_has_left():
    gateway, client, clock = make_gateway()
    client.reply(GOOD)
    ask(gateway, clock, budget=make_budget(clock, seconds=4))
    assert client.requests[0].config.http_options.timeout == 4000


def test_a_text_call_has_no_schema_and_no_parsed_object():
    gateway, client, clock = make_gateway()
    client.reply("Revenue grew 4%.")
    result = ask(gateway, clock, Step.COMPOSER, schema=None)
    assert result.parsed is None and result.text == "Revenue grew 4%."
    config = client.requests[0].config
    assert config.response_schema is None and config.response_mime_type is None
    assert config.temperature == pytest.approx(0.3)


def test_the_step_decides_the_model_and_a_temperature_can_be_overridden():
    gateway, client, clock = make_gateway()
    client.reply(GOOD).reply(GOOD)
    assert ask(gateway, clock, Step.INPUT_GUARD).model_used == LITE
    ask(gateway, clock, Step.SQL_REPAIR, temperature=0.9)
    assert client.requests[1].model == FLASH and client.requests[1].config.temperature == pytest.approx(0.9)


def test_json_in_a_code_fence_is_accepted():
    gateway, client, clock = make_gateway()
    client.reply(f"```json\n{GOOD}\n```")
    assert ask(gateway, clock).parsed.sql == "SELECT 1"


# retry with jitter ------------------------------------------------------------------------------

def test_a_429_then_success_is_one_retry_with_a_jittered_wait_and_two_calls_charged():
    gateway, client, clock = make_gateway(jitter=lambda: 0.5)
    client.fail(rate_limited()).reply(GOOD)
    budget = make_budget(clock)
    result = ask(gateway, clock, budget=budget)
    assert result.model_used == FLASH and result.attempts == 2 and result.fallback_used is False
    assert budget.calls_used == 2 and len(client.requests) == 2
    assert clock.sleeps == [pytest.approx(0.5)]  # 0.5 x min(20, 1 x 2^0)


def test_waits_grow_and_stay_within_the_cap_for_every_jitter_value():
    for jitter, expected in ((0.0, [0.0, 0.0]), (0.999, [0.999, 1.998]), (1.0, [1.0, 2.0])):
        gateway, client, clock = make_gateway(jitter=lambda j=jitter: j)
        client.fail(unavailable(), times=2).reply(GOOD)
        ask(gateway, clock)
        assert clock.sleeps == [pytest.approx(w) for w in expected]  # between 0 and base x 2^(n-1), never above


def test_the_cap_limits_the_wait():
    gateway, client, clock = make_gateway(make_settings(retry=LlmRetry(base_s=10, cap_s=15, attempts=3)))
    client.fail(unavailable(), times=2).reply(GOOD)
    ask(gateway, clock)
    assert clock.sleeps == [pytest.approx(10), pytest.approx(15)]  # 10, then min(15, 20)


def test_a_retry_delay_in_the_429_is_honored():
    gateway, client, clock = make_gateway(jitter=lambda: 0.1)
    client.fail(rate_limited(retry_delay_s=7)).reply(GOOD)
    ask(gateway, clock)
    assert clock.sleeps == [pytest.approx(7)]  # at least what the server asked, more than the 0.1 s of jitter


def test_a_retry_after_header_is_honored_too():
    gateway, client, clock = make_gateway(jitter=lambda: 0.1)
    error = rate_limited()
    error.response = type("Response", (), {"headers": {"retry-after": "4"}})()
    client.fail(error).reply(GOOD)
    ask(gateway, clock)
    assert clock.sleeps == [pytest.approx(4)]


@pytest.mark.parametrize("failure", [
    unavailable(), sdk_error(500, "INTERNAL"), sdk_error(408, "DEADLINE_EXCEEDED"),
    httpx.ReadTimeout("slow"), httpx.ConnectError("down"), TimeoutError("slow"), ConnectionResetError("reset"),
])
def test_5xx_timeouts_and_network_errors_are_retried(failure):
    gateway, client, clock = make_gateway()
    client.fail(failure).reply(GOOD)
    assert ask(gateway, clock).attempts == 2


def test_a_wait_that_would_pass_the_deadline_is_not_slept_and_the_sibling_is_asked_now():
    gateway, client, clock = make_gateway()
    client.fail(rate_limited(retry_delay_s=30), model=FLASH).reply(GOOD, model=LITE)
    result = ask(gateway, clock, budget=make_budget(clock, seconds=10))
    assert result.model_used == LITE and result.fallback_used and clock.sleeps == []


def test_a_bad_request_is_not_retried_and_does_not_try_the_sibling():
    gateway, client, clock = make_gateway()
    client.fail(sdk_error(400, "INVALID_ARGUMENT"))
    with pytest.raises(AgentError) as caught:
        ask(gateway, clock)
    assert caught.value.kind is ErrorKind.LLM_UNAVAILABLE and caught.value.user_message == SETUP_MESSAGE
    assert caught.value.retryable is False and "HTTP 400 INVALID_ARGUMENT" in caught.value.detail
    assert len(client.requests) == 1 and clock.sleeps == []


def test_a_wrong_key_says_it_is_a_setup_problem():
    gateway, client, clock = make_gateway()
    client.fail(sdk_error(403, "PERMISSION_DENIED"))
    with pytest.raises(AgentError) as caught:
        ask(gateway, clock)
    assert caught.value.user_message == SETUP_MESSAGE


def test_a_model_id_that_does_not_exist_sends_the_call_to_the_sibling_and_never_opens_the_breaker():
    gateway, client, clock = make_gateway()
    client.fail(sdk_error(404, "NOT_FOUND"), model=FLASH, times=None).reply(GOOD, model=LITE, times=None)
    for _ in range(5):
        assert ask(gateway, clock).model_used == LITE
    assert len(client.requests_to(FLASH)) == 5  # asked each time: a wrong ID is a config problem, not an outage


def test_a_404_from_both_models_is_a_setup_problem():
    gateway, client, clock = make_gateway()
    client.fail(sdk_error(404, "NOT_FOUND"), times=None)
    with pytest.raises(AgentError) as caught:
        ask(gateway, clock)
    assert caught.value.kind is ErrorKind.LLM_UNAVAILABLE and caught.value.user_message == SETUP_MESSAGE


def test_a_failure_that_is_not_a_gemini_or_network_problem_is_not_hidden():
    gateway, client, clock = make_gateway()
    client.fail(ValueError("a bug in our code")).reply(GOOD)
    with pytest.raises(ValueError):
        ask(gateway, clock)
    assert ask(gateway, clock).parsed is not None  # and the gateway still works


# fallback ---------------------------------------------------------------------------------------

def test_when_flash_fails_every_attempt_flash_lite_answers():
    gateway, client, clock = make_gateway()
    client.fail(unavailable(), model=FLASH, times=3).reply(GOOD, model=LITE)
    budget = make_budget(clock)
    result = ask(gateway, clock, budget=budget)
    assert result.model_used == LITE and result.fallback_used is True and result.attempts == 4
    assert [r.model for r in client.requests] == [FLASH, FLASH, FLASH, LITE]
    assert budget.calls_used == 4  # every outbound attempt, retries and the fallback included


def test_when_both_models_fail_a_typed_error_comes_out_and_it_does_not_crash():
    gateway, client, clock = make_gateway()
    client.fail(unavailable(), times=None)
    with pytest.raises(AgentError) as caught:
        ask(gateway, clock)
    assert caught.value.kind is ErrorKind.LLM_UNAVAILABLE and caught.value.retryable
    assert FLASH in caught.value.detail and LITE in caught.value.detail
    assert len(client.requests) == 6  # 3 attempts per model


def test_429_everywhere_ends_as_rate_limited_not_unavailable():
    gateway, client, clock = make_gateway()
    client.fail(rate_limited(), times=None)
    with pytest.raises(AgentError) as caught:
        ask(gateway, clock)
    assert caught.value.kind is ErrorKind.LLM_RATE_LIMITED


# circuit breaker --------------------------------------------------------------------------------

def test_three_failures_open_the_breaker_the_next_call_skips_to_the_sibling_and_a_probe_closes_it():
    gateway, client, clock = make_gateway()
    client.fail(unavailable(), model=FLASH, times=3).reply(GOOD, model=LITE, times=2).reply(GOOD, model=FLASH, times=None)
    first = ask(gateway, clock)
    assert first.fallback_used and len(client.requests_to(FLASH)) == 3

    second = ask(gateway, clock)  # the breaker is open: Flash is not even tried
    assert second.model_used == LITE and len(client.requests_to(FLASH)) == 3

    clock.advance(31)  # the cooldown is over: one probe
    third = ask(gateway, clock)
    assert third.model_used == FLASH and third.fallback_used is False
    assert ask(gateway, clock).model_used == FLASH  # closed again


def test_a_failed_probe_reopens_the_breaker_and_the_sibling_answers():
    gateway, client, clock = make_gateway()
    client.fail(unavailable(), model=FLASH, times=None).reply(GOOD, model=LITE, times=None)
    ask(gateway, clock)  # 3 failures open it
    clock.advance(31)
    result = ask(gateway, clock)  # the probe fails, one request, then Flash-Lite
    assert result.model_used == LITE and len(client.requests_to(FLASH)) == 4
    ask(gateway, clock)
    assert len(client.requests_to(FLASH)) == 4  # open again


def test_when_every_breaker_is_open_the_error_is_circuit_open_and_nothing_is_sent():
    gateway, client, clock = make_gateway()
    client.fail(unavailable(), times=6)
    with pytest.raises(AgentError):
        ask(gateway, clock)  # 3 failures on each model
    budget = make_budget(clock)
    with pytest.raises(AgentError) as caught:
        ask(gateway, clock, budget=budget)
    assert caught.value.kind is ErrorKind.CIRCUIT_OPEN
    assert len(client.requests) == 6 and budget.calls_used == 0  # skipping a model costs nothing


def test_bad_requests_safety_blocks_and_bad_json_do_not_count_against_the_breaker():
    gateway, client, clock = make_gateway(make_settings(breaker=LlmBreaker(failure_threshold=2, cooldown_s=30)))
    for _ in range(3):
        client.respond(reply_for(None, block_reason="SAFETY"))
        with pytest.raises(AgentError):
            ask(gateway, clock)
    client.reply(GOOD)
    assert ask(gateway, clock).model_used == FLASH  # still closed after three blocks


def test_one_models_breaker_does_not_affect_the_other():
    gateway, client, clock = make_gateway()
    client.fail(unavailable(), model=FLASH, times=None).reply(GOOD, model=LITE, times=None)
    ask(gateway, clock)
    assert ask(gateway, clock, Step.INPUT_GUARD).model_used == LITE  # Flash-Lite is first here and is healthy


# turn budget ------------------------------------------------------------------------------------

def test_the_ninth_call_of_a_simple_turn_raises_budget_exhausted_and_sends_nothing():
    gateway, client, clock = make_gateway()
    client.reply(GOOD, times=None)
    budget = make_budget(clock)
    for _ in range(8):
        ask(gateway, clock, budget=budget)
    with pytest.raises(AgentError) as caught:
        ask(gateway, clock, budget=budget)
    assert caught.value.kind is ErrorKind.BUDGET_EXHAUSTED and len(client.requests) == 8


def test_retries_use_up_the_budget_and_the_budget_error_wins_over_the_provider_error():
    gateway, client, clock = make_gateway()
    client.fail(rate_limited(), times=None)
    budget = make_budget(clock, calls=2)
    with pytest.raises(AgentError) as caught:
        ask(gateway, clock, budget=budget)
    assert caught.value.kind is ErrorKind.BUDGET_EXHAUSTED
    assert len(client.requests) == 2 and budget.calls_used == 2


def test_upgrading_the_budget_lets_a_why_turn_use_20_calls():
    # At the default 10 requests a minute the rate limiter itself would stall a 20-call turn past its 60 s: raise the rpm first.
    gateway, client, clock = make_gateway(make_settings(default_rpm=100))
    client.reply(GOOD, times=None)
    budget = TurnBudget.from_settings(Settings().budget, clock)
    for _ in range(8):
        ask(gateway, clock, budget=budget)
    budget.upgrade("why")
    for _ in range(12):
        ask(gateway, clock, budget=budget)
    with pytest.raises(AgentError) as caught:
        ask(gateway, clock, budget=budget)
    assert caught.value.kind is ErrorKind.BUDGET_EXHAUSTED and budget.calls_used == 20


def test_a_turn_out_of_time_raises_turn_timeout_before_any_request():
    gateway, client, clock = make_gateway()
    budget = make_budget(clock, seconds=60)
    clock.advance(61)
    with pytest.raises(AgentError) as caught:
        ask(gateway, clock, budget=budget)
    assert caught.value.kind is ErrorKind.TURN_TIMEOUT and client.requests == []


def test_a_retry_wait_that_does_not_fit_in_the_turn_is_skipped_and_the_error_is_the_providers():
    gateway, client, clock = make_gateway(make_settings(retry=LlmRetry(base_s=5, cap_s=5, attempts=3)))
    client.fail(unavailable(), times=None)
    with pytest.raises(AgentError) as caught:
        ask(gateway, clock, budget=make_budget(clock, seconds=7))  # one 5 s wait fits, the second does not
    assert caught.value.kind is ErrorKind.LLM_UNAVAILABLE
    assert clock.sleeps == [pytest.approx(5)] and clock.now - 1000 == pytest.approx(5)  # never slept past the deadline
    assert len(client.requests) == 3  # Flash twice, then Flash-Lite once: its own wait did not fit either


def test_a_retry_wait_that_leaves_under_a_second_for_the_call_is_skipped_for_the_sibling():
    """Review finding: a wait of 9.9 s in a turn with 10 s left was slept, and the call then had 0.1 s."""
    gateway, client, clock = make_gateway()
    client.fail(rate_limited(retry_delay_s=9.5), model=FLASH).reply(GOOD, model=LITE)
    result = ask(gateway, clock, budget=make_budget(clock, seconds=10))
    assert result.model_used == LITE and result.fallback_used and clock.sleeps == []
    gateway, client, clock = make_gateway()
    client.fail(rate_limited(retry_delay_s=8.5), model=FLASH).reply(GOOD, model=FLASH)
    assert ask(gateway, clock, budget=make_budget(clock, seconds=10)).model_used == FLASH  # 8.5 s leaves 1.5 s: worth waiting
    assert clock.sleeps == [pytest.approx(8.5)]


def test_a_turn_that_is_already_out_of_time_when_the_next_attempt_starts_is_a_turn_timeout():
    gateway, client, clock = make_gateway()
    budget = make_budget(clock, seconds=60)

    def slow_failure(*args, **kwargs):  # the request itself uses up the whole turn
        clock.advance(61)
        raise unavailable()

    client.models.generate_content = slow_failure
    with pytest.raises(AgentError) as caught:
        ask(gateway, clock, budget=budget)
    assert caught.value.kind is ErrorKind.TURN_TIMEOUT


# bad output -------------------------------------------------------------------------------------

def test_bad_json_is_asked_again_once_with_a_note_and_then_succeeds():
    gateway, client, clock = make_gateway()
    client.reply("not json", tokens_in=100, tokens_out=10).reply(GOOD, tokens_in=110, tokens_out=12)
    budget = make_budget(clock)
    result = ask(gateway, clock, budget=budget)
    assert result.parsed is not None and result.attempts == 2 and budget.calls_used == 2
    assert (result.tokens_in, result.tokens_out) == (210, 22)  # the re-ask is paid for in the result
    assert REASK_NOTE not in client.requests[0].system and REASK_NOTE in client.requests[1].system
    assert clock.sleeps == []  # a re-ask is immediate


def test_bad_json_twice_is_llm_bad_output_after_exactly_one_re_ask():
    gateway, client, clock = make_gateway()
    client.reply("not json", times=None)
    budget = make_budget(clock)
    with pytest.raises(AgentError) as caught:
        ask(gateway, clock, budget=budget)
    assert caught.value.kind is ErrorKind.LLM_BAD_OUTPUT and caught.value.retryable
    assert len(client.requests) == 2 and budget.calls_used == 2


def test_json_that_does_not_match_the_schema_is_bad_output_and_names_only_the_fields():
    gateway, client, clock = make_gateway()
    client.reply(f'{{"sql": 5, "secret": "{CANARY}"}}', times=None)
    with pytest.raises(AgentError) as caught:
        ask(gateway, clock)
    assert "sql" in caught.value.detail and "interpretation" in caught.value.detail
    assert CANARY not in caught.value.detail and CANARY not in str(caught.value)  # the reply is never quoted


def test_the_re_ask_is_per_call_not_per_model():
    gateway, client, clock = make_gateway()
    client.reply("garbage").fail(unavailable(), model=FLASH, times=3).reply("garbage", model=LITE)
    with pytest.raises(AgentError) as caught:
        ask(gateway, clock)
    assert caught.value.kind is ErrorKind.LLM_BAD_OUTPUT


def test_an_empty_reply_that_hit_the_token_limit_is_bad_output_with_the_reason():
    gateway, client, clock = make_gateway()
    client.reply("", finish="MAX_TOKENS", times=None)
    with pytest.raises(AgentError) as caught:
        ask(gateway, clock)
    assert caught.value.kind is ErrorKind.LLM_BAD_OUTPUT and "token limit" in caught.value.detail


def test_text_cut_off_at_the_token_limit_names_the_finish_reason():
    gateway, client, clock = make_gateway()
    client.reply('{"sql": "SELECT', finish="MAX_TOKENS", times=None)
    with pytest.raises(AgentError) as caught:
        ask(gateway, clock)
    assert "MAX_TOKENS" in caught.value.detail


def test_a_reply_that_hit_the_output_cap_is_asked_again_with_a_bigger_cap():
    """Review finding: the re-ask repeated the same cap, so a reply that ended there could never come out different."""
    gateway, client, clock = make_gateway()
    client.reply("", finish="MAX_TOKENS").reply(GOOD)
    result = ask(gateway, clock, Step.INPUT_GUARD)  # a 512-token step on a model that spends them thinking
    assert result.parsed is not None and result.attempts == 2
    assert [r.config.max_output_tokens for r in client.requests] == [512, 1024]


def test_cut_off_json_is_asked_again_with_a_bigger_cap_and_other_bad_output_keeps_the_cap():
    gateway, client, clock = make_gateway()
    client.reply('{"sql": "SELECT', finish="MAX_TOKENS").reply("not json")
    with pytest.raises(AgentError):
        ask(gateway, clock)  # cut off, asked again with 4096, bad again: that is the one re-ask
    assert [r.config.max_output_tokens for r in client.requests] == [2048, 4096]
    client.reply("not json").reply(GOOD)
    ask(gateway, clock, budget=make_budget(clock))
    assert [r.config.max_output_tokens for r in client.requests[2:]] == [2048, 2048]


def test_the_bigger_cap_stops_at_a_ceiling_and_never_goes_below_a_cap_already_higher():
    for cap, expected in ((12_000, 16_384), (30_000, 30_000)):
        gateway, client, clock = make_gateway(make_settings(steps={"sql_write": {"max_output_tokens": cap}}))
        client.reply("", finish="MAX_TOKENS").reply(GOOD)
        ask(gateway, clock)
        assert [r.config.max_output_tokens for r in client.requests] == [cap, expected]


def test_free_text_that_was_cut_off_is_not_returned_as_an_answer():
    """Review finding: with no schema nothing failed to parse, so half a sentence came back as a normal result."""
    gateway, client, clock = make_gateway()
    client.reply("Revenue grew 4% because the", finish="MAX_TOKENS").reply("Revenue grew 4% because of the sale.")
    result = ask(gateway, clock, Step.COMPOSER, schema=None)
    assert result.text == "Revenue grew 4% because of the sale." and result.attempts == 2
    assert [r.config.max_output_tokens for r in client.requests] == [4096, 8192]


def test_free_text_cut_off_twice_is_bad_output_with_the_reason():
    gateway, client, clock = make_gateway()
    client.reply("Revenue grew", finish="MAX_TOKENS", times=None)
    with pytest.raises(AgentError) as caught:
        ask(gateway, clock, Step.COMPOSER, schema=None)
    assert caught.value.kind is ErrorKind.LLM_BAD_OUTPUT and "output token limit" in caught.value.detail
    assert len(client.requests) == 2


# safety blocks ----------------------------------------------------------------------------------

@pytest.mark.parametrize("response", [
    reply_for(None, block_reason="SAFETY"),  # the prompt was blocked
    reply_for(None, block_reason="PROHIBITED_CONTENT"),
    reply_for("partial", finish="SAFETY"),
    reply_for("partial", finish="PROHIBITED_CONTENT"),
    reply_for("partial", finish="RECITATION"),
    reply_for("", finish="SAFETY"),  # a refusal that left no text
])
def test_a_block_the_model_states_is_llm_blocked_with_no_retry_and_no_sibling(response):
    gateway, client, clock = make_gateway()
    client.respond(response, times=None)
    budget = make_budget(clock)
    with pytest.raises(AgentError) as caught:
        ask(gateway, clock, budget=budget)
    assert caught.value.kind is ErrorKind.LLM_BLOCKED and not caught.value.retryable
    assert len(client.requests) == 1 and budget.calls_used == 1 and clock.sleeps == []


@pytest.mark.parametrize("empty", [reply_for(None), reply_for("", finish="STOP"), reply_for(None, block_reason="BLOCKED_REASON_UNSPECIFIED")])
def test_an_empty_reply_with_no_stated_refusal_is_a_glitch_that_is_asked_again_not_a_safety_block(empty):
    """Review finding: it used to be LLM_BLOCKED, so a transient glitch rejected a legitimate question as unsafe (S10)."""
    gateway, client, clock = make_gateway()
    client.respond(empty).reply(GOOD)
    result = ask(gateway, clock)
    assert result.parsed is not None and result.attempts == 2 and client.requests_to(LITE) == []


def test_an_empty_reply_twice_is_bad_output_which_is_retryable_not_blocked():
    gateway, client, clock = make_gateway()
    client.respond(reply_for(None), times=None)
    with pytest.raises(AgentError) as caught:
        ask(gateway, clock)
    assert caught.value.kind is ErrorKind.LLM_BAD_OUTPUT and caught.value.retryable and "empty" in caught.value.detail
    assert len(client.requests) == 2


# rate limiter -----------------------------------------------------------------------------------

def test_requests_over_the_rate_limit_wait_for_a_slot():
    gateway, client, clock = make_gateway(make_settings(rate_limits={FLASH: RateLimit(rpm=6)}))
    client.reply(GOOD, times=None)
    for _ in range(7):
        ask(gateway, clock, budget=make_budget(clock, seconds=600))
    assert clock.sleeps == [pytest.approx(60)]  # the 7th waited until the first was a minute old


def test_a_saturated_model_that_cannot_wait_hands_the_call_to_the_sibling():
    gateway, client, clock = make_gateway(make_settings(rate_limits={FLASH: RateLimit(rpm=1)}))
    client.reply(GOOD, times=None)
    ask(gateway, clock)
    result = ask(gateway, clock, budget=make_budget(clock, seconds=20))  # the next Flash slot is 60 s away
    assert result.model_used == LITE and result.fallback_used and clock.sleeps == []
    assert len(client.requests_to(FLASH)) == 1


def test_when_every_model_is_saturated_the_error_is_rate_limited_and_nothing_is_charged():
    gateway, client, clock = make_gateway(make_settings(default_rpm=1))
    client.reply(GOOD, times=None)
    ask(gateway, clock)
    ask(gateway, clock, Step.INPUT_GUARD)
    budget = make_budget(clock, seconds=20)
    with pytest.raises(AgentError) as caught:
        ask(gateway, clock, budget=budget)
    assert caught.value.kind is ErrorKind.LLM_RATE_LIMITED and budget.calls_used == 0


def test_parallel_subqueries_are_capped_by_the_rate_limiter():
    gateway, _, _ = make_gateway(make_settings(default_rpm=2, rate_limits={FLASH: RateLimit(rpm=3)}))
    assert gateway.parallel_limit(1) == 1 and gateway.parallel_limit(8) == 2 and gateway.parallel_limit(0) == 1


# chaos ------------------------------------------------------------------------------------------

def test_chaos_faults_go_through_the_real_retry_code_and_spend_no_quota():
    chaos = Chaos.scripted().fail_llm(LlmFault.RATE_LIMIT).fail_llm(LlmFault.TIMEOUT)
    gateway, client, clock = make_gateway(chaos=chaos)
    client.reply(GOOD)
    budget = make_budget(clock)
    result = ask(gateway, clock, budget=budget)
    assert result.attempts == 3 and budget.calls_used == 3  # the faulted attempts cost calls, as real ones do
    assert len(client.requests) == 1  # but only one went out
    assert clock.sleeps[0] >= 1.0  # the injected 429 asked for a second


def test_a_chaos_malformed_reply_takes_the_re_ask_path():
    chaos = Chaos.scripted().fail_llm(LlmFault.BAD_JSON)
    gateway, client, clock = make_gateway(chaos=chaos)
    client.reply(GOOD)
    result = ask(gateway, clock)
    assert result.attempts == 2 and REASK_NOTE in client.requests[0].system


def test_a_chaos_malformed_reply_on_a_free_text_step_comes_back_empty_and_is_recovered_not_returned():
    """Review finding: with no schema the garbage text was accepted as the answer."""
    chaos = Chaos.scripted().fail_llm(LlmFault.BAD_JSON)
    gateway, client, clock = make_gateway(chaos=chaos)
    client.reply("The answer.")
    result = ask(gateway, clock, Step.COMPOSER, schema=None)
    assert result.text == "The answer." and result.attempts == 2


def test_chaos_that_never_ends_fails_with_a_typed_error_and_uses_the_sibling_first():
    chaos = Chaos.scripted().fail_llm(LlmFault.UNAVAILABLE, times=3)
    gateway, client, clock = make_gateway(chaos=chaos)
    client.reply(GOOD, model=LITE)
    result = ask(gateway, clock)
    assert result.model_used == LITE and result.fallback_used


def test_a_harsh_run_of_many_calls_ends_every_call_with_a_result_or_a_typed_error():
    gateway, client, clock = make_gateway(chaos=Chaos.from_level("harsh", 2024))
    client.reply(GOOD, times=None)
    outcomes = []
    for _ in range(60):
        try:
            outcomes.append(ask(gateway, clock, budget=make_budget(clock, calls=20)).model_used)
        except AgentError as exc:
            outcomes.append(exc.kind)
        clock.advance(1)
    assert len(outcomes) == 60 and any(isinstance(o, str) for o in outcomes)  # no unhandled exception, most calls answered
    assert all(isinstance(o, (str, ErrorKind)) for o in outcomes)


# spans, metrics, hooks ---------------------------------------------------------------------------

def test_each_attempt_is_an_llm_call_span_and_the_metrics_row_counts_the_calls(tmp_path):
    gateway, client, clock = make_gateway()
    client.fail(unavailable(), model=FLASH, times=3).reply(GOOD, model=LITE, tokens_in=50, tokens_out=7)
    with in_turn(tmp_path) as turn:
        ask(gateway, clock, prompt=prompt_attrs("sql_write", 3, "Write SQL."))
    assert len(turn.calls) == 4
    first, last = turn.calls[0], turn.calls[-1]
    assert first["kind"] == "llm" and first["status"] == "error" and first["error_kind"] == "LLM_UNAVAILABLE"
    assert first["attrs"]["model"] == FLASH and first["attrs"]["attempt"] == 1 and first["attrs"]["retry_reason"] == "server_error"
    assert first["attrs"]["step"] == "sql_write" and first["attrs"]["fallback_used"] is False
    assert first["attrs"]["prompt_version"].startswith("sql_write@3+")
    assert last["status"] == "ok" and last["attrs"]["model"] == LITE and last["attrs"]["fallback_used"] is True
    assert last["attrs"]["tokens_in"] == 50 and last["attrs"]["tokens_out"] == 7 and last["attrs"]["attempt"] == 4
    assert last["attrs"].get("waited_before_s") is None and turn.calls[1]["attrs"]["waited_before_s"] == pytest.approx(1.0)
    assert turn.calls[2]["attrs"].get("breaker_opened") is True
    row = turn.row
    assert row["llm_calls"] == 4 and row["tokens_in"] == 50 and row["tokens_out"] == 7
    assert row["fallback_used"] is True and row["models"] == [FLASH, LITE]
    assert row["error_kinds"] == []  # a failure that a retry or the sibling got over is not a turn error
    assert any(v.startswith("sql_write@3+") for v in row["prompt_versions"])


def test_a_turn_ending_error_is_recorded_on_the_turn(tmp_path):
    gateway, client, clock = make_gateway()
    client.respond(reply_for(None, block_reason="SAFETY"))
    with pytest.raises(AgentError):
        with in_turn(tmp_path):
            ask(gateway, clock)
    # the recorder never ran its tail; read the row from the file
    rows = [json.loads(line) for line in (tmp_path / "runs.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[0]["error_kinds"] == ["LLM_BLOCKED"] and rows[0]["llm_calls"] == 1 and rows[0]["outcome"] == "failed"


def test_chaos_is_visible_in_the_span(tmp_path):
    gateway, client, clock = make_gateway(chaos=Chaos.scripted().fail_llm(LlmFault.UNAVAILABLE))
    client.reply(GOOD)
    with in_turn(tmp_path) as turn:
        ask(gateway, clock)
    assert turn.calls[0]["attrs"]["chaos_fault"] == "unavailable" and "chaos_fault" not in turn.calls[1]["attrs"]


def test_without_a_prompt_the_span_carries_a_hash_of_the_system_prompt(tmp_path):
    gateway, client, clock = make_gateway()
    client.reply(GOOD)
    with in_turn(tmp_path) as turn:
        ask(gateway, clock)
    assert turn.calls[0]["attrs"]["prompt_version"].startswith("sql_write@unversioned+")


def test_a_cost_hook_sees_every_call_and_a_failing_hook_changes_nothing():
    seen = []

    def hook(model, tokens_in, tokens_out):
        seen.append((model, tokens_in, tokens_out))
        raise RuntimeError("the hook is broken")

    gateway, client, clock = make_gateway(cost_hook=hook)
    client.fail(unavailable()).reply(GOOD, tokens_in=9, tokens_out=3)
    assert ask(gateway, clock).parsed is not None
    assert seen == [(FLASH, 0, 0), (FLASH, 9, 3)]


# nothing leaks ----------------------------------------------------------------------------------

def test_the_text_of_a_provider_error_is_never_kept(tmp_path):
    gateway, client, clock = make_gateway()
    leak = genai_errors.ClientError(400, {"error": {"code": 400, "message": f"bad value {CANARY}", "status": f"INVALID {CANARY}"}})
    client.fail(leak)
    with pytest.raises(AgentError) as caught:
        with in_turn(tmp_path):
            ask(gateway, clock)
    assert CANARY not in str(caught.value) and CANARY not in caught.value.detail and caught.value.detail == f"HTTP 400 from {FLASH}"
    stored = "".join(p.read_text(encoding="utf-8") for p in (tmp_path / "traces").iterdir()) + (tmp_path / "runs.jsonl").read_text(encoding="utf-8")
    assert CANARY not in stored


def test_no_key_is_a_clear_setup_error_that_names_no_secret():
    settings = Settings(secrets=Secrets())
    gateway = LLMGateway(settings)
    clock = FakeClock()
    with pytest.raises(AgentError) as caught:
        ask(gateway, clock)
    assert caught.value.kind is ErrorKind.LLM_UNAVAILABLE and caught.value.user_message == SETUP_MESSAGE
    assert caught.value.detail == "GEMINI_API_KEY is not set" and not caught.value.retryable


def test_the_client_has_its_own_timeout_so_models_list_cannot_hang_and_the_sdk_does_not_retry():
    """Review finding: only generate_content passed a timeout, so doctor --live could block forever on models.list()."""
    gateway, _, _ = make_gateway(make_settings(request_timeout_s=12))
    options = gateway.http_options()
    assert options.timeout == 12_000 and options.retry_options.attempts == 1
    assert gateway.http_options(2.5).timeout == 2500


def test_an_sdk_without_retry_options_still_works_because_it_never_retried(monkeypatch):
    from google.genai import types

    monkeypatch.delattr(types, "HttpRetryOptions")
    gateway, _, _ = make_gateway()
    options = gateway.http_options()
    assert options.timeout == 30_000 and options.retry_options is None


def test_the_real_client_is_made_from_the_key_on_first_use_without_calling_the_network():
    from pydantic import SecretStr

    gateway = LLMGateway(Settings(secrets=Secrets(gemini_api_key=SecretStr("not-a-real-key"))))
    sdk = gateway.client
    assert type(sdk).__name__ == "Client" and gateway.client is sdk


def test_the_models_the_key_can_use_are_listed_without_the_models_prefix():
    gateway, _, _ = make_gateway(client=FakeGenaiClient(["gemini-a", "gemini-b"]))
    assert gateway.available_models() == {"gemini-a", "gemini-b"}


# tokens in the log -------------------------------------------------------------------------------

def test_every_outbound_attempt_writes_one_log_line_with_its_tokens_a_failed_one_too(caplog):
    gateway, client, clock = make_gateway()
    client.fail(unavailable()).reply(GOOD, tokens_in=120, tokens_out=30, thoughts=20)
    with caplog.at_level(logging.INFO, logger="src.llm.gateway"):
        ask(gateway, clock)
    lines = [record.getMessage() for record in caplog.records if record.getMessage().startswith("model call")]
    assert lines == [
        f"model call step=sql_write model={FLASH} attempt=1 tokens_in=0 tokens_out=0 fallback=False trace_id=-",
        f"model call step=sql_write model={FLASH} attempt=2 tokens_in=120 tokens_out=50 fallback=False trace_id=-",  # thinking tokens are billed as output
    ]


def test_the_log_line_names_the_trace_of_the_turn_and_never_the_prompt_or_the_reply(tmp_path, caplog):
    gateway, client, clock = make_gateway()
    client.reply(GOOD, tokens_in=9, tokens_out=3)
    with caplog.at_level(logging.INFO, logger="src.llm.gateway"):
        with in_turn(tmp_path) as turn:
            gateway.generate(Step.SQL_WRITE, system="Write SQL.", contents=f"revenue for {CANARY}", schema=Answer, budget=make_budget(clock))
    (line,) = [record.getMessage() for record in caplog.records if record.getMessage().startswith("model call")]
    assert f"trace_id={turn.row['trace_id']}" in line and "tokens_in=9 tokens_out=3" in line
    assert CANARY not in caplog.text and "SELECT 1" not in caplog.text


def test_a_call_to_the_fallback_model_is_logged_as_a_fallback(caplog):
    gateway, client, clock = make_gateway()
    client.fail(sdk_error(404, "NOT_FOUND"), model=FLASH).reply(GOOD, model=LITE, tokens_in=5, tokens_out=1)  # the main model does not exist for this key: its sibling answers
    with caplog.at_level(logging.INFO, logger="src.llm.gateway"):
        ask(gateway, clock)
    lines = [record.getMessage() for record in caplog.records if record.getMessage().startswith("model call")]
    assert lines[-1].endswith("tokens_in=5 tokens_out=1 fallback=True trace_id=-") and f"model={LITE}" in lines[-1]
