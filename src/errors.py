"""Typed errors shared by every plan (decision 3.11).

`AgentError` is for problems inside a turn. It travels through the graph state as
data, and the CLI can always turn it into a friendly message with a trace ID.
`ConfigError` is for startup problems only (bad config, unknown user, no access).
"""
from __future__ import annotations

import traceback
import uuid
from enum import Enum
from pathlib import Path
from typing import NamedTuple

from src import pii


class ErrorKind(str, Enum):
    INPUT_REJECTED = "INPUT_REJECTED"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    INPUT_GUARD_FAILED = "INPUT_GUARD_FAILED"
    NO_ACCESS = "NO_ACCESS"
    ACCESS_STORE_DOWN = "ACCESS_STORE_DOWN"
    GUARD_VIOLATION = "GUARD_VIOLATION"
    RESULT_GUARD_FAILED = "RESULT_GUARD_FAILED"
    OUTPUT_BLOCKED = "OUTPUT_BLOCKED"
    SQL_SYNTAX = "SQL_SYNTAX"
    SQL_UNKNOWN_COLUMN = "SQL_UNKNOWN_COLUMN"
    EMPTY_RESULT = "EMPTY_RESULT"
    QUERY_TOO_EXPENSIVE = "QUERY_TOO_EXPENSIVE"
    QUERY_TIMEOUT = "QUERY_TIMEOUT"
    REPEATED_SQL = "REPEATED_SQL"
    REPEATED_ERROR = "REPEATED_ERROR"
    BQ_UNAVAILABLE = "BQ_UNAVAILABLE"
    LLM_RATE_LIMITED = "LLM_RATE_LIMITED"
    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
    LLM_BAD_OUTPUT = "LLM_BAD_OUTPUT"
    LLM_BLOCKED = "LLM_BLOCKED"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    DAILY_BUDGET_USED = "DAILY_BUDGET_USED"
    TURN_TIMEOUT = "TURN_TIMEOUT"
    NUMERIC_UNGROUNDED = "NUMERIC_UNGROUNDED"
    STALE_CONFIRMATION = "STALE_CONFIRMATION"
    TOOL_FAILED = "TOOL_FAILED"
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    INTERNAL = "INTERNAL"


class _Spec(NamedTuple):
    message: str
    retryable: bool = False   # transient: the same call may work if repeated
    repairable: bool = False  # the graph can loop back and fix the input (SQL repair, broaden)


# A refusal is not a failure: shown in a "not possible" panel and counted as `refused` in the metrics (F7, F32).
REFUSAL_KINDS = frozenset({ErrorKind.OUT_OF_SCOPE, ErrorKind.GUARD_VIOLATION, ErrorKind.INPUT_REJECTED, ErrorKind.DAILY_BUDGET_USED})

# Where a node falls back to what code can do instead of failing the turn.
LLM_FAILURE_KINDS = frozenset({
    ErrorKind.LLM_RATE_LIMITED, ErrorKind.LLM_UNAVAILABLE, ErrorKind.LLM_BAD_OUTPUT, ErrorKind.LLM_BLOCKED, ErrorKind.CIRCUIT_OPEN,
})  # the model could not answer this time
TURN_LIMIT_KINDS = frozenset({ErrorKind.BUDGET_EXHAUSTED, ErrorKind.TURN_TIMEOUT})  # the calls or the time of the turn are used up
MODEL_FALLBACK_KINDS = LLM_FAILURE_KINDS | TURN_LIMIT_KINDS  # no model call can be made now

_NARROW = "Try a narrower question, for example a shorter time range or one brand or category."

# One friendly sentence or two per kind. No internals, no stack traces (NFR19).
_SPECS: dict[ErrorKind, _Spec] = {
    ErrorKind.INPUT_REJECTED: _Spec("I couldn't use that message. Please type a question that is not empty and not too long."),
    ErrorKind.OUT_OF_SCOPE: _Spec("That is outside what I can help with. I answer questions about the store's orders, products, customers and sales."),
    ErrorKind.INPUT_GUARD_FAILED: _Spec("I couldn't safely check your message, so I did not run it. Please try again."),
    ErrorKind.NO_ACCESS: _Spec("No brands are assigned to your account, so I can't show you any data. Please ask an administrator for access."),
    ErrorKind.ACCESS_STORE_DOWN: _Spec("I can't confirm which brands you may see right now, so I stopped. Please try again later."),
    ErrorKind.GUARD_VIOLATION: _Spec("I can't run that request because it needs data or a query that is not allowed."),
    ErrorKind.RESULT_GUARD_FAILED: _Spec("I couldn't safely check the results, so I did not show them."),
    ErrorKind.OUTPUT_BLOCKED: _Spec("My answer failed a safety check, so I did not show it. Please rephrase or ask a narrower question."),
    ErrorKind.SQL_SYNTAX: _Spec("I wrote a query that did not work and could not fix it. Please try rephrasing your question.", repairable=True),
    ErrorKind.SQL_UNKNOWN_COLUMN: _Spec("I used a column that does not exist and could not fix it. Please try rephrasing your question.", repairable=True),
    ErrorKind.EMPTY_RESULT: _Spec("I found no data for that question, even after widening the search.", repairable=True),
    ErrorKind.QUERY_TOO_EXPENSIVE: _Spec(f"That question would scan too much data. {_NARROW}"),
    ErrorKind.QUERY_TIMEOUT: _Spec(f"That question took too long to run. {_NARROW}"),
    ErrorKind.REPEATED_SQL: _Spec("I kept running the same query, so I stopped instead of looping. Please try rephrasing your question."),
    ErrorKind.REPEATED_ERROR: _Spec("I kept hitting the same problem, so I stopped instead of looping. Please try rephrasing your question."),
    ErrorKind.BQ_UNAVAILABLE: _Spec("The data warehouse is not responding right now. Please try again in a minute.", retryable=True),
    ErrorKind.LLM_RATE_LIMITED: _Spec("I'm getting too many requests right now. Please wait a moment and try again.", retryable=True),
    ErrorKind.LLM_UNAVAILABLE: _Spec("The language model is not responding right now. Please try again in a minute.", retryable=True),
    ErrorKind.LLM_BAD_OUTPUT: _Spec("I got an unusable reply from the language model. Please try again.", retryable=True),
    ErrorKind.LLM_BLOCKED: _Spec("The language model declined to process that request. Please rephrase it."),
    ErrorKind.CIRCUIT_OPEN: _Spec("The language model service is paused after repeated failures. Please try again in a minute."),
    ErrorKind.BUDGET_EXHAUSTED: _Spec(f"I reached the limit of work I can do for one question, so I stopped. {_NARROW}"),
    ErrorKind.DAILY_BUDGET_USED: _Spec("Today's usage budget for your account is used up, so I did not run this question. It starts again at midnight UTC. Type /budget to see the use."),
    ErrorKind.TURN_TIMEOUT: _Spec(f"That took longer than the time limit, so I stopped. {_NARROW}"),
    ErrorKind.NUMERIC_UNGROUNDED: _Spec("I removed a number from my answer because I could not check it against the data."),
    ErrorKind.STALE_CONFIRMATION: _Spec("What I found changed after I showed you the preview, so nothing was changed. Here is the new preview."),
    ErrorKind.TOOL_FAILED: _Spec("That action failed and nothing was changed. Please try again. If it keeps failing, share the trace ID."),
    # The tool may still finish after we stopped waiting, so this does not say "nothing was changed". Only a retry of the same
    # request finds the first effect (the idempotency key holds the turn ID, Q23): the user's own retry in a later turn is a new
    # request with a new key, which is why the message tells them to look first.
    ErrorKind.TOOL_TIMEOUT: _Spec("That action took too long, and I can't tell whether it finished. Please check what changed before you try again."),
    ErrorKind.INTERNAL: _Spec("Something went wrong on my side. Your session is still open, so you can try again."),
}

_MAX_DETAIL = 300


def new_trace_id() -> str:
    """Short ID shown to the user and stored in the trace (8 hex characters)."""
    return uuid.uuid4().hex[:8]


def default_message(kind: ErrorKind) -> str:
    return _SPECS[kind].message


class AgentError(Exception):
    """A problem inside a turn. Always carries a friendly message."""

    def __init__(
        self,
        kind: ErrorKind,
        user_message: str | None = None,
        *,
        detail: str = "",
        trace_id: str | None = None,
        retryable: bool | None = None,
        repairable: bool | None = None,
        rule_id: str | None = None,
    ) -> None:
        spec = _SPECS[kind]
        self.kind = kind
        self.user_message = user_message or spec.message
        self.retryable = spec.retryable if retryable is None else retryable
        self.repairable = spec.repairable if repairable is None else repairable
        # Short and for traces only. Masked first, then cut: cutting first could leave half an email that no
        # pattern matches. Every reader of `detail` (trace, log, doctor) gets the masked text.
        self.detail = pii.mask(detail)[:_MAX_DETAIL]
        self.trace_id = trace_id
        self.rule_id = rule_id  # GUARD_VIOLATION carries the rule that fired (F32)
        super().__init__(f"{kind.value}: {self.detail or self.user_message}")

    def __reduce__(self):
        # Errors live in the graph state as data, so they must survive copy, deepcopy and pickle.
        return (_restore_agent_error, (self.kind, self.user_message, self.detail, self.trace_id,
                                       self.retryable, self.repairable, self.rule_id))

    @classmethod
    def from_exception(cls, exc: BaseException, trace_id: str | None = None) -> "AgentError":
        """Wrap any unexpected exception as INTERNAL. An AgentError passes through.

        The detail is the exception class and where it happened, never the exception text, which can
        carry data (a filter value, a path) that no masking pattern catches. The trace stores the same.
        """
        if isinstance(exc, AgentError):
            if exc.trace_id is None:
                exc.trace_id = trace_id
            return exc
        return cls(ErrorKind.INTERNAL, detail=crash_summary(exc), trace_id=trace_id)


def _restore_agent_error(kind, user_message, detail, trace_id, retryable, repairable, rule_id) -> AgentError:
    return AgentError(
        kind, user_message, detail=detail, trace_id=trace_id,
        retryable=retryable, repairable=repairable, rule_id=rule_id,
    )


def crash_summary(exc: BaseException) -> str:
    """Exception class and the innermost call sites, for logs. Never the exception text."""
    frames = traceback.extract_tb(exc.__traceback__)[-6:]
    where = " <- ".join(f"{Path(f.filename).name}:{f.lineno} in {f.name}" for f in reversed(frames))
    return f"{type(exc).__name__} at {where or 'unknown location'}"


def friendly(err: BaseException, trace_id: str | None = None) -> str:
    """The message the user sees: a friendly sentence plus the trace ID. Always has a trace ID.

    A trace ID already on the error wins, so the ID the user quotes is the one the trace was stored under.
    """
    agent_err = AgentError.from_exception(err, trace_id)
    if agent_err.trace_id is None:
        agent_err.trace_id = new_trace_id()
    return f"{agent_err.user_message}\nTrace ID: {agent_err.trace_id}"


class ConfigError(Exception):
    """A startup problem: bad config, unknown user, no access, failed brand check.

    Not an AgentError. The CLI prints `message` and `fix` and exits with code 2.
    """

    def __init__(self, message: str, fix: str | None = None) -> None:
        self.message = message
        self.fix = fix
        super().__init__(message)

    def __reduce__(self):
        return (ConfigError, (self.message, self.fix))
