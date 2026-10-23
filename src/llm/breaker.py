"""A circuit breaker per model (plan 04, decision 3.11, F35).

Closed: calls go through. After `failure_threshold` failures in a row it opens: the gateway skips that model at once
for `cooldown_s`. Then it is half open: one probe call is let through. A success closes it, a failure opens it again.

Only 429, 5xx and timeouts count as failures. A bad request, a safety block or unusable JSON still means the service
answered, so the gateway records those as successes. The clock is injected, so tests never sleep.
"""
from __future__ import annotations

import threading
import time
from enum import Enum
from typing import Callable

from src.config import LlmBreaker


class State(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(
        self, failure_threshold: int = 3, cooldown_s: float = 30.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        if failure_threshold < 1 or cooldown_s <= 0:
            raise ValueError("a breaker needs failure_threshold >= 1 and cooldown_s > 0")
        self._threshold = failure_threshold
        self._cooldown = cooldown_s
        self._clock = clock
        self._state = State.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._probing = False
        self._lock = threading.Lock()

    def _refresh(self) -> None:
        if self._state is State.OPEN and self._clock() - self._opened_at >= self._cooldown:
            self._state, self._probing = State.HALF_OPEN, False

    @property
    def state(self) -> State:
        with self._lock:
            self._refresh()
            return self._state

    def retry_in(self) -> float:
        """Seconds until the next probe is allowed, 0 when calls may go through."""
        with self._lock:
            self._refresh()
            if self._state is State.OPEN:
                return max(self._cooldown - (self._clock() - self._opened_at), 0.0)
            return 0.0

    def allow(self) -> bool:
        """May a call go out? In the half-open state this claims the one probe: the caller must then report
        `record_success`, `record_failure` or, if it never sent the call, `release`."""
        with self._lock:
            self._refresh()
            if self._state is State.CLOSED:
                return True
            if self._state is State.HALF_OPEN and not self._probing:
                self._probing = True
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            if self._state is State.OPEN:
                return  # a call that started before the breaker opened: it does not prove the service is back
            self._state, self._failures, self._probing = State.CLOSED, 0, False

    def record_failure(self) -> None:
        with self._lock:
            if self._state is State.OPEN:
                return
            self._failures += 1
            if self._state is State.HALF_OPEN or self._failures >= self._threshold:
                self._state, self._opened_at, self._probing = State.OPEN, self._clock(), False

    def release(self) -> None:
        """Give back a probe that was claimed but never sent (the budget or the rate limiter said no first)."""
        with self._lock:
            if self._state is State.HALF_OPEN:
                self._probing = False


class Breakers:
    """One breaker per model ID, made on first use from the config."""

    def __init__(self, config: LlmBreaker, clock: Callable[[], float] = time.monotonic) -> None:
        self._config = config
        self._clock = clock
        self._by_model: dict[str, CircuitBreaker] = {}
        self._lock = threading.Lock()

    def get(self, model: str) -> CircuitBreaker:
        with self._lock:
            breaker = self._by_model.get(model)
            if breaker is None:
                breaker = self._by_model[model] = CircuitBreaker(
                    self._config.failure_threshold, self._config.cooldown_s, self._clock
                )
            return breaker
