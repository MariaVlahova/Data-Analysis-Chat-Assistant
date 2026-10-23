"""A rate limiter per model, over a sliding minute, filled from `llm.rate_limits.<model>.rpm` (plan 04, NFR9, S12).

At most `rpm` requests are admitted in any 60 seconds. A burst of `rpm` goes through at once; the next request waits
until the oldest one is a minute old. (A token bucket as big as the minute's allowance would admit nearly twice that in
the first minute, so the configured limit would not hold.)

Google does not publish the free-tier numbers in our sources, so the default is deliberately low and is raised from
the AI Studio limits page. A request takes a slot or waits for one, but never past the turn's deadline: it then
raises `LLM_RATE_LIMITED` at once instead of sleeping (the gateway may still ask the sibling model).
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable, Mapping, Protocol

from src.config import LlmConfig
from src.errors import AgentError, ErrorKind
from src.llm.budget import MIN_USEFUL_CALL_S, fits_in_turn

WINDOW_S = 60.0
MAX_WAIT_WITHOUT_BUDGET_S = 60.0  # a caller with no turn budget (doctor, the offline judge) still never waits forever


class _HasRemainingSeconds(Protocol):
    def remaining_seconds(self) -> float: ...


class RateLimiter:
    def __init__(
        self,
        default_rpm: int,
        rate_limits: Mapping[str, int] | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        max_wait_s: float = MAX_WAIT_WITHOUT_BUDGET_S,
    ) -> None:
        if default_rpm < 1:
            raise ValueError("default_rpm must be 1 or more")
        self._default_rpm = default_rpm
        self._rpm = dict(rate_limits or {})
        self._clock = clock
        self._sleep = sleep
        self._max_wait_s = max_wait_s
        self._grants: dict[str, deque[float]] = {}  # when each recent request was (or will be) let through, oldest first
        self._lock = threading.Lock()

    @classmethod
    def from_config(cls, llm: LlmConfig, **kwargs) -> "RateLimiter":
        return cls(llm.default_rpm, {model: limit.rpm for model, limit in llm.rate_limits.items()}, **kwargs)

    def rpm(self, model: str) -> int:
        return self._rpm.get(model, self._default_rpm)

    def capacity(self, model: str) -> int:
        """The burst size: how many requests go through at once. `loop.parallel_subqueries` is capped by it (S12)."""
        return self.rpm(model)

    def acquire(self, model: str, budget: _HasRemainingSeconds | None = None) -> float:
        """Take one slot for `model`, waiting for it if needed. Returns the seconds waited.

        If the wait would leave less than `MIN_USEFUL_CALL_S` of the turn for the call (or pass `max_wait_s` without
        a budget), raises `LLM_RATE_LIMITED` and takes nothing. A waiting caller has reserved its slot, so threads
        queue in order.
        """
        with self._lock:
            now = self._clock()
            grants = self._grants.setdefault(model, deque())
            while grants and grants[0] <= now - WINDOW_S:
                grants.popleft()
            rpm = self.rpm(model)
            at = now if len(grants) < rpm else max(now, grants[-rpm] + WINDOW_S)
            if grants:
                at = max(at, grants[-1])  # first come, first served
            wait = at - now
            if not fits_in_turn(wait, budget, self._max_wait_s):
                left = budget.remaining_seconds() if budget is not None else self._max_wait_s
                raise AgentError(
                    ErrorKind.LLM_RATE_LIMITED,
                    detail=f"{model} is at its limit of {rpm} requests per minute; "
                           f"the next slot is {wait:.1f} s away and only {max(left, 0.0):.1f} s are left"
                           + (f" (a call needs {MIN_USEFUL_CALL_S:g} s)" if budget is not None else ""),
                )
            grants.append(at)
        if wait > 0:
            self._sleep(wait)
        return wait
