# Plan 08: Saved Reports and confirmation flow
Status: built and tested offline with the scripted gateway and the fake BigQuery client (177 tests in the five plan 08 files; 2709 in the offline suite; the findings of two code reviews are fixed). One step needs a Google sign-in and a Gemini key and is NOT done: the acceptance run of the two example inputs from the brief on the real model (see "Still to do"). The saved reports, the audit log and the checkpoints are real SQLite; the stub of plan 07 is only used by the router tests.
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
  search_text     TEXT NOT NULL,               -- (impl) the part of the body a text search looks in; see "What was decided while building"
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
  undone_at TEXT, idempotency_key TEXT NOT NULL UNIQUE,
  undo_key TEXT UNIQUE                         -- (impl) the key of the undo that restored it, so a retried undo restores nothing more
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

`allowed_roles`: any role in the prototype. `timeout_s`: 5. Registered through the registry from plan 07 with no graph change. The in-memory stub of plan 07 is deleted: the real store has the same tests (it was kept for the router tests until a review found it was a parallel copy of the store's rules that could drift).

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
- On `chat` start, the CLI checks `graph.get_state(config)` for a pending interrupt on the requested conversation (`--conversation`, or else the user's most recently active conversation that has a pause on file). If there is one, it shows the saved preview again and waits for the reply. The user can start a fresh conversation with `/new`, which abandons the pending action without executing it.
- Retention (NFR21): purge checkpoints for conversations idle more than `retention.checkpoint_days` (90). Use the saver's thread-delete method if the installed version has one; otherwise delete by `thread_id` from its tables. Verify on the installed version. Raw rows never enter state (plan 06), so the checkpoint holds only guarded data and questions.

### Failure behavior
A tool error returns `TOOL_FAILED`: an honest message with the trace ID, no half-done effect (transactions), and an audit row with outcome `failed`. A failed-job queue is design-only.

The graph runs a tool on a worker thread and stops waiting after the manifest's `timeout_s` (`TOOL_TIMEOUT`, plan 07). So a tool must not rely on thread-bound resources: open the SQLite connection with `check_same_thread=False` (or open one per call) and keep writes in one transaction. A timed-out tool may still finish. The idempotency key holds the turn ID (Q23), so only a retry of the same request finds the first effect; a retry the user types in a later turn is a new request, and the `TOOL_TIMEOUT` message tells them to check their reports first.

## Files
```text
src/storage/migrations/002_saved_reports_audit.sql  003_report_indexes.sql
src/reports/__init__.py  store.py  idempotency.py  messages.py
src/storage/audit.py              (the audit log: any gated tool writes to it, so it is not in the reports package)
src/tools/saved_reports.py        (manifests and tool classes)
src/agent/checkpointer.py         (SqliteSaver factory, checkpoint purge)
tests/test_reports_store.py  test_delete_flow.py  test_idempotency.py  test_audit.py  test_restart_resume.py
tests/reports_support.py          (the rig: real tools on a migrated file, a movable clock, restart())
```
Changed, not new: `storage/db.py` (`open_connection`, `iso`, `now_utc`), `agent/deps.py` (the `audit` hook), `agent/nodes/tools.py` (gate events to the audit log, the cancel text from the manifest), `agent/nodes/input_guard.py` (`typed_text`), `agent/nodes/finish.py` and `agent/nodes/policy.py` (the labels the user was shown), `agent/state.py` (`shown_labels`), `guards/pseudonymizer.py` and `guards/result_guard.py` (`retire_labels`), `agent/runtime.py` (default wiring, `pending_confirmation`, `abandon_pending`, pruning), `tools/base.py` (`idempotency_key`, `ToolManifest.past`, `audit_verb`), `tools/registry.py` (the verb check), `cli/main.py` and `cli/commands.py` (startup purges, checkpointer, resume, `/new`). `tools/stub_reports.py` is gone.

## Tasks
- [x] Migration `002` and the store with owner-scoped queries.
- [x] `storage/audit.py` with the same-transaction write. Triggers. A helper to read the log for tests.
- [x] `idempotency.py`: key builders and the "return the first result" path.
- [x] Tools and manifests. Register them. Remove the stub from the default wiring.
- [x] Confirmation levels and replies in `execute_action` (plan 07's node calls this code).
- [x] Undo and purge.
- [x] `SqliteSaver`, pending-interrupt detection and the prompt replay in the CLI. Checkpoint purge.
- [x] Write the user-facing messages once, in one place.
- [ ] Acceptance run of the two example inputs on the real model. **Needs a Gemini key.** (See "Still to do".)

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

## What was decided while building (impl)
- **The store opens one short connection per call.** The graph runs a tool on a worker thread, so nothing is shared between threads. `BEGIN IMMEDIATE` takes the write lock first, so two calls with one key cannot both write (a test runs eight threads). `storage.db.open_connection` sets the pragmas for them and for the audit log. The checkpoint saver needs Python's default transactions and a connection it shares between threads, so `agent/checkpointer.py` opens its own (WAL).
- **`search_text` is a column of its own.** The saved body is the whole answer, with its "How I read your question" block, the scope line and the notes. A search over the body would make "delete reports mentioning Brand B" match every report of a user who sees Brand B (a plan 07 review finding). `title` and `search_text` (the answer without its envelope) are searched; `body` is kept whole for the report itself.
- **`text_match` folds case in every alphabet.** SQLite's `LIKE` and `lower()` fold ASCII only, so the store registers a `fold()` function (`str.casefold`) and compares folded text with `LIKE ... ESCAPE '\'`. `%`, `_` and `\` are plain characters; an empty search finds nothing, never everything.
- **The audit hook is generic, and the audit log is not a reports thing.** The graph never names a tool, so the gate's steps go to `Deps.audit`, with the action named from the manifest's verb: `delete_requested`, `delete_confirmed`, `delete_cancelled`, `delete_stale`. A gated tool of another kind gets `send_requested` and so on with no change. `AuditEntry`, `write_entry` and `AuditLog` are in `src/storage/audit.py`, beside the table, so the graph imports nothing from `src/reports` (a test loads the graph in a clean interpreter and checks). The effect's own row (`save`, `delete_executed`, `undo`, `purge`) is written by the store in the transaction of the effect. A gate row that cannot be written stops the step with `TOOL_FAILED` before anything is changed.
- **`ReportStore` gets the keyed hash function.** Its `target_hash` is `id_set_hash(ids, key)` with the installation's key, the same value the gate puts in `pending_action`, so the rows `delete_requested`, `delete_confirmed` and `delete_executed` of one delete carry one `target_hash`, and a `save` carries the hash of its one ID. Nothing in the audit log is invertible, and no row holds an ID, a title or text (a test searches the whole log).
- **Idempotency keys: one recipe.** `tools.base.idempotency_key` (parts joined with `|`, SHA-256) is the only copy; the graph uses it for the key of a write or gated tool (`conversation | turn | tool` for a write, `conversation | request turn | tool | ID-set hash` for a delete) and `reports/idempotency.py` uses it for the callers that bring none (a test checks the two agree). `save` adds the hash of the text to whichever key it has (the graph's, or `conversation | turn | save`), so the plan's body-hash recipe is live in the real path: the same request key with other text is another request. The first-result path is `idempotency.first_result`, run inside the transaction that would make the effect, and it looks the key up **by owner** as every other query does: a key another owner made finds nothing, and the insert then fails on the UNIQUE key (closed), so no one is handed another owner's report, batch or count.
- **A retried undo restores nothing more.** Undo walks back one delete at a time (plan 07's decision), so a retry of the same request must not walk back two. The key is stored on the batch it restored (`undo_key`), and the same key returns that result.
- **Refusals and failures are audited too, and a repeat writes no second row.** A save that the Output Guard's scan stops (A20) writes `save` with outcome `blocked`. A tool that raises writes its action with outcome `failed` (best effort, it never raises) and the graph reports `TOOL_FAILED` with the trace ID. An undo that finds nothing writes its outcome (`none`, `already`, `expired`). A delete that finds nothing writes `no_match`. These have no stored key to find, so they write with `once=True`: a row with the same action, key and outcome is not added again. A repeat of a failure is recorded each time, because it failed each time.
- **The cancel text comes from the manifest.** `ToolManifest.past` (and the known default verbs: "deleted", "sent") gives "Cancelled. Nothing was deleted." for this tool and "Nothing was sent." for an external-send tool. A tool with another verb names its past tense; without one the text says "Nothing was changed", because building "droped" or "notifyed" by rule would put a wrong word in front of the user.
- **The default wiring is the real store, and there is no second copy of it.** `build_agent` builds `ReportStore`, the tools and the audit hook when no `registry` is passed. The test rig (`tests/agent_support.make_rig`) migrates the project's database and adds the conversation row a report points to, so the graph tests run on the real tools. The router tests that are about any tool use dummy tools; the ones about reports use `ReportsEnv`.
- **Startup (`open_session`) does three things in this order:** migrate, remove the reports whose `purge_after` has passed (`reports.store.purge_expired`, audit `purge` with the count when any), then open the `SqliteSaver` and delete the checkpoints of conversations idle more than `retention.checkpoint_days` (the saver's own `delete_thread`, checked on langgraph-checkpoint-sqlite 3.1.2). One query finds the idle conversations that still have checkpoints, so the cost follows what is left to delete and not the whole history. Reports of a purged conversation stay. **Both purges are optional:** a failure (the database is busy, a trigger refuses a row) is logged with where it happened and the chat starts anyway; the next start tries again.
- **Which conversation a new chat opens.** With `--conversation` that one. Without it, the user's most recently active conversation that is waiting at the gate, so a restart finds the preview even when another conversation was used after it; otherwise a new one, as before. "Waiting" is found with one query on the saver's tables (a pause is a write on the `__interrupt__` channel; a test checks that name on the installed LangGraph), then `pending_confirmation` confirms each candidate: a chat start no longer loads a checkpoint for every recent conversation, most of which have none. The search is optional like the purges: a checkpoint that cannot be read is logged and skipped, and the chat starts. The preview and prompt are shown again with "This conversation is waiting for your confirmation", and the next typed line is the reply.
- **`/new` drops the wait.** `AgentRuntime.abandon_pending` ends the thread with `update_state(..., as_node="finish")`, so the old request cannot be confirmed later, not even by `--conversation`, and only then writes `delete_cancelled` with outcome `abandoned`: if closing fails, no row says "abandoned" about a request that is still waiting. A row that cannot be written is logged and does not undo the close.
- **A conversation is waiting only when its prompt exists.** `awaiting_confirmation` and `pending_confirmation` both read the same thing: the gate is next *and* its interrupt is recorded. A checkpoint can also stop just before the gate has run (Ctrl+C, a crash between two steps); then nothing was asked, so the user's next line is a new question and not a blind reply to `DELETE n`, and the chat shows nothing for it. `run` takes one look at the checkpoint per line and, if a cut-off run left steps behind, closes it first, so those steps cannot run later with another turn's state. A row `<verb>_cancelled` / `abandoned` is written for that only when the run stopped **at the gate** of a gated tool with its preview built, i.e. a request that was never confirmed. A run cut off in a tool that is not gated, or after the confirmation (the effect may have committed, and has its own row), gets no such row: the trail never says a delete was both executed and cancelled.
- **What the user types is cleaned and masked before it is stored.** `AgentRuntime.run` passes the typed line through `typed_text` (hidden characters removed, **then** `pii.mask`, as the tracer masks) before it enters state, so the checkpoint, the history, the prompts and a title never hold a raw e-mail, phone or card number; the Input Guard's rewrite gets the same. The order matters: a zero-width character or a soft hyphen inside an address breaks the pattern, so masking first would miss it and the cleaning would then put the plain address back. A question that held something personal never becomes a title (a mask token in it gives "Saved answer"). A confirmation reply gets the same, so `ye<zero-width>s` is `yes`; `yes` and `DELETE 23` compare exactly as before. Answers are the ones the Output Guard passed; they and the questions stay in the app database for `retention.checkpoint_days`.
- **Labels from an earlier process are still not IDs.** A resumed conversation has shown `Customer #48213`, but the new process's pseudonymizer never issued it, so a rewritten follow-up ("customer 48213") would pass `refers_to`. `finish` now keeps the number of every label an answer showed in state (`shown_labels`, whole answers, not the 600-character cut of the history; a label is only a random number, so this keeps nothing real) and `policy_context` hands them to `SessionPseudonymizer.retire_labels` at the start of every turn: their numbers are refused in a query, and no customer is given the same label again. A test shows a label deep in a long answer, and one from an answer older than the history keeps.
- **The checkpoints of a conversation in use are pruned.** The saver writes the whole state at every step; only the newest checkpoint is needed to carry on. After each turn `AgentRuntime` keeps the newest checkpoint of the conversation and its pending writes (the pause at the gate is one) and deletes the rest (`checkpointer.prune_thread`; `SqliteSaver.prune` raises `NotImplementedError` in the installed version). A failure is logged and does not fail the turn. The in-memory saver of the tests is not pruned, so the tests that search every checkpoint for a leak still see them all.
- **Undo, purge and the history of deletes.** Undo reads one row (the newest open delete, through `idx_batches_owner` from migration `003`), not the owner's whole history. A delete with nothing left to restore (its rows are gone) answers "can no longer be undone" and is **not** marked undone, so a retry of the same request cannot answer "Restored N" and a later undo cannot answer "already undone" for reports that never came back. The purge and the undo use the same test for the end of the window (after `purge_after`, not at it), and the purge also deletes the batches past their window, so `delete_batches` does not grow for ever. The audit lookup of a request with no effect uses `idx_audit_key`.
- **One clock, and no naive times.** `storage.db.now_utc()` is the only clock of the store, the audit log and the purges; `iso()` refuses a datetime without a zone instead of reading it as local time.
- **A gated tool's verb is free text.** The audit names are made from `ToolManifest.audit_verb` (letters and underscores: "e-mail" gives `e_mail_requested`), and `ToolRegistry.validate` refuses a gated tool whose verb leaves no usable name, instead of failing at the first request.
- **A stale confirmation's new preview is a request.** The rows `delete_confirmed` and `delete_executed` carry the hash of the set the user confirmed, so the new preview writes its own `delete_requested`: every confirmed hash has a request row.
- **Known edges.** (1) If the process dies after the delete commits and before the checkpoint moves on, the resumed gate finds no reports and says so; the idempotency key still guarantees one batch, but the message does not say that the first delete worked. (2) If that delete is undone before the user comes back, the resumed request finds the same set, sends the same key, and gets the first result ("Deleted N") while nothing is deleted: a replay is answered from the first effect by design. Both are tested; neither can double an effect.

## Still to do
- **Acceptance on the real model, with a Gemini key.** Run "Delete all reports mentioning Client X" and "Delete all the reports we made in this conversation" end to end (save two or three answers first) and confirm the single confirmation turn in the real transcript. The Input Guard's selector extraction for these two sentences is the part only the real model can show; everything after it is tested offline. Copy the real output into the plan 11 example run, do not write it by hand.
- **Versions.** `requirements.txt` now says `langgraph>=1.0` and `langgraph-checkpoint-sqlite>=3.0`, the major versions this was tested on (1.2.16 and 3.1.2), because `SqliteSaver.delete_thread` and the interrupts on a state snapshot's tasks are needed. Try an older release only with the tests.
- **Read the confirmations on real usage.** Decision 9.1: if nearly all answers are "yes", revisit the friction levels.

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
