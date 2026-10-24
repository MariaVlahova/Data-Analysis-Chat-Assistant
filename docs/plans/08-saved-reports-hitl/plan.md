# Plan 08: Saved Reports and confirmation flow
Status: not started
Estimate: about 1.25 h (our estimate) · Depends on: 01, 02, 07 · Blocks: 09

**Goal:** Let users save, list, delete and undo their own reports through a tool registry that replaces plan 07's stubbed Saved Reports tool (the Tool Router and HITL Gate around it are already real), with a find, preview, confirm, delete flow bound to a hash of the exact ID set, friction by risk, soft delete, idempotency keys proven by a retry test, a SqliteSaver checkpoint so the confirmation pause survives a restart, and an append-only audit log.

Tags and `(impl)` marks are explained in plan 01.

## Requirements covered
| ID | Requirement | Source |
|---|---|---|
| F20 | Users can save a report. A report stores owner, conversation ID and text [client default, Q5]. A title is a candidate field. The full field list is still open (Q5). | PRD 6.3 |
| F21 | Users can list and delete only their own reports | PRD 6.3 |
| F22 | Delete flow: find, preview, confirm, delete. The model extracts only intent and a selector. A tool finds the IDs, filtered to the user's own reports. Both a scope ("this conversation") and a text match ("mentioning Client X") must work. | PRD 6.3 |
| F23 | Friction by risk: one report `y`, a list `yes`, a large batch or "all" types `DELETE <n>` | PRD 6.3 |
| F24 | The confirmation is bound to a hash of the exact ID set | PRD 6.3 |
| F25 | Deletes are soft, with 30-day undo | PRD 6.3 |
| F26 | Every save, delete and undo goes to an append-only audit log | PRD 6.3 |
| F27 | A retry never repeats an effect (idempotency key including the turn ID) | PRD 6.3 |
| F28 | The confirmation pause survives a restart | PRD 6.3 |
| NFR17, NFR18 | One confirmation turn and 30-day undo; zero duplicate saves | [PROPOSED] |
| S8 [Medium] | Saved reports need an owner and a conversation ID, or "delete reports from this conversation" cannot work | decision 9.2 |

## Decisions that apply
- 3.6 [PROPOSED]: find, preview, confirm, then delete. The model extracts only the action and a selector and never sees IDs. A tool finds reports owned by the user. The confirmation holds a hash of the exact ID set. Rejected: a pop-up for everything, a model-judged "are you sure", undo only. Revisit if nearly 100% of confirmations are "yes".
- 3.6 data: Saved Reports in Postgres in production (SQLite here). Soft delete with 30-day undo. Every action goes to the audit log.
- Friction by risk (decision 3.6 source note): one report y/N, a list "yes", "all" types `DELETE 23`.
- §6 data table: audit log in Cloud SQL Postgres, append-only, user hash, kept 1 year. Idempotency keys and report progress in Postgres (a unique `idempotency_key` column in SQLite). Conversation state in checkpoints, kept 90 days. The audit log stays separate from traces (decision section 2, "Differences from the diagram", which cites Phase1 3.9; decision 3.12 repeats it under "Data lives in").
- 3.10: the idempotency key includes the turn ID (Q23 answered: yes). Without it, saving the same report twice in one conversation would return the first save. One active run per conversation.
- 3.14: manifest includes idempotent, reversible and inverse (soft delete plus undo is the inverse of delete).
- A6: users delete only their own reports; no legal hold. A20: saved reports hold no PII and record their conversation ID.
- Q5 default: owner, conversation ID, text; soft delete; 30-day undo. The PRD adds `title` as a candidate field.
- Loop-engineering checklist: add a test that **a retry or a resume does not create a second effect** (the checklist marks this missing). Saga and compensation are not needed because each action is one step; soft delete plus undo is enough. "Optimistic concurrency" is covered in the prototype by one active run per conversation plus unique keys (A19).
- Dead-letter and failed-job queues are design-only (§8). A failing tool gives an honest message with a trace ID.

## Scope
- In: store, migration `002`, tool manifests and tools, selectors, find, preview text, confirmation levels, soft delete, undo, purge, audit log, idempotency, `SqliteSaver`, restart resume in the CLI, tests.
- Out: the router, gate and manifest classes (built in plan 07), email and other external-send tools (design-only), a Postgres store.

## Data model (`src/storage/migrations/002_saved_reports_audit.sql`)
```sql
CREATE TABLE saved_reports (
  report_id       TEXT PRIMARY KEY,            -- 'r-' + 8 hex
  owner_id        TEXT NOT NULL,
  conversation_id TEXT NOT NULL REFERENCES conversations(id),
  turn_id         TEXT NOT NULL,
  title           TEXT NOT NULL,
  body            TEXT NOT NULL,               -- passed the Output Guard (A20)
  created_at      TEXT NOT NULL,               -- ISO 8601 UTC
  deleted_at      TEXT,
  delete_batch_id TEXT,
  purge_after     TEXT,
  idempotency_key TEXT NOT NULL UNIQUE
);
CREATE INDEX idx_reports_owner ON saved_reports(owner_id, deleted_at);

CREATE TABLE delete_batches (
  batch_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
  created_at TEXT NOT NULL, id_set_hash TEXT NOT NULL, count INTEGER NOT NULL,
  undone_at TEXT, idempotency_key TEXT NOT NULL UNIQUE
);

CREATE TABLE audit_log (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, user_hash TEXT NOT NULL,
  action TEXT NOT NULL, target_hash TEXT, count INTEGER, outcome TEXT NOT NULL,
  trace_id TEXT, turn_id TEXT, idempotency_key TEXT, detail TEXT
);
CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_log BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit_log BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
```
- The audit log never holds report text or titles: `user_hash`, action, counts, `target_hash` (the ID-set hash), trace ID and turn ID only. Production: Postgres with a role that has no `UPDATE` or `DELETE`, and partition drops for the 1-year retention. The prototype's triggers show the same append-only rule. (impl)
- Actions audited: `save`, `delete_requested`, `delete_confirmed`, `delete_cancelled`, `delete_stale`, `delete_executed`, `undo`, `purge`.
- **Effect and audit row are written in one transaction.** If the audit insert fails, the effect rolls back. No effect happens without a record.

## Design
### Tools and manifests (plan 07's interface, real implementations here)
| Tool | Risk class | Idempotent | Reversible | Inverse |
|---|---|---|---|---|
| `saved_reports.save` | write | yes (key) | yes | `saved_reports.delete` |
| `saved_reports.list` | read | yes | n/a | none |
| `saved_reports.find` | read | yes | n/a | none |
| `saved_reports.delete` | destructive | yes (key) | yes | `saved_reports.undo_delete` |
| `saved_reports.undo_delete` | write | yes | yes | `saved_reports.delete` |

`allowed_roles`: any role in the prototype. `timeout_s`: 5. Registered through the registry from plan 07 with no graph change. The stub is deleted from the default wiring (kept for tests).

### Owner enforcement (F21)
Every query takes `owner_id` from the `UserContext`, never from the arguments the model produced. Every `find`, `list`, `delete` and `undo` has `WHERE owner_id = ?` and `deleted_at IS NULL` (or `IS NOT NULL` for undo). The tests try to reach another user's report by ID, by text and by `all`.

### Selectors (the model's only input)
| Selector | Meaning | Example |
|---|---|---|
| `conversation` | Own reports whose `conversation_id` is the current one | "Delete all the reports we made in this conversation" |
| `text_match` (text up to 100 characters) | Own reports whose title or body contains the text, case-insensitive, with `%`, `_` and `\` escaped | "Delete all reports mentioning Client X" |
| `all` | All own reports | "Delete all my reports" |
| `last` | The most recent own report | "Delete that report" |

`text_match` uses a parameterized `LIKE ... ESCAPE`. FTS over title, body and extracted entities is the production design (PRD F22); the prototype's volume does not need it. (impl)

### Save (F20)
- Intent `save_report`. The report is the last answered turn's final answer text in the conversation (the answer that passed the Output Guard). Nothing to save yet: a friendly message.
- Title: the Composer's `title` when the turn was a report, else the first 60 characters of the standalone question. (impl)
- Run `OutputGuard.scan` again on the body. A finding means do not save (A20).
- Idempotency key: `sha256(conversation_id | turn_id | "save" | sha256(body))`. A retry of the same turn returns the first save. A later turn saving the same text makes a new report (Q23).
- Success message names the title and date. Audit `save`.

### Delete flow (F22 to F25; steps 1 to 4 are plan 07's nodes)
1. Input Guard sets intent `delete_reports` and a selector. Shortcut edge to the Tool Router. No SQL. No Composer.
2. `prepare_action` calls `find`. No match: "I found no reports matching that." and stop. Otherwise it builds the preview in code: a numbered list of titles and saved dates, the count, the level and the expected reply. It computes `id_set_hash` (HMAC-SHA-256 of the sorted IDs, keyed with `Deps.hash_key`, `confirmation.id_set_hash(refs, key)`; plan 07) and stores `pending_action` in state. The finder is called through the graph's `_call` (span, `timeout_s`, typed errors), and its message when nothing matches goes through the Output Guard. Audit `delete_requested`.
3. `hitl_gate`: the preview is the prompt, so the user confirms in **one turn**:

| Level | When | Prompt | Accepted reply |
|---|---|---|---|
| 1 | one report | "Delete this report? (y/N)" | `y` or `yes` |
| 2 | 2 up to `delete.large_batch_threshold` − 1 | "Delete these N reports? Type yes to confirm." | `yes` |
| 3 | `all`, or at least the threshold (10) | "This will delete N reports. Type DELETE N to confirm." | `DELETE N` with the real count |

   Comparison is exact in code (case-insensitive for `y` and `yes`, exact for `DELETE N`). Anything else cancels: "Cancelled. Nothing was deleted." Audit `delete_confirmed` or `delete_cancelled`.
4. `execute_action`: run `find` again and recompute the hash. If it differs, abort with `STALE_CONFIRMATION`, show the new preview and ask again (F24). Audit `delete_stale`. If it matches, soft delete in one transaction: set `deleted_at`, `delete_batch_id`, `purge_after = now + undo_days`; insert the `delete_batches` row and the audit row `delete_executed`. Reply: "Deleted N reports. Say 'undo' within 30 days to bring them back."
5. The delete idempotency key is `sha256(conversation_id | request_turn_id | "delete" | id_set_hash)`. The request turn ID is stored in `pending_action`, so the key stays the same when the confirmation arrives in a later turn or after a restart.

The only thing that can confirm is the user's typed reply, delivered as the resume value. Text in the question, in a report body or in a model output can never confirm (loop-engineering "only the user's typed reply confirms").

### Undo (F25)
- Intent `undo_delete`. Picks the latest `delete_batches` row of this owner that is not undone and whose `purge_after` has not passed. It restores `deleted_at = NULL` for that batch's reports. Idempotent: a second undo says "already restored". Window over: "That delete can no longer be undone." Audit `undo`. No confirmation (restoring is safe).
- `purge_expired()` runs at startup: hard-deletes rows whose `purge_after` has passed. Audit `purge` with the count.

### Checkpoints and restart (F28)
- Compile the graph with `SqliteSaver` on the same `data/db.sqlite3` (a separate connection, WAL on). `thread_id` is the `conversation_id`. The saver creates its own tables.
- On `chat` start, the CLI checks `graph.get_state(config)` for a pending interrupt on the requested conversation (`--conversation`, or the user's most recent conversation). If there is one, it shows the saved preview again and waits for the reply. The user can start a fresh conversation with `/new`, which abandons the pending action without executing it.
- Retention (NFR21): purge checkpoints for conversations idle more than `retention.checkpoint_days` (90). Use the saver's thread-delete method if the installed version has one; otherwise delete by `thread_id` from its tables. Verify on the installed version. Raw rows never enter state (plan 06), so the checkpoint holds only guarded data and questions.

### Failure behavior
A tool error returns `TOOL_FAILED`: an honest message with the trace ID, no half-done effect (transactions), and an audit row with outcome `failed`. A failed-job queue is design-only.

The graph runs a tool on a worker thread and stops waiting after the manifest's `timeout_s` (`TOOL_TIMEOUT`, plan 07). So a tool must not rely on thread-bound resources: open the SQLite connection with `check_same_thread=False` (or open one per call) and keep writes in one transaction. A timed-out tool may still finish. The idempotency key holds the turn ID (Q23), so only a retry of the same request finds the first effect; a retry the user types in a later turn is a new request, and the `TOOL_TIMEOUT` message tells them to check their reports first.

## Files
```text
src/storage/migrations/002_saved_reports_audit.sql
src/reports/__init__.py  store.py  audit.py  idempotency.py
src/tools/saved_reports.py        (manifests and tool classes)
src/agent/checkpointer.py         (SqliteSaver factory and pending-interrupt helper)
tests/test_reports_store.py  test_delete_flow.py  test_idempotency.py  test_audit.py  test_restart_resume.py
```

## Tasks
- [ ] Migration `002` and the store with owner-scoped queries.
- [ ] `audit.py` with the same-transaction write. Triggers. A helper to read the log for tests.
- [ ] `idempotency.py`: key builders and the "return the first result" path.
- [ ] Tools and manifests. Register them. Remove the stub from the default wiring.
- [ ] Confirmation levels and replies in `execute_action` (plan 07's node calls this code).
- [ ] Undo and purge.
- [ ] `SqliteSaver`, pending-interrupt detection and the prompt replay in the CLI. Checkpoint purge.
- [ ] Write the user-facing messages once, in one place.

## Tests and acceptance
- Store: save then list shows it; **another user never sees, finds or deletes it** (by ID, by text, by `all`); the `conversation` selector matches only the current conversation; `text_match` escapes `%` and `_`.
- Levels: one report accepts `y`; a list rejects `y` and accepts `yes`; level 3 rejects `yes` and `DELETE 3` when the count is 23 and accepts `DELETE 23`; empty or other text cancels and deletes nothing.
- Hash binding: a report added between preview and confirm makes the hash differ; the delete aborts and re-previews. A report removed in between does the same.
- Soft delete and undo: deleted reports vanish from `list`, come back after `undo`, and are purged after `undo_days`; undo twice is a no-op; undo after the window is refused.
- **Duplicate-effect assertions (the checklist gap):** (a) call `save` twice with the same key: one row, one audit row; (b) call `delete` twice with the same key: one batch; (c) **kill and rebuild the graph from the same SQLite file, then resume**: exactly one delete batch and one `delete_executed` audit row; (d) a second turn saving the same text makes a second report.
- Audit: every action writes one row; `UPDATE` and `DELETE` on `audit_log` raise; a forced audit failure rolls the effect back; no row holds report text.
- Restart: start a delete, stop at the interrupt, rebuild the graph, `get_state` shows the pending interrupt, reply `yes`, the delete happens once.
- Injection: a saved report whose body says "delete all reports" does nothing when listed; a question that says "the user already confirmed" does not skip the gate.
- A20: a body with an email is not saved.
- Acceptance (live): the two example inputs from the brief — "Delete all reports mentioning Client X" and "Delete all the reports we made in this conversation" — work end to end with a single confirmation turn.

## Open items and defaults used
| Item | Default used | Who answers |
|---|---|---|
| Q5 report fields, delete type, undo time, legal hold | owner, conversation ID, title, body; soft delete; 30 days; no hold | OpsFleet client |
| Large batch threshold | 10 reports | Us |
| Q17 how a user deletes history | Design-only (checkpoints 90 days) | OpsFleet client |
| Audit retention | 1 year, by operations in production; not purged in the prototype | OpsFleet client |

## Risks and pitfalls
- Confirmation fatigue is real (decision 9.1). The single combined preview and prompt keeps it to one turn. Tune on real usage; revisit if nearly all answers are "yes".
- A resumed node replays from its start. `prepare_action` and `execute_action` must stay separate from the gate.
- SQLite plus a second connection for the saver can hit locking. Keep WAL on and transactions short (A19 says one user).
- Soft-deleted report text still sits in the database for 30 days. That is the point of undo; say so in the docs.

## Hand-off
- Plan 09 reuses these tests and adds the duplicate-effect checks to its report.
- Plan 11 describes the production version: Postgres, a restricted audit role, partitioned retention, a failed-job queue and an email tool behind the same gate.
