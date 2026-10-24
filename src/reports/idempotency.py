"""Idempotency keys and the "return the first result" path (plan 08, F27, decision 3.10, Q23).

A key names one request. The same key twice has one effect: the second call finds the first result and changes nothing, and
writes no second audit row. The turn ID is part of every key (Q23), so saving the same text in a later turn is a new request
with a new key. A retry of the same request, or a resume after a restart, finds the first effect.

The graph builds the key of a write or gated tool with `tools.base.idempotency_key` and puts it in `ToolContext.idempotency_key`.
The builders here use the same function and give the same value for a caller that has none, and a test ties the two. `save` adds
the hash of the text to whichever key it has, so the same request key with other text is another request.
"""
from __future__ import annotations

import hashlib
import sqlite3
from typing import Literal

from src.tools.base import idempotency_key as make_key

SAVE_TOOL = "saved_reports.save"
DELETE_TOOL = "saved_reports.delete"
UNDO_TOOL = "saved_reports.undo_delete"

Kind = Literal["save", "delete", "undo"]

# Fixed SQL for each kind: the column that holds the key is never taken from a caller. The owner is in every lookup, like in every
# other query of the store: a key that another owner made finds nothing (and the insert then fails on the UNIQUE key, closed).
_FIRST = {
    "save": "SELECT * FROM saved_reports WHERE idempotency_key = ? AND owner_id = ?",
    "delete": "SELECT * FROM delete_batches WHERE idempotency_key = ? AND owner_id = ?",
    "undo": "SELECT * FROM delete_batches WHERE undo_key = ? AND owner_id = ?",
}


def save_key(conversation_id: str, turn_id: str, body: str, request_key: str | None = None) -> str:
    """conversation | turn | save | hash of the text, or the key the graph gave this request in place of the first three. A later
    turn that saves the same text has another turn ID, so it is another request."""
    head = (request_key,) if request_key else (conversation_id, turn_id, SAVE_TOOL)
    return make_key(*head, hashlib.sha256(body.encode("utf-8")).hexdigest())


def delete_key(conversation_id: str, request_turn_id: str, id_set_hash: str) -> str:
    """conversation | the turn that asked for the delete | delete | hash of the exact ID set.

    The request turn, not the turn that confirms, so the key is the same when the confirmation arrives in a later turn or after
    a restart."""
    return make_key(conversation_id, request_turn_id, DELETE_TOOL, id_set_hash)


def undo_key(conversation_id: str, turn_id: str) -> str:
    return make_key(conversation_id, turn_id, UNDO_TOOL)


def first_result(conn: sqlite3.Connection, kind: Kind, key: str, owner_id: str) -> sqlite3.Row | None:
    """The row this owner's first call with this key made, or None. Run it inside the transaction that would make the effect: the
    write lock is held, so two calls with one key cannot both find nothing."""
    return conn.execute(_FIRST[kind], (key, owner_id)).fetchone()
