"""TurnBudget (plan 04): calls, seconds and bytes per turn, and `upgrade()` once the turn kind is known."""
import pytest

from src.config import BudgetConfig
from src.errors import AgentError, ErrorKind
from src.llm.budget import MIN_CALL_TIMEOUT_S, TurnBudget, call_timeout, fits_in_turn
from src.llm.fake import FakeClock


def simple_budget(clock: FakeClock) -> TurnBudget:
    return TurnBudget.from_settings(BudgetConfig(), clock)


def test_the_ninth_call_of_a_simple_turn_is_refused_and_not_counted():
    budget = simple_budget(FakeClock())
    for _ in range(8):
        budget.charge_call()
    with pytest.raises(AgentError) as caught:
        budget.charge_call()
    assert caught.value.kind is ErrorKind.BUDGET_EXHAUSTED
    assert budget.calls_used == 8 and budget.calls_remaining == 0


def test_upgrade_why_raises_the_limit_to_20_calls():
    budget = simple_budget(FakeClock())
    for _ in range(8):
        budget.charge_call()
    budget.upgrade("why")
    for _ in range(12):
        budget.charge_call()
    assert budget.calls_used == 20
    with pytest.raises(AgentError) as caught:
        budget.charge_call()
    assert caught.value.kind is ErrorKind.BUDGET_EXHAUSTED


def test_upgrade_report_gives_90_seconds_and_never_lowers_a_limit():
    clock = FakeClock()
    budget = simple_budget(clock)
    budget.upgrade("report")
    clock.advance(80)
    budget.check_time()  # a simple turn would be over after 60 s
    budget.upgrade("simple")  # a smaller kind changes nothing
    assert budget.calls_limit == 20 and budget.remaining_seconds() == pytest.approx(10)


def test_the_clock_past_60_seconds_raises_a_turn_timeout():
    clock = FakeClock()
    budget = simple_budget(clock)
    clock.advance(59.9)
    budget.check_time()
    clock.advance(0.2)
    with pytest.raises(AgentError) as caught:
        budget.check_time()
    assert caught.value.kind is ErrorKind.TURN_TIMEOUT
    with pytest.raises(AgentError) as caught:
        budget.charge_call()  # a call is refused for the time before it is refused for the count
    assert caught.value.kind is ErrorKind.TURN_TIMEOUT
    assert budget.remaining_seconds() == 0.0


def test_the_seconds_run_from_the_start_of_the_turn_also_after_an_upgrade():
    clock = FakeClock()
    budget = simple_budget(clock)
    clock.advance(30)  # the Input Guard call took 30 s
    budget.upgrade("why")
    assert budget.remaining_seconds() == pytest.approx(30)


def test_check_calls_charges_nothing():
    budget = TurnBudget(calls=1, seconds=60, bytes_=0, clock=FakeClock())
    budget.check_calls()
    budget.check_calls()
    assert budget.calls_used == 0
    budget.charge_call()
    with pytest.raises(AgentError):
        budget.check_calls()


def test_bytes_are_recorded_and_going_over_raises():
    budget = TurnBudget(calls=1, seconds=60, bytes_=1000, clock=FakeClock())
    budget.charge_bytes(400)
    assert budget.bytes_remaining == 600
    with pytest.raises(AgentError) as caught:
        budget.charge_bytes(700)
    assert caught.value.kind is ErrorKind.BUDGET_EXHAUSTED
    assert budget.bytes_remaining == 0  # the bytes were billed, so they are counted


def test_upgrade_needs_limits_and_a_known_kind():
    plain = TurnBudget(calls=8, seconds=60, bytes_=0, clock=FakeClock())
    with pytest.raises(ValueError):
        plain.upgrade("why")
    with pytest.raises(ValueError):
        simple_budget(FakeClock()).upgrade("huge")  # type: ignore[arg-type]


def test_a_budget_fits_what_safe_query_runner_reads():
    """The runner's BudgetLike protocol: bytes_remaining (a property), charge_bytes, check_time, remaining_seconds."""
    budget = simple_budget(FakeClock())
    assert isinstance(budget.bytes_remaining, int)
    assert callable(budget.charge_bytes) and callable(budget.check_time) and isinstance(budget.remaining_seconds(), float)


def test_a_call_gets_the_configured_timeout_or_what_is_left_of_the_turn_but_never_less_than_the_floor():
    clock = FakeClock()
    budget = TurnBudget(calls=8, seconds=60, bytes_=0, clock=clock)
    assert call_timeout(30, None) == 30 and call_timeout(30, budget) == 30
    clock.advance(45)
    assert call_timeout(30, budget) == pytest.approx(15)
    clock.advance(14.99)
    assert call_timeout(30, budget) == MIN_CALL_TIMEOUT_S


def test_safe_query_runner_and_the_gateway_share_the_one_timeout_rule():
    import inspect

    from src.data_access import safe_runner
    from src.llm import gateway

    assert safe_runner.call_timeout is call_timeout is gateway.call_timeout
    assert "MIN_CALL_TIMEOUT_S" not in inspect.getsource(safe_runner).replace("call_timeout", "")  # no second copy of the floor


def test_a_wait_fits_only_if_a_useful_second_is_left_for_the_call():
    clock = FakeClock()
    budget = TurnBudget(calls=8, seconds=10, bytes_=0, clock=clock)
    assert fits_in_turn(8.9, budget) and not fits_in_turn(9.0001, budget) and not fits_in_turn(9.9, budget)
    assert fits_in_turn(0, budget)  # no wait always fits
    clock.advance(9.5)
    assert fits_in_turn(0, budget)  # even with the turn nearly over: the budget's own checks decide whether to call
    assert fits_in_turn(100, None) and fits_in_turn(30, None, 60) and not fits_in_turn(61, None, 60)
