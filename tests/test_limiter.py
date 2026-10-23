"""RateLimiter (plan 04, NFR9, S12): at most `rpm` requests in any minute, never waiting past the turn's deadline."""
import pytest

from src.config import LlmConfig, RateLimit
from src.errors import AgentError, ErrorKind
from src.llm.budget import TurnBudget
from src.llm.fake import FakeClock
from src.llm.limiter import RateLimiter


def make_limiter(clock: FakeClock, rpm: int = 6, **limits: int) -> RateLimiter:
    return RateLimiter(rpm, limits, clock=clock, sleep=clock.sleep)


def test_a_burst_up_to_the_minute_allowance_goes_through_without_waiting():
    clock = FakeClock()
    limiter = make_limiter(clock, rpm=6)
    assert [limiter.acquire("m") for _ in range(6)] == [0.0] * 6
    assert clock.sleeps == []


def test_the_next_request_waits_until_the_oldest_is_a_minute_old():
    clock = FakeClock()
    limiter = make_limiter(clock, rpm=6)
    for _ in range(6):
        limiter.acquire("m")
    clock.advance(20)
    assert limiter.acquire("m") == pytest.approx(40.0)  # 60 s after the first, not a refill rate later
    assert clock.sleeps == [pytest.approx(40.0)]


def test_once_the_wait_is_over_the_window_is_free_again():
    clock = FakeClock()
    limiter = make_limiter(clock, rpm=6)
    for _ in range(6):
        limiter.acquire("m")
    assert [limiter.acquire("m") for _ in range(3)] == [pytest.approx(60.0), 0.0, 0.0]  # the first waits, then 6 are 60 s old


def test_slots_free_up_a_minute_after_they_were_used():
    clock = FakeClock()
    limiter = make_limiter(clock, rpm=6)
    for _ in range(6):
        limiter.acquire("m")
    clock.advance(59)
    assert limiter.acquire("m") == pytest.approx(1.0)
    clock.advance(60)
    assert [limiter.acquire("m") for _ in range(6)] == [0.0] * 6


@pytest.mark.parametrize("rpm", [1, 3, 10])
def test_never_more_than_rpm_requests_in_any_minute(rpm):
    """A token bucket as big as the allowance let nearly 2 x rpm through in the first minute (review finding)."""
    clock = FakeClock()
    limiter = make_limiter(clock, rpm=rpm)
    granted = []
    for step in range(6 * rpm + 5):
        limiter.acquire("m")
        granted.append(clock.now)
        clock.advance(step % 4)  # requests arrive unevenly
    for start in granted:
        assert sum(1 for t in granted if start <= t < start + 60) <= rpm


def test_waiters_are_served_in_order():
    clock = FakeClock()
    limiter = make_limiter(clock, rpm=2)
    times = []
    for _ in range(7):
        limiter.acquire("m")
        times.append(clock.now - 1000)
    assert times == sorted(times) and times[-1] <= 180  # 7 requests at 2 a minute: the last one at the start of minute 4


def test_a_wait_that_would_pass_the_deadline_is_refused_at_once_and_takes_nothing():
    clock = FakeClock()
    limiter = make_limiter(clock, rpm=6)
    budget = TurnBudget(calls=8, seconds=5, bytes_=0, clock=clock)
    for _ in range(6):
        limiter.acquire("m", budget)
    with pytest.raises(AgentError) as caught:
        limiter.acquire("m", budget)  # 60 s away, 5 s left
    assert caught.value.kind is ErrorKind.LLM_RATE_LIMITED
    assert clock.sleeps == []  # it did not sleep
    clock.advance(60)
    assert limiter.acquire("m") == 0.0  # the refused request took no slot


def test_a_wait_that_leaves_less_than_a_second_for_the_call_is_refused_too():
    clock = FakeClock()
    limiter = make_limiter(clock, rpm=1)
    limiter.acquire("m")
    clock.advance(50.5)  # the next slot is 9.5 s away
    tight = TurnBudget(calls=8, seconds=10, bytes_=0, clock=clock)
    with pytest.raises(AgentError) as caught:
        limiter.acquire("m", tight)  # it would wake with 0.5 s to make a call
    assert caught.value.kind is ErrorKind.LLM_RATE_LIMITED and "needs 1 s" in caught.value.detail
    roomy = TurnBudget(calls=8, seconds=11, bytes_=0, clock=clock)
    assert limiter.acquire("m", roomy) == pytest.approx(9.5)


def test_no_wait_is_never_refused_for_lack_of_headroom():
    clock = FakeClock()
    limiter = make_limiter(clock, rpm=6)
    nearly_over = TurnBudget(calls=8, seconds=60, bytes_=0, clock=clock)
    clock.advance(59.5)
    assert limiter.acquire("m", nearly_over) == 0.0  # whether the turn can still make a call is the budget's to say


def test_without_a_budget_a_wait_over_the_cap_is_refused_too():
    clock = FakeClock()
    limiter = RateLimiter(1, clock=clock, sleep=clock.sleep, max_wait_s=30)
    limiter.acquire("m")
    with pytest.raises(AgentError) as caught:
        limiter.acquire("m")  # 60 s away
    assert caught.value.kind is ErrorKind.LLM_RATE_LIMITED


def test_each_model_has_its_own_window_and_rate():
    clock = FakeClock()
    limiter = make_limiter(clock, rpm=2, fast=60)
    limiter.acquire("slow")
    limiter.acquire("slow")
    assert limiter.acquire("fast") == 0.0  # the other model is unaffected
    assert limiter.rpm("fast") == 60 and limiter.rpm("slow") == 2 and limiter.capacity("fast") == 60


def test_it_reads_the_rates_from_config():
    llm = LlmConfig(default_rpm=10, rate_limits={"gemini-3.8-flash": RateLimit(rpm=15)})
    limiter = RateLimiter.from_config(llm)
    assert limiter.rpm("gemini-3.8-flash") == 15 and limiter.rpm("gemini-3.5-flash-lite") == 10
