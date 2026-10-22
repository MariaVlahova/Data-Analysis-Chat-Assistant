# Plan 08: Saved Reports and confirmation flow
Status: not started

**Goal:** Let users save, list, delete and undo their own reports through a tool registry that replaces plan 07's stubbed Saved Reports tool (the Tool Router and HITL Gate around it are already real), with a find, preview, confirm, delete flow bound to a hash of the exact ID set, friction by risk, soft delete, idempotency keys proven by a retry test, a SqliteSaver checkpoint so the confirmation pause survives a restart, and an append-only audit log.
