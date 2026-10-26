"""Token use per user and the daily budget (NFR8, Q15).

`UsageLedger` keeps one row per turn of the chat and one per turn that `/eval` judged (migration `004`): the user hash, the source, the trace ID
and counts. Never text. The day is a UTC day. `DailyBudget` reads the ledger against `budget.daily_tokens_per_user`: no cap is the default (the
amount is the client's open question Q15), and then the budget only counts.

The cap is checked before a turn starts, so the turn that crosses it still finishes: a turn is the unit of work, and stopping one halfway would
leave a half answer. `/budget clear` adds a row to `budget_clears` and writes an audit row in the same transaction; it never deletes a usage row,
so what was spent stays on record and only the count that the cap looks at starts again.

Like `AuditLog`, it opens a short-lived connection for each call, so it is safe to use from any thread.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal

from src.config import Settings
from src.errors import AgentError, ErrorKind
from src.storage.audit import AuditEntry, write_entry
from src.storage.db import iso, now_utc, open_connection

Source = Literal["chat", "eval"]
SOURCES: tuple[str, ...] = ("chat", "eval")
ClearedBy = Literal["user", "operator"]


@dataclass(frozen=True)
class Totals:
    entries: int = 0  # turns
    calls: int = 0  # model calls
    tokens_in: int = 0
    tokens_out: int = 0

    @property
    def tokens(self) -> int:
        return self.tokens_in + self.tokens_out


@dataclass(frozen=True)
class DayUsage:
    day: str  # YYYY-MM-DD, UTC
    chat: Totals
    eval: Totals
    cleared_at: str | None = None  # when today's count was last cleared, if it was

    @property
    def tokens(self) -> int:
        return self.chat.tokens + self.eval.tokens


@dataclass(frozen=True)
class BudgetStatus:
    usage: DayUsage
    cap: int | None  # None: no cap is set

    @property
    def used(self) -> int:
        return self.usage.tokens

    @property
    def fraction(self) -> float | None:
        return None if self.cap is None else self.used / self.cap

    @property
    def exhausted(self) -> bool:
        return self.cap is not None and self.used >= self.cap


class UsageLedger:
    def __init__(self, path: Path, now: Callable[[], datetime] | None = None) -> None:
        self.path = Path(path)
        self._now = now or now_utc

    @classmethod
    def from_settings(cls, settings: Settings, now: Callable[[], datetime] | None = None) -> "UsageLedger":
        return cls(settings.db_path, now)

    def _day_start(self) -> datetime:
        return self._now().astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    def record(
        self, user_hash: str, source: Source, *, tokens_in: int, tokens_out: int, calls: int = 0, trace_id: str | None = None,
    ) -> None:
        """Add what one turn spent. Nothing is written for a turn that spent nothing (a refusal before any model call)."""
        tokens_in, tokens_out, calls = max(int(tokens_in or 0), 0), max(int(tokens_out or 0), 0), max(int(calls or 0), 0)
        if source not in SOURCES:
            raise ValueError(f"unknown usage source {source!r}, expected one of {SOURCES}")
        if not (tokens_in or tokens_out or calls):
            return
        with closing(open_connection(self.path)) as conn:
            conn.execute(
                "INSERT INTO token_usage (ts, user_hash, source, trace_id, calls, tokens_in, tokens_out) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (iso(self._now()), user_hash, source, trace_id, calls, tokens_in, tokens_out),
            )

    def today(self, user_hash: str) -> DayUsage:
        """The use of today (UTC), since the newest clear if there was one today."""
        day_start = iso(self._day_start())
        with closing(open_connection(self.path)) as conn:
            clear = conn.execute(
                "SELECT usage_seq, ts FROM budget_clears WHERE user_hash = ? ORDER BY seq DESC LIMIT 1", (user_hash,)
            ).fetchone()
            after = clear["usage_seq"] if clear else 0
            rows = conn.execute(
                "SELECT source, COUNT(*) AS entries, SUM(calls) AS calls, SUM(tokens_in) AS tokens_in, SUM(tokens_out) AS tokens_out "
                "FROM token_usage WHERE user_hash = ? AND seq > ? AND ts >= ? GROUP BY source",
                (user_hash, after, day_start),
            ).fetchall()
        by_source = {row["source"]: Totals(row["entries"], row["calls"] or 0, row["tokens_in"] or 0, row["tokens_out"] or 0) for row in rows}
        cleared_at = clear["ts"] if clear and clear["ts"] >= day_start else None
        return DayUsage(day_start[:10], by_source.get("chat", Totals()), by_source.get("eval", Totals()), cleared_at)

    def clear(self, user_hash: str, *, by: ClearedBy, trace_id: str | None = None) -> DayUsage:
        """Start today's count again. Returns the use that was cleared. Audited in the same transaction."""
        before = self.today(user_hash)
        stamp = iso(self._now())
        with closing(open_connection(self.path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                newest = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM token_usage").fetchone()[0]
                conn.execute(
                    "INSERT INTO budget_clears (ts, user_hash, cleared_by, usage_seq) VALUES (?, ?, ?, ?)", (stamp, user_hash, by, newest)
                )
                write_entry(
                    conn, AuditEntry("budget_cleared", user_hash, "ok", count=before.tokens, trace_id=trace_id, detail=f"cleared by {by}"), stamp
                )
            except BaseException:
                conn.rollback()
                raise
            conn.commit()
        return before


class DailyBudget:
    """The ledger read against the cap of `budget.daily_tokens_per_user`."""

    def __init__(self, ledger: UsageLedger, cap: int | None, alert_fraction: float = 0.8) -> None:
        self.ledger = ledger
        self.cap = cap
        self.alert_fraction = alert_fraction

    @classmethod
    def from_settings(cls, settings: Settings, ledger: UsageLedger | None = None) -> "DailyBudget":
        return cls(ledger or UsageLedger.from_settings(settings), settings.budget.daily_tokens_per_user, settings.budget.alert_fraction)

    def status(self, user_hash: str) -> BudgetStatus:
        return BudgetStatus(self.ledger.today(user_hash), self.cap)

    def check(self, user_hash: str) -> BudgetStatus:
        """Raise `DAILY_BUDGET_USED` when the cap is used up. Returns the status so the caller can tell whether a turn crossed the alert line."""
        status = self.status(user_hash)
        if status.exhausted:
            raise AgentError(ErrorKind.DAILY_BUDGET_USED, detail=f"{status.used} of {self.cap} tokens used today")
        return status

    def crossed_alert(self, before: BudgetStatus, after: BudgetStatus) -> bool:
        """True when the use went from under the alert line to at or over it: one warning, not one on every later turn."""
        if self.cap is None or before.fraction is None or after.fraction is None:
            return False
        return before.fraction < self.alert_fraction <= after.fraction
