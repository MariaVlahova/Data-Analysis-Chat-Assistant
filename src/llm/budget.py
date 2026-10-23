"""`TurnBudget`: what one turn may spend in model calls, seconds and BigQuery bytes (plan 04, S3, NFR4, F34).

Plan 07 makes one per turn with the simple limits and calls `upgrade()` once the Input Guard has said what kind of
turn it is. The gateway charges a call for every outbound attempt (retries and fallbacks included). `SafeQueryRunner`
reads `bytes_remaining`, `remaining_seconds()` and `check_time()` and calls `charge_bytes()`. Code ends a loop with
these limits, never the model (decision 3.10).
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Literal, Mapping, Protocol

from src.config import BudgetConfig, TurnLimits
from src.errors import AgentError, ErrorKind

TurnKind = Literal["simple", "why", "report"]

MIN_CALL_TIMEOUT_S = 0.1  # the least one call is given when the turn is almost out of time
MIN_USEFUL_CALL_S = 1.0  # a wait that leaves less than this for the call is not worth sleeping: a sibling can answer now


class _HasRemainingSeconds(Protocol):
    def remaining_seconds(self) -> float: ...


def call_timeout(configured_s: float, budget: _HasRemainingSeconds | None) -> float:
    """The time one outbound call (a model request, a BigQuery job) may take: the configured timeout, or what is left
    of the turn if that is less, and never less than `MIN_CALL_TIMEOUT_S`."""
    if budget is None:
        return configured_s
    return max(min(configured_s, budget.remaining_seconds()), MIN_CALL_TIMEOUT_S)


def fits_in_turn(wait_s: float, budget: _HasRemainingSeconds | None, limit_s: float | None = None) -> bool:
    """May the caller sleep `wait_s` and still have `MIN_USEFUL_CALL_S` left to make its call? Without a budget the
    wait is held to `limit_s` (None: any wait). No wait always fits: whether the turn can still make a call is for
    `TurnBudget.check_calls()` to say."""
    if wait_s <= 0:
        return True
    if budget is None:
        return limit_s is None or wait_s <= limit_s
    return wait_s + MIN_USEFUL_CALL_S <= budget.remaining_seconds()


class TurnBudget:
    def __init__(
        self,
        calls: int,
        seconds: float,
        bytes_: int,
        clock: Callable[[], float] = time.monotonic,
        *,
        limits: Mapping[str, TurnLimits] | None = None,
    ) -> None:
        """`limits` maps a turn kind to its calls and seconds, for `upgrade()`. Without it `upgrade()` is refused."""
        if calls < 0 or seconds <= 0 or bytes_ < 0:
            raise ValueError("a budget needs calls >= 0, seconds > 0 and bytes >= 0")
        self._clock = clock
        self._started = clock()
        self._calls_limit = calls
        self._seconds_limit = float(seconds)
        self._bytes_limit = bytes_
        self._calls_used = 0
        self._bytes_used = 0
        self._limits = dict(limits or {})
        self._lock = threading.Lock()  # sub-queries may run on worker threads (S12, loop.parallel_subqueries)
        self.kind: str = "simple"

    @classmethod
    def from_settings(cls, budget: BudgetConfig, clock: Callable[[], float] = time.monotonic) -> "TurnBudget":
        """A new turn: the simple limits, with the why and report limits ready for `upgrade()`."""
        return cls(
            budget.simple.calls, budget.simple.seconds, budget.bytes_per_turn, clock,
            limits={"simple": budget.simple, "why": budget.why, "report": budget.report},
        )

    # model calls ---------------------------------------------------------------------------------

    @property
    def calls_used(self) -> int:
        return self._calls_used

    @property
    def calls_limit(self) -> int:
        return self._calls_limit

    @property
    def calls_remaining(self) -> int:
        return max(self._calls_limit - self._calls_used, 0)

    def check_calls(self) -> None:
        """Raise if another call is not allowed. Charges nothing, so a caller can check before it waits for a rate limit."""
        self.check_time()
        if self._calls_used >= self._calls_limit:
            raise self._calls_error()

    def charge_call(self) -> None:
        """Count one outbound model call, or raise `TURN_TIMEOUT` / `BUDGET_EXHAUSTED` without counting it."""
        self.check_time()
        with self._lock:
            if self._calls_used >= self._calls_limit:
                raise self._calls_error()
            self._calls_used += 1

    def _calls_error(self) -> AgentError:
        return AgentError(
            ErrorKind.BUDGET_EXHAUSTED,
            detail=f"call {self._calls_used + 1} would pass the limit of {self._calls_limit} model calls for a {self.kind} turn",
        )

    # BigQuery bytes ------------------------------------------------------------------------------

    @property
    def bytes_remaining(self) -> int:
        return max(self._bytes_limit - self._bytes_used, 0)

    def charge_bytes(self, n: int) -> None:
        """Count the bytes a query was billed. The bytes are recorded either way; going over the limit raises
        `BUDGET_EXHAUSTED`, so the next query (whose dry-run gate sees `bytes_remaining`) cannot start."""
        with self._lock:
            self._bytes_used += max(int(n), 0)
            used, limit = self._bytes_used, self._bytes_limit
        if used > limit:
            raise AgentError(
                ErrorKind.BUDGET_EXHAUSTED, detail=f"{used} bytes billed passes the limit of {limit} bytes for this turn"
            )

    # time ----------------------------------------------------------------------------------------

    @property
    def elapsed_seconds(self) -> float:
        return self._clock() - self._started

    def remaining_seconds(self) -> float:
        return max(self._seconds_limit - self.elapsed_seconds, 0.0)

    def check_time(self) -> None:
        """Raise `TURN_TIMEOUT` once the turn has used its seconds."""
        elapsed = self.elapsed_seconds
        if elapsed >= self._seconds_limit:
            raise AgentError(
                ErrorKind.TURN_TIMEOUT, detail=f"the turn used {elapsed:.1f} s of its {self._seconds_limit:.0f} s"
            )

    # turn kind -----------------------------------------------------------------------------------

    def upgrade(self, kind: TurnKind) -> None:
        """Raise the limits for a why or report turn, once the Input Guard has classified it (S3).

        Limits only go up: a second call with a smaller kind changes nothing. The clock keeps running from the start
        of the turn, so the Input Guard call is already part of the seconds.
        """
        if kind not in ("simple", "why", "report"):
            raise ValueError(f"unknown turn kind {kind!r}")
        if kind not in self._limits:
            raise ValueError("this budget has no limits for upgrade(); create it with TurnBudget.from_settings()")
        wanted = self._limits[kind]
        with self._lock:
            if wanted.calls > self._calls_limit or wanted.seconds > self._seconds_limit:
                self.kind = kind
            self._calls_limit = max(self._calls_limit, wanted.calls)
            self._seconds_limit = max(self._seconds_limit, float(wanted.seconds))
