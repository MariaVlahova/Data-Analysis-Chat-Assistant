"""`--chaos`: seeded fault injection that shows the resilience (plan 04, F37, decision section 8).

Faults replace the call they hit, so a chaos run spends no Gemini or BigQuery quota. They go through the same retry,
breaker and fallback code as real faults: the gateway and `SafeQueryRunner` receive an exception here and handle it
like the one the library would have raised. Chaos never skips a guard: a corrupted SQL still goes through the SQL Guard.

Two modes:
- random: `Chaos.from_level("mild" | "harsh", seed)`. Mild is about 15% of calls, harsh about 40%. A run of faults is
  cut short (`max_consecutive`), so a turn can still finish. The same seed gives the same faults.
- scripted, for tests: `Chaos.scripted()` injects nothing until told: `fail_llm("rate_limit", times=3)`,
  `fail_bq()`, `corrupt_sql()`.

Plan 07 calls `begin_turn()` when it makes a turn's runtime: the bad-SQL fault happens at most once per turn, and
a run of faults never carries over from the last turn.

Imports of the Google libraries are inside the functions: the CLI builds a `Chaos` before it needs them.
"""
from __future__ import annotations

import random
import re
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Any

from src.llm.routing import Step

CHAOS_REPLY = "chaos: injected fault"  # the only text these exceptions carry
RETRY_DELAY_S = 1.0  # what an injected 429 asks the caller to wait


class LlmFault(str, Enum):
    RATE_LIMIT = "rate_limit"  # 429 with a retry delay
    UNAVAILABLE = "unavailable"  # 503
    TIMEOUT = "timeout"
    BAD_JSON = "bad_json"  # a reply that is not valid JSON


class BqFault(str, Enum):
    UNAVAILABLE = "unavailable"  # 503
    TIMEOUT = "timeout"


class SqlFault(str, Enum):
    SYNTAX = "syntax"  # a dangling ORDER BY: both sqlglot and BigQuery reject it
    UNKNOWN_COLUMN = "unknown_column"  # a column that does not exist


class MalformedReply(Exception):
    """Returned (not raised) by `maybe_fail_llm` for `BAD_JSON`: the call goes through, the reply is garbage. The
    gateway treats the text like any unusable model output, so the re-ask path runs."""

    text = "{ chaos: this is not valid json"


@dataclass(frozen=True)
class Profile:
    llm_rate: float
    bq_rate: float
    sql_rate: float  # chance per turn that the SQL writer's first query is corrupted
    max_consecutive: int  # model faults in a row before one clean call is forced
    bq_max_consecutive: int  # BigQuery faults in a row. BigQuery has no sibling to fall back to, so this stays below the
    #                          runner's attempts (`bigquery.transient_retries` 2 means 3): the last attempt always goes through


# The share of calls hit (impl). Both stay winnable: harsh with 3 model faults in a row opens a breaker but the sibling
# model answers, and a BigQuery call is never faulted on its last attempt.
PROFILES: dict[str, Profile] = {
    "mild": Profile(llm_rate=0.15, bq_rate=0.10, sql_rate=0.15, max_consecutive=2, bq_max_consecutive=2),
    "harsh": Profile(llm_rate=0.40, bq_rate=0.30, sql_rate=0.40, max_consecutive=3, bq_max_consecutive=2),
}

_LLM_MIX = ((LlmFault.RATE_LIMIT, 4), (LlmFault.UNAVAILABLE, 3), (LlmFault.TIMEOUT, 2), (LlmFault.BAD_JSON, 2))
_BQ_MIX = ((BqFault.UNAVAILABLE, 2), (BqFault.TIMEOUT, 1))
_SQL_MIX = ((SqlFault.SYNTAX, 1), (SqlFault.UNKNOWN_COLUMN, 1))

_SELECT = re.compile(r"\bSELECT\b(\s+(?:DISTINCT|ALL)\b)?\s+", re.IGNORECASE)
MISSING_COLUMN = "chaos_missing_column"


@dataclass
class _Scripted:
    fault: Any
    times: int
    step: Step | None = None  # None: any step


def _pick(rng: random.Random, mix: tuple) -> Any:
    return rng.choices([item for item, _ in mix], weights=[weight for _, weight in mix])[0]


def corrupt_sql(sql: str, fault: SqlFault) -> str:
    """The SQL with one mistake the repair loop can fix. Falls back to a syntax error when there is no SELECT to change."""
    if fault is SqlFault.UNKNOWN_COLUMN:
        match = _SELECT.search(sql)
        if match:
            return sql[: match.end()] + MISSING_COLUMN + ", " + sql[match.end():]
    return sql.rstrip().rstrip(";") + "\nORDER BY"


class Chaos:
    def __init__(self, profile: Profile | None = None, seed: int | None = None, level: str | None = None) -> None:
        self.profile = profile
        self.level = level
        self.seed = seed
        self._rng = random.Random(seed)
        self._lock = threading.Lock()
        self._script_llm: list[_Scripted] = []
        self._script_bq: list[_Scripted] = []
        self._script_sql: list[_Scripted] = []
        self._run_llm = 0  # injected faults in a row
        self._run_bq = 0
        self._sql_done_this_turn = False
        self.injected: list[str] = []  # what was injected, in order, for a test or a summary

    @classmethod
    def from_level(cls, level: str, seed: int | None = None) -> "Chaos":
        """A random profile. Without a seed one is drawn, and kept in `.seed` so the run can be replayed."""
        if level not in PROFILES:
            raise ValueError(f"unknown chaos level {level!r}, expected one of {', '.join(PROFILES)}")
        if seed is None:
            seed = random.SystemRandom().randrange(1, 10_000)
        return cls(PROFILES[level], seed, level)

    @classmethod
    def scripted(cls) -> "Chaos":
        """Injects nothing until a test queues faults."""
        return cls()

    # scripting (tests) -------------------------------------------------------------------------

    def fail_llm(self, fault: LlmFault | str = LlmFault.RATE_LIMIT, *, times: int = 1, step: Step | None = None) -> "Chaos":
        """Fail the next `times` model calls (of `step`, if given) with `fault`. Scripted faults come before random ones."""
        self._script_llm.append(_Scripted(LlmFault(fault), times, step))
        return self

    def fail_bq(self, fault: BqFault | str = BqFault.UNAVAILABLE, *, times: int = 1) -> "Chaos":
        self._script_bq.append(_Scripted(BqFault(fault), times))
        return self

    def corrupt_next_sql(self, fault: SqlFault | str = SqlFault.SYNTAX, *, times: int = 1) -> "Chaos":
        """Corrupt the next `times` queries the SQL writer returns (several in one turn: 'bad SQL always')."""
        self._script_sql.append(_Scripted(SqlFault(fault), times))
        return self

    # per turn ----------------------------------------------------------------------------------

    def begin_turn(self) -> None:
        with self._lock:
            self._sql_done_this_turn = False
            self._run_llm = self._run_bq = 0

    # injection ---------------------------------------------------------------------------------

    def _take_script(self, queue: list[_Scripted], step: Step | None = None) -> Any:
        for item in queue:
            if item.times > 0 and (item.step is None or item.step == step):
                item.times -= 1
                if item.times == 0:
                    queue.remove(item)
                return item.fault
        return None

    def _roll(self, rate: float, run: int, cap: int) -> bool:
        return run < cap and self._rng.random() < rate

    def maybe_fail_llm(self, step: Step) -> Exception | None:
        """The exception for this model call to fail with, or None. A `MalformedReply` means 'answer with garbage'."""
        with self._lock:
            fault = self._take_script(self._script_llm, step)
            if fault is None and self.profile is not None:
                if self._roll(self.profile.llm_rate, self._run_llm, self.profile.max_consecutive):
                    fault = _pick(self._rng, _LLM_MIX)
            self._run_llm = self._run_llm + 1 if fault is not None else 0
            if fault is None:
                return None
            self.injected.append(f"llm:{fault.value}")
        return _llm_exception(fault)

    def maybe_corrupt_sql(self, sql: str) -> str:
        """The SQL writer's output, or a broken copy of it. Random mode corrupts at most one query per turn."""
        with self._lock:
            fault = self._take_script(self._script_sql)
            if fault is None and self.profile is not None and not self._sql_done_this_turn:
                if self._rng.random() < self.profile.sql_rate:
                    fault = _pick(self._rng, _SQL_MIX)
            if fault is None:
                return sql
            self._sql_done_this_turn = True
            self.injected.append(f"sql:{fault.value}")
        return corrupt_sql(sql, fault)

    def maybe_fail_bq(self) -> Exception | None:
        """The exception for this BigQuery call to fail with, or None. `SafeQueryRunner` classifies it like a real one."""
        with self._lock:
            fault = self._take_script(self._script_bq)
            if fault is None and self.profile is not None:
                if self._roll(self.profile.bq_rate, self._run_bq, self.profile.bq_max_consecutive):
                    fault = _pick(self._rng, _BQ_MIX)
            self._run_bq = self._run_bq + 1 if fault is not None else 0
            if fault is None:
                return None
            self.injected.append(f"bq:{fault.value}")
        return _bq_exception(fault)


def _llm_exception(fault: LlmFault) -> Exception:
    if fault is LlmFault.BAD_JSON:
        error: Exception = MalformedReply()
    elif fault is LlmFault.RATE_LIMIT:
        from google.genai import errors

        error = errors.ClientError(429, {"error": {
            "code": 429, "message": CHAOS_REPLY, "status": "RESOURCE_EXHAUSTED",
            "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": f"{RETRY_DELAY_S:g}s"}],
        }})
    elif fault is LlmFault.UNAVAILABLE:
        from google.genai import errors

        error = errors.ServerError(503, {"error": {"code": 503, "message": CHAOS_REPLY, "status": "UNAVAILABLE"}})
    else:
        import httpx

        error = httpx.ReadTimeout(CHAOS_REPLY)
    error.chaos_fault = fault.value  # type: ignore[attr-defined]
    return error


def _bq_exception(fault: BqFault) -> Exception:
    from google.api_core import exceptions as api_exc

    error: Exception = api_exc.ServiceUnavailable(CHAOS_REPLY) if fault is BqFault.UNAVAILABLE else TimeoutError(CHAOS_REPLY)
    error.chaos_fault = fault.value  # type: ignore[attr-defined]
    return error
