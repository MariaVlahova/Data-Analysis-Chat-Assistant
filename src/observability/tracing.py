"""Per-turn tracing: a short trace ID, spans for every step, model call, tool call and query (F40, F41).

Spans are OpenTelemetry-shaped (`trace_id, span_id, parent_id, name, kind, start_ts, end_ts,
duration_ms, status, error_kind, attrs, events`), so a later swap to the OTel SDK is mechanical.
One file per turn: `data/traces/<trace_id>.jsonl`, one span per line, written when the turn ends.

The PII rule (F44, S6) is enforced here, not left to callers:
- every string attribute is masked by `pii.mask` and cut to 2000 characters;
- a DataFrame, a list of rows or any other object is never stored, only its type name;
- exception text is never stored, only the error kind, the class and (for a crash) the call sites.

Nesting uses `contextvars`. A worker thread must run inside `contextvars.copy_context()` to see the
turn. Outside a turn every function here is a quiet no-op, so plan 03 to 08 code runs unchanged in
tests and in `doctor`.
"""
from __future__ import annotations

import decimal
import hashlib
import json
import logging
import math
import numbers
import os
import re
import secrets
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Mapping

from src import pii
from src.config import Settings
from src.errors import REFUSAL_KINDS, AgentError, ErrorKind, crash_summary, new_trace_id
from src.observability.metrics import GUARDS, Pricing, TurnMetrics, write_run_row
from src.scalars import plain

log = logging.getLogger(__name__)

SPAN_KINDS = ("step", "llm", "tool", "query", "guard", "hitl")
ROOT_KIND = "turn"
GUARD_OUTCOMES = ("pass", "rewrite", "block")

MAX_STRING = 2000
MAX_ITEMS = 50  # list items and dict keys kept per attribute
MAX_DEPTH = 3
MAX_SPANS = 500  # per turn: a runaway loop must not fill the disk
MAX_EVENTS = 100  # per span
_ID_PATTERN = r"[0-9a-f]{8}"  # exactly what `new_trace_id()` makes
TRACE_ID_RE = re.compile(_ID_PATTERN)


def valid_trace_id(value: object) -> str | None:
    """The ID (lower-cased) if it has the exact shape `new_trace_id()` makes, else None.

    The one gate between a typed or stored ID and a file name. The strict shape rules out path separators,
    a trailing newline (`fullmatch`, not `$`) and Windows device names such as CON, AUX and COM1, which
    would make a read block on the device.
    """
    if isinstance(value, str) and TRACE_ID_RE.fullmatch(value.lower()):
        return value.lower()
    return None


def trace_file(traces_dir: Path, trace_id: object) -> Path | None:
    """Where a turn's trace lives, or None for an ID that is not valid. The viewer uses this too."""
    safe = valid_trace_id(trace_id)
    return Path(traces_dir) / f"{safe}.jsonl" if safe else None


# attribute cleaning ----------------------------------------------------------------------------

def _text(value: str) -> str:
    masked = pii.mask(value)  # mask first: cutting first could leave half an email that no pattern matches
    return masked if len(masked) <= MAX_STRING else masked[: MAX_STRING - 1] + "…"


def clean(value: Any, _depth: int = 0) -> Any:
    """Make a value safe to store: masked strings, real numbers, no rows, no objects, no NaN."""
    if isinstance(value, Enum):
        value = value.value
    value = plain(value)
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, numbers.Integral):
        return int(value)
    if isinstance(value, (numbers.Real, decimal.Decimal)):
        try:
            number = float(value)
        except (ValueError, OverflowError):
            return None
        return number if math.isfinite(number) else None
    if isinstance(value, str):
        return _text(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, os.PathLike):
        return _text(os.fspath(value))
    if isinstance(value, Mapping):
        if _depth >= MAX_DEPTH:
            return "<mapping>"
        return {_text(str(key)): clean(item, _depth + 1) for key, item in list(value.items())[:MAX_ITEMS]}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = sorted(value, key=str) if isinstance(value, (set, frozenset)) else list(value)
        if any(isinstance(item, (Mapping, list, tuple, set, frozenset)) for item in items):
            return f"<{len(items)} nested items withheld>"  # a list of rows is data at rest
        kept = [clean(item, _depth + 1) for item in items[:MAX_ITEMS]]
        if len(items) > MAX_ITEMS:
            kept.append(f"...(+{len(items) - MAX_ITEMS} more)")
        return kept
    return f"<{type(value).__name__}>"  # a DataFrame or any other object: its type, never its content


def prompt_attrs(template_id: str, version: str | int, text: str) -> dict[str, Any]:
    """Span attributes that describe a prompt without storing it (F41).

    The text is kept only when `--debug-prompts` is on, and then still masked.
    """
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:8]  # "replace": a lone surrogate must not raise
    attrs: dict[str, Any] = {
        "prompt_id": template_id,
        "prompt_version": f"{template_id}@{version}+{digest}",
        "prompt_hash": digest,
        "prompt_chars": len(text),
    }
    ctx = _TRACE.get()
    if ctx is not None and ctx.tracer.debug_prompts:
        attrs["prompt_text"] = text
    return attrs


# spans -----------------------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class Span:
    trace_id: str
    span_id: str
    parent_id: str | None
    name: str
    kind: str
    start_ts: str = field(default_factory=_now_iso)
    end_ts: str | None = None
    duration_ms: float | None = None
    status: str = "ok"
    error_kind: str | None = None
    attrs: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    _started: float = field(default_factory=time.perf_counter, repr=False)

    def set(self, **attrs: Any) -> None:
        """Add attributes, cleaned and masked."""
        for key, value in attrs.items():
            self.attrs[key] = clean(value)

    def event(self, name: str, **attrs: Any) -> None:
        if len(self.events) >= MAX_EVENTS:
            self.attrs["events_dropped"] = self.attrs.get("events_dropped", 0) + 1
            return
        self.events.append({"name": name, "ts": _now_iso(), "attrs": {k: clean(v) for k, v in attrs.items()}})

    def finish(self, status: str | None = None, error_kind: str | None = None) -> None:
        if self.end_ts is not None:
            return
        self.end_ts = _now_iso()
        self.duration_ms = round((time.perf_counter() - self._started) * 1000, 1)
        if status:
            self.status, self.error_kind = status, error_kind or self.error_kind

    def to_record(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "name": self.name,
            "kind": self.kind,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "error_kind": self.error_kind,
            "attrs": self.attrs,
            "events": self.events,
        }


@dataclass
class _TraceContext:
    trace_id: str
    metrics: TurnMetrics
    tracer: "Tracer"
    spans: list[Span] = field(default_factory=list)
    used_ids: set[str] = field(default_factory=set)
    dropped_spans: int = 0
    seen_errors: list[BaseException] = field(default_factory=list)  # one error passes through every open span

    def new_span(self, name: str, kind: str, parent: Span | None, attrs: Mapping[str, Any]) -> Span:
        stored = len(self.spans) < MAX_SPANS
        if stored:
            span_id = secrets.token_hex(2)
            while span_id in self.used_ids:
                span_id = secrets.token_hex(2)
            self.used_ids.add(span_id)
        else:
            span_id = "0000"  # a dropped span takes no ID: a runaway loop must not use them all up
            self.dropped_spans += 1  # still returned, so the caller's code runs; it is just not stored
        span = Span(self.trace_id, span_id, parent.span_id if parent else None, name, kind)
        span.set(**attrs)
        if stored:
            self.spans.append(span)
        return span


_TRACE: ContextVar[_TraceContext | None] = ContextVar("opsfleet_trace", default=None)
_SPAN: ContextVar[Span | None] = ContextVar("opsfleet_span", default=None)


def describe_error(exc: BaseException) -> tuple[str, str]:
    """(error kind, short detail) for a span. Never the exception text: it can carry data."""
    if isinstance(exc, AgentError):
        return exc.kind.value, exc.detail
    if isinstance(exc, (KeyboardInterrupt, GeneratorExit)):
        return "INTERRUPTED", ""
    return ErrorKind.INTERNAL.value, crash_summary(exc)


def is_control_flow(exc: BaseException) -> bool:
    """LangGraph pauses and hands over by raising: `interrupt()` raises GraphInterrupt, a subclass of
    GraphBubbleUp. That is the HITL gate waiting for the user, not a failure. Matched by class name so
    this module does not import langgraph."""
    return any(cls.__name__ == "GraphBubbleUp" for cls in type(exc).__mro__)


def _mark_error(ctx: _TraceContext | None, span: Span, exc: BaseException) -> None:
    if is_control_flow(exc):
        span.set(paused=type(exc).__name__)  # not an error: status stays ok and no error is counted
        return
    kind, detail = describe_error(exc)
    span.status, span.error_kind = "error", kind
    first_sight = ctx is None or not any(seen is exc for seen in ctx.seen_errors)
    if not first_sight:
        return  # a parent of the span that failed: status and kind are enough
    span.set(error_type=type(exc).__name__, error_detail=detail)
    if isinstance(exc, AgentError) and exc.rule_id:
        span.set(rule=exc.rule_id)
    if ctx is not None:
        ctx.seen_errors.append(exc)
        if kind in ErrorKind.__members__:
            ctx.metrics.add_error(kind)


@contextmanager
def span(name: str, kind: str = "step", **attrs: Any) -> Iterator[Span]:
    """A span inside the current one. Records the error and re-raises if the body fails.

    `name` and `kind` are parameters, so they cannot also be attribute names.
    """
    if kind not in SPAN_KINDS:
        raise ValueError(f"unknown span kind {kind!r}, expected one of {SPAN_KINDS}")
    ctx = _TRACE.get()
    parent = _SPAN.get()
    if ctx is not None:
        current = ctx.new_span(name, kind, parent, attrs)
    else:  # outside a turn: a throwaway span, so `with span(...) as s: s.set(...)` still works
        current = Span("", "0000", None, name, kind)
        current.set(**attrs)
    token = _SPAN.set(current)
    try:
        yield current
    except BaseException as exc:
        _mark_error(ctx, current, exc)
        raise
    finally:
        current.finish()
        _SPAN.reset(token)


def event(name: str, **attrs: Any) -> None:
    """A point-in-time record inside the current span."""
    current = _SPAN.get()
    if current is not None:
        current.event(name, **attrs)


def guard_decision(guard: str, outcome: str, rule: str | None = None, **detail: Any) -> None:
    """Event `guard.decision`. A `block` also counts in the turn's guard-block counters (F42)."""
    if guard not in GUARDS:
        raise ValueError(f"unknown guard {guard!r}, expected one of {GUARDS}")
    if outcome not in GUARD_OUTCOMES:
        raise ValueError(f"unknown guard outcome {outcome!r}, expected one of {GUARD_OUTCOMES}")
    event("guard.decision", guard=guard, outcome=outcome, rule=rule, **detail)
    if outcome == "block":
        turn_metrics().add_guard_block(guard)


def current_trace_id() -> str | None:
    """For `friendly()` error messages and BigQuery labels. None outside a turn."""
    ctx = _TRACE.get()
    return ctx.trace_id if ctx else None


def turn_metrics() -> TurnMetrics:
    """The running turn's counters. Outside a turn: a throwaway object, so callers never check."""
    ctx = _TRACE.get()
    return ctx.metrics if ctx else TurnMetrics()


# the tracer ------------------------------------------------------------------------------------

class TurnTrace:
    """What `start_turn` hands to the caller. `row` is filled once the turn has ended."""

    def __init__(self, trace_id: str, metrics: TurnMetrics, root: Span) -> None:
        self.trace_id = trace_id
        self.metrics = metrics
        self.root = root
        self.row: dict[str, Any] | None = None

    def set_turn_id(self, turn_id: str) -> None:
        """For a caller that learns the turn number only inside the trace (the number comes from the database)."""
        self.metrics.turn_id = turn_id
        self.root.set(turn_id=turn_id)


def default_outcome(error: BaseException | None) -> str:
    """The outcome when nobody set one: a clean exit (or a LangGraph pause) is answered, a refusal kind is
    refused, the rest failed. Plan 07 and 08 set `partial`, `action_done` and `action_cancelled` themselves."""
    if error is None or is_control_flow(error):
        return "answered"
    if isinstance(error, AgentError) and error.kind in REFUSAL_KINDS:
        return "refused"
    return "failed"


class Tracer:
    def __init__(
        self,
        traces_dir: Path,
        metrics_file: Path,
        *,
        pricing: Pricing | None = None,
        debug_prompts: bool = False,
    ) -> None:
        self.traces_dir = Path(traces_dir)
        self.metrics_file = Path(metrics_file)
        self.pricing = pricing
        self.debug_prompts = debug_prompts

    @classmethod
    def from_settings(cls, settings: Settings, *, debug_prompts: bool = False) -> "Tracer":
        prices = settings.llm.pricing_usd_per_1m or {}
        return cls(
            settings.traces_path,
            settings.metrics_path,
            pricing={model: (price.in_, price.out) for model, price in prices.items()},
            debug_prompts=debug_prompts,
        )

    def trace_path(self, trace_id: str) -> Path | None:
        return trace_file(self.traces_dir, trace_id)

    @contextmanager
    def start_turn(
        self,
        user_hash: str,
        conversation_id: str,
        turn_id: str,
        *,
        trace_id: str | None = None,
        question: str | None = None,
        chaos: bool = False,
        persona_version: str | None = None,
    ) -> Iterator[TurnTrace]:
        """Create the trace ID and the `turn` root span. Always writes the trace file and the metrics row,
        even when the turn raises (the `finally`): the turns you must debug are the ones that crash.

        Pass the trace ID the user was already given, so the ID in an error message is the stored one.
        """
        tid = valid_trace_id(trace_id) or new_trace_id()
        metrics = TurnMetrics(
            trace_id=tid, user_hash=user_hash, conversation_id=conversation_id, turn_id=turn_id, chaos=chaos
        )
        metrics.set_persona_version(persona_version)
        ctx = _TraceContext(tid, metrics, self)
        root = ctx.new_span(
            "turn", ROOT_KIND, None,
            {"user_hash": user_hash, "conversation_id": conversation_id, "turn_id": turn_id,
             "question": question, "chaos": chaos, "persona_version": metrics.persona_version},
        )
        handle = TurnTrace(tid, metrics, root)
        trace_token, span_token = _TRACE.set(ctx), _SPAN.set(root)
        error: BaseException | None = None
        try:
            yield handle
        except BaseException as exc:
            error = exc
            _mark_error(ctx, root, exc)
            raise
        finally:
            try:
                self._finish(ctx, handle, error)
            except Exception as exc:  # noqa: BLE001 - observability must never break the turn it observes
                log.error("could not write the trace for %s: %s", tid, crash_summary(exc))
            finally:
                _SPAN.reset(span_token)
                _TRACE.reset(trace_token)

    def _finish(self, ctx: _TraceContext, handle: TurnTrace, error: BaseException | None) -> None:
        metrics, root = ctx.metrics, handle.root
        if metrics.outcome is None:
            metrics.outcome = default_outcome(error)
        root.finish()
        root.set(intent=metrics.intent, turn_kind=metrics.turn_kind, outcome=metrics.outcome,
                 persona_version=metrics.persona_version)
        if ctx.dropped_spans:
            root.set(spans_dropped=ctx.dropped_spans)
        for span_ in ctx.spans:
            if span_.end_ts is None:
                span_.finish("error", "UNFINISHED")

        row = metrics.to_row(ts=root.end_ts or _now_iso(), latency_ms=root.duration_ms or 0.0, pricing=self.pricing)
        handle.row = row
        log.info(
            "turn done trace_id=%s outcome=%s llm_calls=%d tokens_in=%d tokens_out=%d bq_queries=%d bytes_billed=%d latency_ms=%.0f",
            ctx.trace_id, metrics.outcome, metrics.llm_calls, metrics.tokens_in, metrics.tokens_out, metrics.bq_queries, metrics.bytes_billed,
            root.duration_ms or 0.0,
        )
        if metrics.pii_at_output > 0:
            log.critical("ALERT pii_at_output=%d trace_id=%s", metrics.pii_at_output, ctx.trace_id)

        # Both writes are tried whatever the first one raises (an OSError, but also an encoding or JSON
        # error): a missing trace is better than a missing row.
        failures: list[str] = []
        try:
            self._write_trace(ctx)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"trace file ({type(exc).__name__})")
        try:
            write_run_row(self.metrics_file, row)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"metrics row ({type(exc).__name__})")
        if failures:
            log.error("could not write %s for %s", " and ".join(failures), ctx.trace_id)

    def _write_trace(self, ctx: _TraceContext) -> None:
        path = self.trace_path(ctx.trace_id)
        if path is None:  # cannot happen: the ID was checked in start_turn
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        text = "".join(
            json.dumps(span_.to_record(), ensure_ascii=False, separators=(",", ":"), allow_nan=False,
                       default=lambda obj: f"<{type(obj).__name__}>") + "\n"
            for span_ in ctx.spans
        )
        temp = path.with_name(path.name + ".tmp")
        try:
            temp.write_text(text, encoding="utf-8", newline="\n")
            os.replace(temp, path)  # a reader never sees half a trace
        except BaseException:
            try:
                temp.unlink(missing_ok=True)  # a half-written masked trace must not outlive the retention limit
            except OSError:
                pass  # the purge deletes old *.jsonl.tmp files as well
            raise


# retention -------------------------------------------------------------------------------------

# A trace file, or the temp file of a write that failed. Built from the same ID shape as `valid_trace_id`,
# so `runs.jsonl` (or anything else) in the same folder is never matched.
_TRACE_FILE_RE = re.compile(rf"{_ID_PATTERN}\.jsonl(?:\.tmp)?")


def purge_old_traces(traces_dir: Path, days: int, *, now: datetime | None = None) -> int:
    """Delete trace files older than `days` (NFR22, Q25 default 90). Returns how many were removed.

    Only files named like a trace are touched. `runs.jsonl` rows carry no PII and are kept.
    """
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=days)).timestamp()
    removed = 0
    try:
        candidates = list(Path(traces_dir).iterdir())
    except OSError:
        return 0  # no folder yet, or not readable: nothing to purge
    for path in candidates:
        if not _TRACE_FILE_RE.fullmatch(path.name):
            continue
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue  # in use or already gone: the next start tries again
    return removed
