-- Plan 08: saved reports, delete batches and the append-only audit log.
-- Every report has an owner and the conversation it was saved in (S8), so "delete the reports from this conversation" can work.

CREATE TABLE saved_reports (
    report_id       TEXT PRIMARY KEY,                -- r-<8 hex>. Never shown to the model, a trace or a log
    owner_id        TEXT NOT NULL,                   -- login name from users.yaml
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    turn_id         TEXT NOT NULL,
    title           TEXT NOT NULL,
    body            TEXT NOT NULL,                   -- the answer as it was shown: it passed the Output Guard (A20)
    search_text     TEXT NOT NULL,                   -- the part of the body a text search looks in: not the scope line or the notes (impl)
    created_at      TEXT NOT NULL,                   -- ISO 8601, UTC
    deleted_at      TEXT,                            -- soft delete: set while the report can still be restored
    delete_batch_id TEXT,
    purge_after     TEXT,                            -- deleted_at + undo_days: then the row is removed for good
    idempotency_key TEXT NOT NULL UNIQUE
);

CREATE INDEX idx_reports_owner ON saved_reports (owner_id, deleted_at);

-- One row per confirmed delete. "undo" restores the newest batch that is not undone and still inside its window.
CREATE TABLE delete_batches (
    batch_id        TEXT PRIMARY KEY,                -- b-<8 hex>
    owner_id        TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    id_set_hash     TEXT NOT NULL,                   -- the keyed hash of the confirmed ID set
    count           INTEGER NOT NULL,
    undone_at       TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,
    undo_key        TEXT UNIQUE                      -- the key of the undo that restored it, so a retried undo restores nothing more (impl)
);

-- The audit log never holds report text or titles: the user hash, the action, counts, the ID-set hash and the trace and turn IDs.
CREATE TABLE audit_log (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    user_hash       TEXT NOT NULL,
    action          TEXT NOT NULL,                   -- save, delete_requested, delete_confirmed, delete_cancelled, delete_stale, delete_executed, undo, purge
    target_hash     TEXT,
    count           INTEGER,
    outcome         TEXT NOT NULL,
    trace_id        TEXT,
    turn_id         TEXT,
    idempotency_key TEXT,
    detail          TEXT
);

CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_log BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit_log BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
