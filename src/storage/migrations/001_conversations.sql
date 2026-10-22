-- Plan 01: conversations.
-- Plan 08 adds 002_saved_reports_audit.sql and needs conversations.id in saved reports (S8).

CREATE TABLE conversations (
    id             TEXT PRIMARY KEY,                -- conv-<8 hex>
    user_id        TEXT NOT NULL,                   -- login name from users.yaml
    created_at     TEXT NOT NULL,                   -- ISO 8601, UTC
    last_active_at TEXT NOT NULL,
    turn_count     INTEGER NOT NULL DEFAULT 0       -- turns so far, so turn IDs (t-<n>) stay unique on resume
);

CREATE INDEX idx_conversations_user ON conversations (user_id, last_active_at);
