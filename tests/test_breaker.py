"""CircuitBreaker (plan 04, decision 3.11): closed, open and half open, with an injected clock."""
from src.config import LlmBreaker
from src.llm.breaker import Breakers, CircuitBreaker, State
from src.llm.fake import FakeClock


def make(clock: FakeClock, threshold: int = 3, cooldown: float = 30.0) -> CircuitBreaker:
    return CircuitBreaker(threshold, cooldown, clock)


def test_three_failures_in_a_row_open_it():
    breaker = make(FakeClock())
    for _ in range(2):
        assert breaker.allow()
        breaker.record_failure()
    assert breaker.state is State.CLOSED and breaker.allow()
    breaker.record_failure()
    assert breaker.state is State.OPEN and not breaker.allow()


def test_a_success_resets_the_count():
    breaker = make(FakeClock())
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state is State.CLOSED  # two since the success, not four


def test_after_the_cooldown_one_probe_is_allowed_and_a_success_closes_it():
    clock = FakeClock()
    breaker = make(clock)
    for _ in range(3):
        breaker.record_failure()
    clock.advance(29.9)
    assert not breaker.allow() and 0 < breaker.retry_in() < 1
    clock.advance(0.2)
    assert breaker.state is State.HALF_OPEN
    assert breaker.allow()  # the probe
    assert not breaker.allow()  # only one at a time
    breaker.record_success()
    assert breaker.state is State.CLOSED and breaker.allow() and breaker.allow()


def test_a_failed_probe_opens_it_again_for_a_full_cooldown():
    clock = FakeClock()
    breaker = make(clock)
    for _ in range(3):
        breaker.record_failure()
    clock.advance(31)
    assert breaker.allow()
    breaker.record_failure()
    assert breaker.state is State.OPEN
    clock.advance(29)
    assert not breaker.allow()
    clock.advance(2)
    assert breaker.allow()


def test_a_probe_that_was_never_sent_can_be_given_back():
    clock = FakeClock()
    breaker = make(clock)
    for _ in range(3):
        breaker.record_failure()
    clock.advance(31)
    assert breaker.allow()
    breaker.release()
    assert breaker.allow()  # the slot is free again


def test_a_late_result_while_open_changes_nothing():
    breaker = make(FakeClock())
    for _ in range(3):
        breaker.record_failure()
    breaker.record_success()  # a call that started before it opened
    assert breaker.state is State.OPEN


def test_breakers_are_made_per_model_from_config():
    clock = FakeClock()
    board = Breakers(LlmBreaker(failure_threshold=1, cooldown_s=5), clock)
    board.get("a").record_failure()
    assert board.get("a").state is State.OPEN and board.get("b").state is State.CLOSED
    assert board.get("a") is board.get("a")
