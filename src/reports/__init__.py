"""Saved Reports (plan 08): the SQLite store, the append-only audit log, idempotency keys and the user-facing messages.

The tools in `src/tools/saved_reports.py` are thin: they take the owner from the tool context, call the store and write the
words from `messages.py`. Everything that changes data happens in the store, in one transaction with its audit row.
"""
