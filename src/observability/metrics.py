"""One PII-free metrics row per turn in `runs.jsonl`, and the totals computed from those rows (F42, F43).

Rows hold counts, IDs (hashes only) and version tags. They never hold a question, an answer, SQL or a
cell value, so they are kept longer than the traces (plan 02), but not for ever: `purge_old_rows` removes
the rows older than `retention.metrics_days` at startup, from `runs.jsonl` and `evaluations.jsonl` alike.
"""
from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from src import pii
from src.fileutil import atomic_write_bytes

OUTCOMES = ("answered", "partial", "refused", "action_done", "action_cancelled", "failed")
# A correct refusal is not task success: it is counted as `refused` (impl).
SUCCESS_OUTCOMES = frozenset({"answered", "partial", "action_done", "action_cancelled"})
GUARDS = ("input", "sql", "result", "output", "numeric", "hitl")

# NFR7. Google's "1 TB per month" free query tier is 1 TiB (2**40 bytes).
FREE_TIER_BYTES_PER_MONTH = 1024 ** 4
FREE_TIER_WARN_FRACTION = 0.8

Pricing = Mapping[str, tuple[float, float]]  # model ID -> (USD per 1M tokens in, out)


def _count(value: int | None) -> int:
    return max(0, int(value or 0))


def _unique_append(items: list[str], value: str | None) -> None:
    if value and value not in items:
        items.append(value)


@dataclass
class TurnMetrics:
    """Counters for the turn that is running. Other plans reach it through `tracing.turn_metrics()`.

    One turn runs on one thread (A19), so nothing here is locked.
    """

    trace_id: str = ""
    user_hash: str = ""
    conversation_id: str = ""
    turn_id: str = ""
    chaos: bool = False
    intent: str | None = None
    turn_kind: str | None = None
    outcome: str | None = None
    error_kinds: list[str] = field(default_factory=list)
    guard_blocks: dict[str, int] = field(default_factory=lambda: dict.fromkeys(GUARDS, 0))
    pii_at_output: int = 0
    llm_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    fallback_used: bool = False
    bq_queries: int = 0
    bytes_billed: int = 0
    repairs: int = 0
    replans: int = 0
    subquestions: int = 0
    models: list[str] = field(default_factory=list)
    prompt_versions: list[str] = field(default_factory=list)
    persona_version: str = "none"
    _tokens_by_model: dict[str, list[int]] = field(default_factory=dict, repr=False)

    # recording -----------------------------------------------------------
    def set_outcome(self, outcome: str) -> None:
        if outcome not in OUTCOMES:
            raise ValueError(f"unknown outcome {outcome!r}, expected one of {OUTCOMES}")
        self.outcome = outcome

    def set_intent(self, intent: str | None, turn_kind: str | None = None) -> None:
        self.intent = intent or self.intent
        self.turn_kind = turn_kind or self.turn_kind

    def add_llm_call(
        self,
        model: str,
        *,
        tokens_in: int | None = 0,
        tokens_out: int | None = 0,
        fallback_used: bool = False,
        prompt_version: str | None = None,
    ) -> None:
        """Count one model call. Plan 04 calls this for every attempt, with the model actually used."""
        self.llm_calls += 1
        self.tokens_in += _count(tokens_in)
        self.tokens_out += _count(tokens_out)
        self.fallback_used = self.fallback_used or fallback_used
        _unique_append(self.models, model)
        _unique_append(self.prompt_versions, prompt_version)
        used = self._tokens_by_model.setdefault(model, [0, 0])
        used[0] += _count(tokens_in)
        used[1] += _count(tokens_out)

    def add_query(self, bytes_billed: int | None = 0) -> None:
        """Count one executed BigQuery query. A dry run is a span only: it bills nothing."""
        self.bq_queries += 1
        self.bytes_billed += _count(bytes_billed)

    def add_guard_block(self, guard: str) -> None:
        if guard not in self.guard_blocks:
            raise ValueError(f"unknown guard {guard!r}, expected one of {GUARDS}")
        self.guard_blocks[guard] += 1

    def add_error(self, kind: str) -> None:
        _unique_append(self.error_kinds, kind)

    def add_pii_at_output(self, count: int) -> None:
        """The Output Guard found `count` PII findings in the Composer draft. This must stay 0."""
        self.pii_at_output += _count(count)

    def add_repair(self) -> None:
        self.repairs += 1

    def add_replan(self) -> None:
        self.replans += 1

    def set_subquestions(self, count: int) -> None:
        self.subquestions = _count(count)

    def add_prompt_version(self, version: str | None) -> None:
        _unique_append(self.prompt_versions, version)

    def set_persona_version(self, label: str | None) -> None:
        self.persona_version = label or "none"

    # output --------------------------------------------------------------
    def estimated_cost(self, pricing: Pricing | None) -> float | None:
        """Tokens times configured prices. `None` when no prices are set, or a model used has no price:
        we never invent one. With prices set, a turn with no model call costs `0.0`."""
        if not pricing:
            return None
        total = 0.0
        for model, (tokens_in, tokens_out) in self._tokens_by_model.items():
            price = pricing.get(model)
            if price is None:
                return None
            total += tokens_in / 1_000_000 * price[0] + tokens_out / 1_000_000 * price[1]
        return round(total, 6)

    def to_row(self, *, ts: str, latency_ms: float, pricing: Pricing | None = None) -> dict[str, Any]:
        row: dict[str, Any] = {
            "ts": ts,
            "trace_id": self.trace_id,
            "user_hash": self.user_hash,
            "conversation_id": self.conversation_id,
            "turn_id": self.turn_id,
            "intent": self.intent,
            "turn_kind": self.turn_kind,
            "outcome": self.outcome,
            "success": self.outcome in SUCCESS_OUTCOMES,
            "error_kinds": list(self.error_kinds),
            "guard_blocks": dict(self.guard_blocks),
            "pii_at_output": self.pii_at_output,
            "latency_ms": round(latency_ms, 1),
            "llm_calls": self.llm_calls,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "fallback_used": self.fallback_used,
            "bq_queries": self.bq_queries,
            "bytes_billed": self.bytes_billed,
            "cost_usd": self.estimated_cost(pricing),
            "repairs": self.repairs,
            "replans": self.replans,
            "subquestions": self.subquestions,
            "models": list(self.models),
            "prompt_versions": list(self.prompt_versions),
            "persona_version": self.persona_version,
            "chaos": self.chaos,
        }
        return _masked(row)


def _masked(value: Any) -> Any:
    """Defence in depth: a row carries no free text, but every string still passes the shared masker."""
    if isinstance(value, str):
        return pii.mask(value)
    if isinstance(value, dict):
        return {key: _masked(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_masked(item) for item in value]
    return value


# file ------------------------------------------------------------------------------------------

def write_run_row(path: Path, row: Mapping[str, Any]) -> None:
    """Append one JSON line. One `write` call, so a crash never leaves half a row in the middle."""
    line = json.dumps(row, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(line + "\n")


def purge_old_rows(path: Path, days: int, *, now: datetime | None = None) -> int:
    """Delete the rows of a metrics file (`runs.jsonl`, `evaluations.jsonl`) whose `ts` is more than `days` old. Returns how many went.

    A line that cannot be dated (a torn or edited line, a row with no readable `ts`) is kept: nothing is deleted on a guess. The other
    lines are kept byte for byte, and a file with nothing to remove is not touched. The file is replaced in one step, and not at all when
    it grew after it was read (another chat appended a row): that purge is skipped and the next start tries again, so a row written
    at the same time is never lost to it. No file is nothing to purge; a file that cannot be read or replaced raises `OSError`.
    """
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return 0
    kept: list[bytes] = []
    removed = 0
    for line in data.split(b"\n"):  # "\n" only, as in `read_runs`
        if not line.strip():
            continue
        if _older_than(line, cutoff):
            removed += 1
        else:
            kept.append(line)
    if not removed or path.stat().st_size != len(data):
        return 0
    atomic_write_bytes(path, b"".join(line + b"\n" for line in kept))
    return removed


def _older_than(line: bytes, cutoff: datetime) -> bool:
    try:
        stamp = datetime.fromisoformat(json.loads(line)["ts"])
    except (ValueError, KeyError, TypeError):  # not JSON or not UTF-8 (both ValueError), not an object, no `ts`, or a `ts` that is no date
        return False
    return (stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)) < cutoff


def read_runs(path: Path) -> tuple[list[dict[str, Any]], int]:
    """All rows, and how many lines could not be read (a torn or edited line is skipped, not fatal).

    No file is an empty history. A file that exists but cannot be opened (locked, no permission) raises
    `OSError`: the caller says so instead of reporting an empty history.
    """
    rows: list[dict[str, Any]] = []
    skipped = 0
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return rows, skipped
    # Bad bytes (a write cut in the middle of a character) become U+FFFD and that one line is skipped.
    # Split on "\n" only: str.splitlines() also splits on U+2028, U+2029 and U+0085, which JSON allows unescaped.
    for line in data.decode("utf-8", errors="replace").split("\n"):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            skipped += 1
            continue
        if isinstance(row, dict):
            rows.append(row)
        else:
            skipped += 1
    return rows, skipped


# totals ----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class MetricsSummary:
    turns: int
    total_rows: int  # all rows in the file, before `last` is applied
    outcomes: dict[str, int]
    success_rate: float
    refusal_rate: float
    failure_rate: float
    guard_blocks: dict[str, int]
    pii_at_output_total: int
    pii_alert_trace_ids: list[str]  # turns whose row has pii_at_output > 0 (NFR20 stand-in)
    latency_p50_ms: float | None
    latency_p95_ms: float | None
    avg_llm_calls: float
    tokens_in: int
    tokens_out: int
    top_errors: list[tuple[str, int]]
    cost_usd: float | None  # sum of the priced turns, None when no turn was priced
    unpriced_turns: int  # turns whose row has no cost: no prices set, or a model without a price
    month: str  # YYYY-MM (UTC)
    bytes_billed_month: int  # across all users and the whole file, not only the last N rows
    free_tier_fraction: float
    free_tier_warning: bool
    skipped_lines: int = 0
    user_scoped: bool = False  # True when only one user's turns were counted


def percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile: the smallest value with at least `pct` percent of the values at or below it."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct * len(ordered) / 100))  # multiply first: 0.95 * 100 style rounding must not move the rank
    return ordered[rank - 1]


def _num(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return 0
    return value


def summarize(
    rows: list[dict[str, Any]],
    *,
    last: int | None = None,
    now: datetime | None = None,
    skipped_lines: int = 0,
    user_hash: str | None = None,
) -> MetricsSummary:
    """Totals and rates. With `user_hash`, only that user's turns are counted (then `last` applies to them).

    The month-to-date bytes always cover every user: the free tier is shared, and one number identifies nobody.
    """
    if last is not None and last < 1:
        raise ValueError("last must be 1 or more")
    month = (now or datetime.now(timezone.utc)).strftime("%Y-%m")
    all_rows = rows
    if user_hash is not None:
        rows = [row for row in rows if row.get("user_hash") == user_hash]
    window = rows[-last:] if last else rows
    turns = len(window)

    outcomes = Counter(str(row.get("outcome")) for row in window)
    blocks = dict.fromkeys(GUARDS, 0)
    errors: Counter[str] = Counter()
    pii_ids: list[str] = []
    cost_total, priced, unpriced = 0.0, 0, 0
    for row in window:
        row_blocks = row.get("guard_blocks")
        if isinstance(row_blocks, dict):
            for guard in GUARDS:
                blocks[guard] += int(_num(row_blocks.get(guard)))
        kinds = row.get("error_kinds")
        if isinstance(kinds, list):
            errors.update(str(kind) for kind in kinds)
        if _num(row.get("pii_at_output")) > 0:
            pii_ids.append(str(row.get("trace_id", "?")))
        cost = row.get("cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            cost_total += cost
            priced += 1
        else:
            unpriced += 1

    latencies = [_num(row.get("latency_ms")) for row in window if "latency_ms" in row]
    month_bytes = sum(
        int(_num(row.get("bytes_billed"))) for row in all_rows if str(row.get("ts", ""))[:7] == month
    )
    fraction = month_bytes / FREE_TIER_BYTES_PER_MONTH

    def rate(count: int) -> float:
        return count / turns if turns else 0.0

    return MetricsSummary(
        turns=turns,
        total_rows=len(rows),
        outcomes=dict(outcomes),
        success_rate=rate(sum(1 for row in window if row.get("success") is True)),
        refusal_rate=rate(outcomes["refused"]),
        failure_rate=rate(outcomes["failed"]),
        guard_blocks=blocks,
        pii_at_output_total=sum(int(_num(row.get("pii_at_output"))) for row in window),
        pii_alert_trace_ids=pii_ids,
        latency_p50_ms=percentile(latencies, 50),
        latency_p95_ms=percentile(latencies, 95),
        avg_llm_calls=sum(_num(row.get("llm_calls")) for row in window) / turns if turns else 0.0,
        tokens_in=int(sum(_num(row.get("tokens_in")) for row in window)),
        tokens_out=int(sum(_num(row.get("tokens_out")) for row in window)),
        top_errors=errors.most_common(5),
        cost_usd=round(cost_total, 6) if priced else None,
        unpriced_turns=unpriced,
        month=month,
        bytes_billed_month=month_bytes,
        free_tier_fraction=fraction,
        free_tier_warning=month_bytes >= FREE_TIER_WARN_FRACTION * FREE_TIER_BYTES_PER_MONTH,  # bytes, not the ratio: no rounding at the edge
        skipped_lines=skipped_lines,
        user_scoped=user_hash is not None,
    )
