"""The Saved Reports store on SQLite (plan 08, F20 to F28). Production would be Postgres behind the same methods.

Three rules shape every method:

- **The owner is an argument of every query.** `find`, `delete` and `undo` all have `WHERE owner_id = ?`, so a report of another
  user is never seen, found or changed, whatever IDs or words the caller passes (F21).
- **An effect and its audit row are one transaction.** If the audit insert fails, the effect rolls back (F26). A failed effect
  leaves nothing half done.
- **A key names one request.** The same idempotency key finds the first result and changes nothing (F27, `idempotency.py`).

Each call opens its own short-lived connection. The graph runs a tool on a worker thread and stops waiting after `timeout_s`, so
no connection is shared between threads; `BEGIN IMMEDIATE` takes the write lock, so two calls with one key cannot both write.
Soft-deleted reports keep their text in the database until `purge_after` (30 days), which is what lets `undo` work.
"""
from __future__ import annotations

import logging
import secrets
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterator, Literal, Sequence

from src.errors import crash_summary
from src.reports import idempotency
from src.storage import audit
from src.storage.audit import AuditEntry, AuditLog
from src.storage.db import iso, now_utc, open_connection

if TYPE_CHECKING:
    from src.config import Settings

log = logging.getLogger(__name__)

HashIds = Callable[[Sequence[str]], str]  # the keyed hash of an ID set: `confirmation.id_set_hash` with the installation's key
Clock = Callable[[], datetime]

UndoStatus = Literal["restored", "none", "already", "expired"]


def fold(text: str) -> str:
    """Case-insensitive for every alphabet: SQLite's own `lower()` and LIKE only fold ASCII."""
    return text.casefold()


def connect(path: Path) -> sqlite3.Connection:
    """A new connection for one call, with the `fold` function the text search uses."""
    conn = open_connection(path)
    try:
        conn.create_function("fold", 1, fold, deterministic=True)
    except sqlite3.Error:
        conn.close()
        raise
    return conn


@contextmanager
def transaction(path: Path) -> Iterator[sqlite3.Connection]:
    """`BEGIN IMMEDIATE` ... commit, or roll back on any exception. The write lock is taken at the start."""
    with closing(connect(path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise
        conn.commit()


def purge_expired(path: Path, now: Clock | None = None, undo_days: int = 30) -> int:
    """Remove for good the soft-deleted reports whose undo window is over, and the delete batches of that window with them. Runs
    at startup. One audit row with the count, when anything was removed. Needs no key and no user, so the chat can run it before
    the agent is built.

    The window is over when `now` is *after* `purge_after` (a delete made at noon can be undone until noon 30 days later, and not
    purged before it): the same test `undo` makes, so a report is never purged while an undo would still restore it."""
    current = (now or now_utc)()
    moment = iso(current)
    with transaction(path) as conn:
        removed = conn.execute(
            "DELETE FROM saved_reports WHERE deleted_at IS NOT NULL AND purge_after IS NOT NULL AND purge_after < ?", (moment,)
        ).rowcount
        conn.execute("DELETE FROM delete_batches WHERE created_at < ?", (iso(current - timedelta(days=undo_days)),))  # nothing left to undo
        if removed:
            audit.write_entry(conn, AuditEntry("purge", audit.SYSTEM_USER, "ok", count=removed), moment)
        return removed


def escape_like(text: str) -> str:
    """`\\`, `%` and `_` are plain characters in the user's words, not wildcards."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@dataclass(frozen=True)
class Actor:
    """Who acts and in which turn: the audit row's identity. The owner of every query is `user_id`."""

    user_id: str
    user_hash: str
    conversation_id: str
    turn_id: str
    trace_id: str


@dataclass(frozen=True)
class ReportRow:
    report_id: str = field(repr=False)  # never shown to the model, a trace or a log
    title: str
    created_at: str

    @property
    def date(self) -> str:
        return self.created_at[:10]


@dataclass(frozen=True)
class SaveOutcome:
    report: ReportRow
    created: bool  # False: this key had saved it already


@dataclass(frozen=True)
class DeleteOutcome:
    count: int
    replayed: bool = False  # True: this key had deleted them already


@dataclass(frozen=True)
class UndoOutcome:
    status: UndoStatus
    count: int = 0
    replayed: bool = False


def _row(record: sqlite3.Row) -> ReportRow:
    return ReportRow(record["report_id"], record["title"], record["created_at"])


class ReportStore:
    def __init__(self, path: Path, *, hash_ids: HashIds, undo_days: int = 30, now: Clock | None = None) -> None:
        self.path = Path(path)
        self.undo_days = undo_days
        self._hash = hash_ids
        self._now = now or now_utc
        self.audit = AuditLog(self.path, self._now)

    @classmethod
    def from_settings(cls, settings: "Settings", *, hash_ids: HashIds, now: Clock | None = None) -> "ReportStore":
        return cls(settings.db_path, hash_ids=hash_ids, undo_days=settings.delete.undo_days, now=now)

    # connections ------------------------------------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        return connect(self.path)

    def _transaction(self):
        return transaction(self.path)

    @staticmethod
    def _unused_id(conn: sqlite3.Connection, table: str, column: str, prefix: str) -> str:
        for _ in range(8):  # 2^32 values: a clash is a rarity, not a loop
            candidate = prefix + secrets.token_hex(4)
            if conn.execute(f"SELECT 1 FROM {table} WHERE {column} = ?", (candidate,)).fetchone() is None:
                return candidate
        raise RuntimeError("could not find an unused ID")

    # reads ------------------------------------------------------------------------------------
    def find(self, owner_id: str, conversation_id: str, kind: str, text: str | None = None) -> list[ReportRow]:
        """The owner's reports that are not deleted and match the selector, oldest first (`last` gives the newest one).

        `conversation`: saved in `conversation_id`. `text_match`: the title or the searchable body contains the text, case-insensitive,
        with `%`, `_` and `\\` as plain characters. `all`: every one. Any other kind finds nothing."""
        where, args = ["owner_id = ?", "deleted_at IS NULL"], [owner_id]
        order, limit = "created_at, rowid", ""
        if kind == "conversation":
            where.append("conversation_id = ?")
            args.append(conversation_id)
        elif kind == "text_match":
            if not text or not text.strip():
                return []  # an empty search would match everything
            pattern = f"%{escape_like(fold(text.strip()))}%"
            where.append("(fold(title) LIKE ? ESCAPE '\\' OR fold(search_text) LIKE ? ESCAPE '\\')")
            args += [pattern, pattern]
        elif kind == "last":
            order, limit = "created_at DESC, rowid DESC", " LIMIT 1"
        elif kind != "all":
            return []
        sql = f"SELECT report_id, title, created_at FROM saved_reports WHERE {' AND '.join(where)} ORDER BY {order}{limit}"
        with closing(self.connect()) as conn:
            return [_row(record) for record in conn.execute(sql, args)]

    def list_reports(self, owner_id: str) -> list[ReportRow]:
        return self.find(owner_id, "", "all")

    # effects ----------------------------------------------------------------------------------
    def save(self, who: Actor, *, title: str, body: str, search_text: str, key: str) -> SaveOutcome:
        with self._transaction() as conn:
            first = idempotency.first_result(conn, "save", key, who.user_id)
            if first is not None:
                return SaveOutcome(_row(first), created=False)
            report_id = self._unused_id(conn, "saved_reports", "report_id", "r-")
            created_at = iso(self._now())
            conn.execute(
                "INSERT INTO saved_reports (report_id, owner_id, conversation_id, turn_id, title, body, search_text, created_at, "
                "idempotency_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (report_id, who.user_id, who.conversation_id, who.turn_id, title, body, search_text, created_at, key),
            )
            audit.write_entry(conn, AuditEntry(
                "save", who.user_hash, "ok", target_hash=self._hash([report_id]), count=1, trace_id=who.trace_id,
                turn_id=who.turn_id, idempotency_key=key,
            ), created_at)
            return SaveOutcome(ReportRow(report_id, title, created_at), created=True)

    def delete(self, who: Actor, ids: Sequence[str], *, key: str) -> DeleteOutcome:
        """Soft-delete the owner's reports among `ids` as one batch. Returns how many were deleted.

        An ID that is not the owner's, or is already deleted, is skipped: the caller's list is never trusted. The audit row's
        `target_hash` is the hash of the confirmed ID set, the same value the `delete_requested` row carries."""
        wanted = sorted({str(report_id) for report_id in ids})
        target = self._hash(wanted)
        with self._transaction() as conn:
            first = idempotency.first_result(conn, "delete", key, who.user_id)
            if first is not None:
                return DeleteOutcome(first["count"], replayed=True)
            marks = ",".join("?" * len(wanted))
            found = [
                record["report_id"] for record in conn.execute(
                    f"SELECT report_id FROM saved_reports WHERE owner_id = ? AND deleted_at IS NULL AND report_id IN ({marks})",
                    (who.user_id, *wanted),
                )
            ] if wanted else []
            now = self._now()
            moment = iso(now)
            entry = dict(target_hash=target, trace_id=who.trace_id, turn_id=who.turn_id, idempotency_key=key)
            if not found:
                # No effect and no key stored: a repeat of this request must not add a second row either.
                audit.write_entry(conn, AuditEntry("delete_executed", who.user_hash, "no_match", count=0, **entry), moment, once=True)
                return DeleteOutcome(0)
            batch_id = self._unused_id(conn, "delete_batches", "batch_id", "b-")
            marks = ",".join("?" * len(found))
            conn.execute(
                f"UPDATE saved_reports SET deleted_at = ?, delete_batch_id = ?, purge_after = ? "
                f"WHERE owner_id = ? AND deleted_at IS NULL AND report_id IN ({marks})",
                (moment, batch_id, iso(now + timedelta(days=self.undo_days)), who.user_id, *found),
            )
            conn.execute(
                "INSERT INTO delete_batches (batch_id, owner_id, conversation_id, created_at, id_set_hash, count, idempotency_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (batch_id, who.user_id, who.conversation_id, moment, target, len(found), key),
            )
            audit.write_entry(conn, AuditEntry("delete_executed", who.user_hash, "ok", count=len(found), **entry), moment)
            return DeleteOutcome(len(found))

    def undo(self, who: Actor, *, key: str) -> UndoOutcome:
        """Restore the reports of the newest delete that is not undone and is still inside its window. No confirmation: restoring
        is safe. Each undo walks one delete further back; the same key restores nothing a second time."""
        with self._transaction() as conn:
            first = idempotency.first_result(conn, "undo", key, who.user_id)
            if first is not None:
                return UndoOutcome("restored", first["count"], replayed=True)
            now = self._now()
            moment = iso(now)
            latest = conn.execute(  # one row, by the index on (owner, time): the history of deletes only grows
                "SELECT * FROM delete_batches WHERE owner_id = ? AND undone_at IS NULL ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (who.user_id,),
            ).fetchone()
            status: UndoStatus
            restored = 0
            if latest is None:
                any_delete = conn.execute("SELECT 1 FROM delete_batches WHERE owner_id = ? LIMIT 1", (who.user_id,)).fetchone()
                status = "already" if any_delete else "none"
            elif now > datetime.fromisoformat(latest["created_at"]) + timedelta(days=self.undo_days):
                status = "expired"
            else:
                restored = conn.execute(
                    "UPDATE saved_reports SET deleted_at = NULL, delete_batch_id = NULL, purge_after = NULL "
                    "WHERE owner_id = ? AND delete_batch_id = ?",
                    (who.user_id, latest["batch_id"]),
                ).rowcount
                if restored:
                    conn.execute("UPDATE delete_batches SET undone_at = ?, undo_key = ? WHERE batch_id = ?", (moment, key, latest["batch_id"]))
                    status = "restored"
                else:
                    # Nothing left to restore (the rows are gone). The batch stays as it is: marking it would make a retry of this very
                    # request answer "restored N", and a later undo "already undone", for reports that were never brought back.
                    status = "expired"
            audit.write_entry(conn, AuditEntry(
                "undo", who.user_hash, "ok" if status == "restored" else status,
                target_hash=latest["id_set_hash"] if latest is not None else None,
                count=restored, trace_id=who.trace_id, turn_id=who.turn_id, idempotency_key=key,
            ), moment, once=status != "restored")  # a refusal has no stored key, so a repeat asks to be recorded once
            return UndoOutcome(status, restored)

    def purge_expired(self) -> int:
        return purge_expired(self.path, self._now, self.undo_days)

    # an effect that did not happen -----------------------------------------------------------
    def id_set_hash(self, ids: Sequence[str]) -> str:
        return self._hash(sorted({str(report_id) for report_id in ids}))

    def record(self, who: Actor, action: str, outcome: str, *, key: str | None = None, detail: str = "", once: bool = False) -> None:
        """An audit row for a request that ended without an effect: it failed and rolled back, or it was refused. Best effort: it
        never raises, because the caller is already reporting the problem and the database may be the reason. `once`: a repeat
        of a refused request (same key, action and outcome) is not recorded again; a repeat of a failure is, because it failed again."""
        try:
            self.audit.append(AuditEntry(
                action, who.user_hash, outcome, trace_id=who.trace_id, turn_id=who.turn_id, idempotency_key=key, detail=detail,
            ), once=once)
        except Exception as exc:  # noqa: BLE001 - see above
            log.error("could not record %s %s in the audit log: %s", action, outcome, crash_summary(exc))
