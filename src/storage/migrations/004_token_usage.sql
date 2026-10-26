-- Token use per user, for the daily budget (NFR8, Q15) and for `/budget`.
-- One row per turn of the chat (source 'chat') and one per turn that `/eval` judged (source 'eval'). Counts only: no text, no IDs of reports.
-- The user is the user hash, as everywhere else. A day is a UTC day.

CREATE TABLE token_usage (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,                        -- ISO 8601, UTC, to the second
    user_hash  TEXT NOT NULL,
    source     TEXT NOT NULL CHECK (source IN ('chat', 'eval')),
    trace_id   TEXT,                                 -- the turn this was spent on
    calls      INTEGER NOT NULL DEFAULT 0,           -- model calls, retries and fallbacks included
    tokens_in  INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_token_usage_user ON token_usage (user_hash, seq);

-- `/budget clear` adds a row here and never deletes a usage row. The count of a day starts after the newest clear: `usage_seq` is the newest
-- `token_usage.seq` at that moment, so a row written in the same second as the clear is counted on the right side of it.
CREATE TABLE budget_clears (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    user_hash  TEXT NOT NULL,
    cleared_by TEXT NOT NULL CHECK (cleared_by IN ('user', 'operator')),
    usage_seq  INTEGER NOT NULL
);

CREATE INDEX idx_budget_clears_user ON budget_clears (user_hash, seq);
