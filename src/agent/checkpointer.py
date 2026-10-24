"""The checkpointer on disk (plan 08, F28, NFR21): the confirmation pause survives a restart.

LangGraph's `SqliteSaver` writes its own tables (`checkpoints`, `writes`) into the same `data/db.sqlite3`, on a separate
connection with WAL on. The thread ID is the conversation ID, so a conversation that stopped at the HITL Gate is still stopped
there after the process is gone: `AgentRuntime.pending_confirmation` finds it and the chat shows the preview again.

What is in a checkpoint is what the state holds: questions, guarded and masked results, counts, the final answer, and for a
pending action the selector, a keyed hash of the ID set and the preview text. Report IDs, raw rows and the pseudonymizer are
never in state (plan 06, plan 07). What the user typed is masked (`pii.mask`) before it enters state, as it is before it enters a
trace, and the answers are the ones the Output Guard passed. A checkpoint still holds the user's questions and answers for
`retention.checkpoint_days`, in the app database, so the data folder is as private as the data it describes.

Retention: a conversation idle for `retention.checkpoint_days` (90) loses its checkpoints. The saved reports of that
conversation stay; they have their own life (soft delete, 30-day undo).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from langgraph.checkpoint.sqlite import SqliteSaver

from src.errors import ConfigError
from src.storage.db import iso, now_utc

if TYPE_CHECKING:
    from src.config import Settings


def open_checkpointer(settings: "Settings") -> SqliteSaver:
    """A `SqliteSaver` on its own connection to the app database. Call it after `Database.migrate()`: the database must be one this
    app made, and the saver then adds its tables. Close it with `close_checkpointer`."""
    path = settings.db_path
    conn: sqlite3.Connection | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Python's default transactions, as the saver expects (it commits after each write). `check_same_thread=False`: the graph
        # may call it from a worker thread; the saver serializes its own use with a lock.
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        saver = SqliteSaver(conn)
        saver.setup()
        return saver
    except (OSError, sqlite3.Error) as exc:
        if conn is not None:
            conn.close()
        raise ConfigError(
            f"The checkpoint store could not be opened: {path} ({exc})",
            fix="Check that the data folder is writable and that the file is a SQLite database.",
        ) from None


INTERRUPT_CHANNEL = "__interrupt__"  # where LangGraph records a pause in the saver's `writes` table; a test checks it on the installed version


def prune_thread(saver: SqliteSaver, thread_id: str) -> int:
    """Keep only the newest checkpoint of a conversation, with its pending writes (a pause at the gate is one of them). Returns how
    many older checkpoints were removed.

    The saver writes the whole state at every step of every turn, and only the newest checkpoint is needed to carry on, so without
    this a conversation in daily use would keep every step of every turn for as long as it is used. (`SqliteSaver.prune` is not
    implemented in the version installed.) The graph has no subgraphs, so the namespace is the empty one."""
    with saver.cursor() as cur:
        newest = cur.execute(
            "SELECT checkpoint_id FROM checkpoints WHERE thread_id = ? AND checkpoint_ns = '' ORDER BY checkpoint_id DESC LIMIT 1", (thread_id,)
        ).fetchone()
        if newest is None:
            return 0
        removed = cur.execute(
            "DELETE FROM checkpoints WHERE thread_id = ? AND checkpoint_ns = '' AND checkpoint_id <> ?", (thread_id, newest[0])
        ).rowcount
        cur.execute("DELETE FROM writes WHERE thread_id = ? AND checkpoint_ns = '' AND checkpoint_id <> ?", (thread_id, newest[0]))
        return removed


def waiting_conversations(saver: SqliteSaver, user_id: str, limit: int) -> list[str]:
    """The IDs of the user's conversations that have a pause recorded, most recently active first, at most `limit`.

    One query on the saver's tables instead of loading a checkpoint for each conversation. These are candidates: an old pause that
    was answered can still be on file in a database that was not pruned, so the caller asks `pending_confirmation` of each."""
    with saver.lock:
        rows = saver.conn.execute(
            "SELECT c.id FROM conversations c WHERE c.user_id = ? "
            "AND EXISTS (SELECT 1 FROM writes w WHERE w.thread_id = c.id AND w.channel = ?) "
            "ORDER BY c.last_active_at DESC, c.rowid DESC LIMIT ?",
            (user_id, INTERRUPT_CHANNEL, limit),
        ).fetchall()
    return [row[0] for row in rows]


def close_checkpointer(saver: SqliteSaver | None) -> None:
    if saver is not None:
        saver.conn.close()


def purge_old_checkpoints(saver: SqliteSaver, days: int, now: datetime | None = None) -> int:
    """Delete the checkpoints of conversations idle for more than `days`. Returns how many conversations lost theirs.

    Idle means the conversation's `last_active_at`, which every turn updates. One query finds the conversations that are idle and
    still have checkpoints, so the cost follows what is left to delete, not the whole history; each is removed with the saver's own
    `delete_thread`. The conversations table is in the same database as the saver's."""
    cutoff = iso((now or now_utc()) - timedelta(days=days))
    with saver.lock:
        threads = [
            row[0] for row in saver.conn.execute(
                "SELECT DISTINCT thread_id FROM checkpoints WHERE thread_id IN (SELECT id FROM conversations WHERE last_active_at < ?)",
                (cutoff,),
            )
        ]
    for thread_id in threads:
        saver.delete_thread(thread_id)
    return len(threads)
