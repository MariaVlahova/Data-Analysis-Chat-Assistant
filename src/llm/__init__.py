"""The Gemini gateway (plan 04): model routing, rate limit, retry, circuit breaker, fallback, turn budget and chaos.

Plans 07 to 10 import from here:

    from src.llm import LLMGateway, Step, TurnBudget, Chaos

The names load on first use. `import src.llm.chaos` (the CLI building a `Chaos` for a flag) and `import src.llm.budget`
(`SafeQueryRunner` clamping a timeout) do not load the Google SDK; only `src.llm.gateway` and `src.llm.fake` do.
"""
from __future__ import annotations

import importlib
from typing import Any

_EXPORTS = {
    "Breakers": "src.llm.breaker",
    "CircuitBreaker": "src.llm.breaker",
    "TurnBudget": "src.llm.budget",
    "Chaos": "src.llm.chaos",
    "Gateway": "src.llm.gateway",
    "LLMGateway": "src.llm.gateway",
    "LLMResult": "src.llm.gateway",
    "RateLimiter": "src.llm.limiter",
    "Router": "src.llm.routing",
    "Step": "src.llm.routing",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module 'src.llm' has no attribute {name!r}")
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value  # found once, then a plain attribute
    return value


def __dir__() -> list[str]:
    return sorted([*globals(), *_EXPORTS])
