-- Plan 08 review: the two lookups that grow with the history get an index.
-- delete_batches is read by owner for every undo; audit_log is read by key for every request that ended without an effect (`once`).

CREATE INDEX idx_batches_owner ON delete_batches (owner_id, created_at);
CREATE INDEX idx_audit_key ON audit_log (idempotency_key, action);
