-- Plan 12 (NFR21, A11): the audit log is kept for one year, and no longer.
-- Migration 002 made every delete refuse. That kept the log for ever, which is not what the requirement says. Now a delete is refused only for a row younger than
-- 365 days, so a row stays append-only for the whole year it must be kept, and the startup purge (retention.audit_days, never below 365) can remove the older ones.
-- audit_no_update stays as it is: no row is ever changed.
-- The cutoff is written in the format of the `ts` column (ISO 8601 UTC to the second, `+00:00`), so the comparison of two texts is the comparison of two times.

DROP TRIGGER audit_no_delete;

CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit_log
WHEN OLD.ts > strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now', '-365 days')
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
