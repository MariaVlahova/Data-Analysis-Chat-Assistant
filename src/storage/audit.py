"""The append-only audit log (plan 08, F26, NFR21). It lives in the app database beside the tables it audits, and no tool owns it:
any gated tool names its steps the same way (`<verb>_requested`, `_confirmed`, `_cancelled`, `_stale`) and writes them here.

A row says who did what and how it ended: the user hash, the action, a count, the hash of the ID set, the trace and turn IDs
and the idempotency key. It never holds report text, titles or IDs. `migrations/002` makes the table append-only with two
triggers; production would use a Postgres role without UPDATE or DELETE and partition drops for the 1-year retention. (impl)

Retention (plan 12, NFR21, A11): `migrations/005` lets a delete through only for a row older than 365 days, so a row is append-only for the whole year
it must be kept. `purge_old_entries` deletes the rows older than `retention.audit_days` (never below 365) at startup, and writes one `audit_purged` row about it
in the same transaction.

The actions of plan 08 are `save`, `delete_requested`, `delete_confirmed`, `delete_cancelled`, `delete_stale`, `delete_executed`,
`undo` and `purge`. The startup purge of the log itself (below) writes `audit_purged`.

`write_entry` takes the connection of a transaction that is already open: an effect and its audit row commit together, and if
the audit insert fails the effect rolls back. No effect happens without a record. `AuditLog.append` is for the events that
have no effect of their own (a delete was asked for, confirmed, cancelled or found stale).

A repeat of a request writes no second row. An effect is found by its key before it is made again (`reports/idempotency.py`); a
request that ended without an effect (nothing matched, nothing to undo, a save that was refused) asks for `once=True`, which skips
the row when this key already has one with the same action and outcome.
"""
from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from src import pii
from src.storage.db import iso, now_utc, open_connection

SYSTEM_USER = "system"  # the user hash of a row nobody asked for: the startup purge
MAX_DETAIL = 200

_ACTION_NAME = re.compile(r"^[a-z][a-z_]{1,39}$")


@dataclass(frozen=True)
class AuditEntry:
    action: str
    user_hash: str
    outcome: str  # ok, failed, blocked, cancelled, abandoned, no_match, ...
    target_hash: str | None = None  # the keyed hash of the ID set the action was about
    count: int | None = None
    trace_id: str | None = None
    turn_id: str | None = None
    idempotency_key: str | None = None
    detail: str | None = None  # a few words, masked. Never report text.


def write_entry(conn: sqlite3.Connection, entry: AuditEntry, ts: str | None = None, *, once: bool = False) -> bool:
    """Insert one row on `conn`, inside the caller's transaction. Returns False when `once` found the row already there."""
    if not _ACTION_NAME.match(entry.action):
        raise ValueError(f"not an audit action name: {entry.action!r}")
    if once and entry.idempotency_key is not None and conn.execute(
        "SELECT 1 FROM audit_log WHERE action = ? AND idempotency_key = ? AND outcome = ?",
        (entry.action, entry.idempotency_key, entry.outcome),
    ).fetchone():
        return False
    detail = pii.mask(entry.detail)[:MAX_DETAIL] if entry.detail else None  # masked first, then cut
    conn.execute(
        "INSERT INTO audit_log (ts, user_hash, action, target_hash, count, outcome, trace_id, turn_id, idempotency_key, detail) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (ts or iso(now_utc()), entry.user_hash, entry.action, entry.target_hash, entry.count, entry.outcome,
         entry.trace_id, entry.turn_id, entry.idempotency_key, detail),
    )
    return True


def purge_old_entries(path: Path, days: int, *, now: Callable[[], datetime] | None = None) -> int:
    """Delete the audit rows older than `days` and add an `audit_purged` row (the count, the system user) in the same transaction, so a purge is on the record
    it shortens. Returns how many rows were removed; no row is written when there was nothing to remove. The trigger of migration 005 refuses a row younger than
    365 days whatever `days` says: this function cannot shorten the year, and a transaction that the trigger refuses is rolled back whole."""
    moment = (now or now_utc)()
    cutoff = iso(moment - timedelta(days=days))
    with closing(open_connection(path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            removed = conn.execute("DELETE FROM audit_log WHERE ts < ?", (cutoff,)).rowcount
            if removed:
                write_entry(conn, AuditEntry("audit_purged", SYSTEM_USER, "ok", count=removed, detail=f"older than {days} days"), iso(moment))
        except BaseException:
            conn.rollback()
            raise
        conn.commit()
    return removed


class AuditLog:
    """Appends and reads rows on its own short-lived connections, so it is safe to call from a tool's worker thread."""

    def __init__(self, path: Path, now: Callable[[], datetime] | None = None) -> None:
        self.path = Path(path)
        self._now = now or now_utc

    def append(self, entry: AuditEntry, *, once: bool = False) -> None:
        with closing(open_connection(self.path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                write_entry(conn, entry, iso(self._now()), once=once)
            except BaseException:
                conn.rollback()
                raise
            conn.commit()

    def entries(self, *, action: str | None = None) -> list[dict[str, Any]]:
        """The rows in the order they were written, for tests and for an operator."""
        sql, args = "SELECT * FROM audit_log", ()
        if action is not None:
            sql, args = sql + " WHERE action = ?", (action,)
        with closing(open_connection(self.path)) as conn:
            return [dict(row) for row in conn.execute(sql + " ORDER BY seq", args)]
