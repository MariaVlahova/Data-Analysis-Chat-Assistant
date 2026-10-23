# Plan 02: Observability
Status: done (code and tests, all offline; deviations and additions are listed under "As built")
Estimate: about 0.75 h (our estimate) · Depends on: 01 · Blocks: 03 to 09 (they all emit spans)

**Goal:** Give every turn a short trace ID with spans for each step, model call, tool call and query, mask PII out of every span and trace (not only the metrics row), write one PII-free metrics row per turn (task success, guard blocks, PII at output, latency, cost) to runs.jsonl, and expose them through the /trace command and a metrics summary command.

Tags and `(impl)` marks are explained in plan 01.

## Requirements covered
| ID | Requirement | Source |
|---|---|---|
| F40 | Each turn has a short trace ID. Every step, model call, tool call and query is a span. | PRD 6.5 |
| F41 | Traces record model, prompt and persona versions, plus latency and errors. Token counts are our own addition. | PRD 6.5 |
| F42 | One metrics row per turn in `runs.jsonl`: task success, guard blocks, PII at output, cost per question. Latency is our own addition. | decision 3.12 [DECIDED] |
| F43 | `/trace` shows the last turn's full step list. A metrics summary command shows totals. Lookup of any past turn by trace ID is our own extension. | PRD 6.5 |
| F44 | Logs and traces are masked so they never carry raw PII | S6 fix, Q25 default [PROPOSED] |
| NFR7 | BigQuery use stays inside the free 1 TB per month | [DECIDED] |
| NFR20, NFR22 | Alert on any PII at output; how long traces are kept | [PROPOSED] / [NOT SPECIFIED] |

## Decisions that apply
- 3.12 [DECIDED]: OpenTelemetry spans per step, model call, tool call and query. Production sends them to Langfuse (self-hosted) and Cloud Trace. A short trace ID appears in each error and opens the full conversation view. Each trace records model, prompt and persona versions. LangSmith was considered and rejected.
- §8 prototype stand-in: local span log, `runs.jsonl`, `/trace` and metrics commands, optional Phoenix.
- §6: the audit log is separate from traces (plan 08). Metrics events go to `runs.jsonl` in the prototype.
- S6 [High]: `bq_client.py` logs full error text, so logs and traces can hold data values. Fix: mask logs and set a PII rule for traces. The plan 03 wrapper masks errors; this plan masks every span and log line.
- Loop-engineering checklist: guard decisions and approvals should appear in the traces. Each number in an answer is not linked to the sub-query it came from (yellow). We add both below.
- Q25 default: alert on any PII, keep logs 90 days, mask PII before writing.

## Scope
- In: tracer, span log, masking, logging filter, metrics row and aggregator, `/trace`, `metrics` command, the shared `pii.py`, retention cleanup, local alert line.
- Out: OpenTelemetry SDK, Langfuse, Cloud Trace, Pub/Sub, BigQuery metrics, dashboards (design-only, F45, plan 11).
- Out: the audit log (plan 08).

## Interfaces this plan provides
| Name | Use |
|---|---|
All names import from `src.observability` unless noted.

| Name | Use |
|---|---|
| `Tracer.start_turn(user_hash, conversation_id, turn_id, *, trace_id=None, question=None, chaos=False, persona_version=None)` | Context manager. Creates the trace ID (or keeps the one the user was already shown) and the turn root span. Always writes the trace file and the metrics row, even if the turn crashes (`finally`). The chat builds one `Tracer` per session (`session.tracer`). |
| `span(name, kind="step", **attrs)` | Context manager. Kinds: `step`, `llm`, `tool`, `query`, `guard`, `hitl`. Nesting uses `contextvars`. `name` and `kind` cannot also be attribute names. Outside a turn it is a quiet no-op. |
| `event(name, **attrs)` | A point-in-time record inside the current span. |
| `guard_decision(guard, outcome, rule=None, **detail)` | Event `guard.decision` with outcome `pass`, `rewrite` or `block`. Guards: `input`, `sql`, `result`, `output`, `numeric`, `hitl`. A `block` also increments the turn's guard-block counters. An unknown guard or outcome raises `ValueError`. |
| `current_trace_id()` | For `friendly()` error messages and BigQuery labels. `None` outside a turn. |
| `turn_metrics()` | The running turn's `TurnMetrics` (a throwaway one outside a turn). Methods: `add_llm_call(model, tokens_in=, tokens_out=, fallback_used=, prompt_version=)` (every attempt), `add_query(bytes_billed)` (executed queries only), `add_guard_block(guard)`, `set_outcome(outcome)`, `set_intent(intent, turn_kind)`, `add_pii_at_output(n)`, `add_repair()`, `add_replan()`, `set_subquestions(n)`, `add_prompt_version(v)`, `set_persona_version(label)`. |
| `prompt_attrs(template_id, version, text)` | Span attributes for a prompt: ID, `id@version+sha8`, hash and length. The text is added only with `--debug-prompts`, and then masked. Use it as `span("llm.call", "llm", **prompt_attrs(...), model=...)`. |
| `src.pii.scan(text)` and `src.pii.mask(text)` | Shared by this plan, plan 03 (error masking) and plan 06 (Result Guard and Output Guard). |
| `PIIMaskingFilter`, `src.logging_setup.setup_logging()` | The filter is on every handler of the `src` logger. |

Two things the plans that emit spans must do themselves: a model call is **both** a span (`llm.call`) and a `turn_metrics().add_llm_call(...)`, and an executed query is **both** a span (`bq.query`) and `turn_metrics().add_query(...)`. The span shows the detail, the counter feeds `runs.jsonl`.

## Design
### Trace IDs and spans
- Trace ID: 8 hex characters from `secrets.token_hex(4)`. Short enough to read out loud and type into `/trace`. Span ID: 4 hex characters. (impl)
- Span record is OpenTelemetry-shaped so a later swap to the OTel SDK is mechanical: `trace_id, span_id, parent_id, name, kind, start_ts, end_ts, duration_ms, status (ok|error), error_kind, attrs, events`.
- Storage: one file per turn, `data/traces/<trace_id>.jsonl`. Buffered in memory and flushed at turn end, so a lookup by trace ID is one file read.
- Spans every plan must emit:

| Span name | Emitted by | Key attrs (all masked, no cell values) |
|---|---|---|
| `turn` | 02 | user_hash, conversation_id, turn_id, intent, turn_kind, outcome |
| `input_guard`, `context_assembly`, `planner`, `sql_loop`, `result_guard`, `evidence_check`, `composer`, `output_guard`, `tool_router`, `hitl_gate` | 07 | step status, counts |
| `llm.call` | 04 | step, model, attempt, tokens in/out, latency, retry reason, fallback used, prompt version |
| `bq.dry_run`, `bq.query` | 03 | final SQL (masked), bytes processed, bytes billed, rows, cache hit, error kind |
| `guard.sql` span (kind `guard`) | 05 | one around every SQL Guard check, holding its `guard.decision` event. Attributes: scope mode, fingerprint, tables, limit, rewrites, per_customer, demographic. Never the SQL (the `bq.*` spans hold that, masked) |
| `guard.decision` event | 05, 06, 07, 08 | guard, outcome, rule ID |
| `hitl.requested`, `hitl.approved`, `hitl.denied` events | 07, 08 | level, count, id-set hash (never the report text) |
| `numeric_check` event | 07 | number checked, matching sub-question ID (the provenance gap in the checklist) |

### What may be stored (the PII rule, F44, S6)
- Every string attribute passes through `pii.mask()` and is cut to 2000 characters.
- Never attach a DataFrame, a list of rows, or a cell value. Query spans record the SQL, row count, bytes and column names only.
- Prompts are not stored by default. Store prompt template ID, version, a hash and the character count. A `--debug-prompts` flag stores the text, still masked. (impl)
- LLM outputs are stored after masking. The final answer is stored after the Output Guard, so it is already clean. The Composer draft that the Output Guard blocked is stored masked.
- The user's question is stored masked (a user may type an email in the question).
- Identity in traces and metrics is `user_hash` only.
- Version tags (F41): model ID as actually used (after fallback), prompt version (template constant plus a hash of the text), persona version (`id@vN+sha8` from plan 01, or `none`).

### Shared PII patterns (`src/pii.py`)
Created here because the tracer needs it first. Plan 06 reuses it and adds output-only patterns.
- Patterns and tokens come from the Result Guard note: email → `[EMAIL_MASKED]`, phone → `[PHONE_MASKED]`, national ID or tax ID (for example `ddd-dd-dddd`) → `[ID_MASKED]`, card number (13 to 19 digits, Luhn-valid) → `[CARD_MASKED]`.
- `scan(text)` returns findings with type and span positions. `mask(text)` replaces them. Both are pure functions with no I/O.
- Keep false positives low: plain revenue figures such as `1,234,567` or `$12,345.67` must not match the phone or card patterns. Test this with a fixture of typical numbers.

### Logging
- `PIIMaskingFilter` rewrites every log record message and args with `pii.mask()`.
- It is attached to every handler of the `src` logger in `setup_logging()` (plan 01 keeps the root logger untouched, so other libraries stay out of `app.log`). On the handlers, not the logger: a logger filter would skip records from child loggers such as `src.cli.main`. This covers `bq_client.py`'s `f"... {str(e)}"` lines (S6) once they go through a module logger, which was changed here (see "As built").
- Log file `data/logs/app.log`. Rotating, 5 files of 2 MB. (impl)

### Metrics row (`data/runs.jsonl`, one JSON line per turn)
| Field | Meaning |
|---|---|
| `ts, trace_id, user_hash, conversation_id, turn_id` | Identity (hash only) |
| `intent, turn_kind` | From the Input Guard (`simple`, `why`, `report`, `tool`) |
| `outcome` | `answered`, `partial`, `refused`, `action_done`, `action_cancelled`, `failed` |
| `success` | True for `answered`, `partial`, `action_done`, `action_cancelled`. A correct refusal is not task success; it is counted as `refused`. (impl) |
| `error_kinds` | List of `ErrorKind` seen in the turn |
| `guard_blocks` | Counts per guard: `input`, `sql`, `result`, `output`, `numeric`, `hitl` |
| `pii_at_output` | Number of PII findings the Output Guard saw in the Composer draft. Must be 0. |
| `latency_ms` | Whole turn. Our addition. |
| `llm_calls, tokens_in, tokens_out, fallback_used` | Calls count every attempt (plan 04) |
| `bq_queries, bytes_billed` | For NFR6, NFR7 |
| `cost_usd` | Tokens times configured prices. `null` when prices are not set. We never invent prices. |
| `repairs, replans, subquestions` | Loop activity |
| `models, prompt_versions, persona_version` | Versions used |
| `chaos` | True when `--chaos` was on |

Cost: add `llm.pricing_usd_per_1m` to config with `in` and `out` per model, default `null`. Fill from the current Google pricing page when needed. The free tier means real spend is zero, so the number is an estimate of paid-tier cost. (impl)

### Commands
- `/trace [id]`: Rich tree of the last turn, or of any past turn by ID. Each node shows step name, status, duration, model, tokens, short masked summary, guard decisions inline and errors in red. `--raw` prints the JSON. Unknown ID prints a clear message, not a stack trace.
- `metrics [--last N]` (operator, all users) and `/metrics` (your own turns, the CEO's cover all): totals and rates from `runs.jsonl`: turns, success, refusal and failure rates, guard blocks by type, `pii_at_output` total, latency p50 and p95, average calls per turn, top error kinds, total estimated cost, and **month-to-date bytes billed against the 1 TB free tier with a warning at 80%** (NFR7).
- Alert (NFR20 stand-in): when any row has `pii_at_output > 0`, log CRITICAL, add an `ALERT` line to `/metrics` and print a one-line warning after the turn. Production sends a Pub/Sub event and a Cloud Monitoring alert (design-only).

### Retention (NFR22 is [NOT SPECIFIED])
- Default 90 days for trace files (Q25 default). A purge runs at startup and deletes older files. `runs.jsonl` rows carry no PII and are kept. Config `retention.trace_days`.

## Files
```text
src/pii.py
src/observability/__init__.py  tracing.py  metrics.py  logging_setup.py  viewer.py  alerts.py
tests/test_pii.py  test_tracing.py  test_metrics.py  test_logging_mask.py  test_viewer.py  test_observability_cli.py
```

## Tasks
- [x] `src/pii.py` with the four patterns, `scan`, `mask`, and the false-positive fixture.
- [x] `tracing.py`: trace and span IDs, `contextvars` nesting, `span`, `event`, `guard_decision`, masking and truncation of attributes, flush in `finally`.
- [x] `metrics.py`: `TurnMetrics`, `write_run_row`, `summarize`, month-to-date bytes.
- [x] `logging_setup.py`: filter and rotating handler. Wire it into plan 01's `setup_logging()`.
- [x] `viewer.py` and the `/trace` and `metrics` commands in the CLI.
- [x] Retention purge at startup. Alert helper.
- [x] Document the span names and fields in this plan's table so plans 03 to 08 emit the same names.

## Tests and acceptance
- `test_pii`: each pattern masks a sample; revenue and count numbers do not match; masking is idempotent.
- `test_tracing`: nested spans have correct parents; an exception inside a span records `status=error` and the error kind, and the turn still writes its file and metrics row; a canary string such as `canary@example.test` placed in a question, a SQL literal and an error message never appears in the trace file or the log file.
- `test_metrics`: a fixture `runs.jsonl` gives the expected rates, percentiles and month-to-date bytes; a row with `pii_at_output = 1` triggers the alert line.
- `/trace` renders a stored turn and prints a clear message for an unknown ID.
- Acceptance: after a failed chaos turn, the user sees a trace ID, `/trace <id>` shows which step failed and why, and `runs.jsonl` has a matching row.

## As built
Choices made while building. Each is ours `(impl)` unless a source is named.

| Topic | What was built | Why |
|---|---|---|
| Exception text | A span stores the error kind, the exception class and, for a crash, the call sites. Never the exception text. An `AgentError` also stores its short `detail`, masked **when the error is built** (masked first, then cut to 300 characters, so an email on the cut cannot survive as a fragment). | Plan 01 already keeps exception text out of the log, and `RuntimeError("password=hunter2")` has no PII pattern to catch it. Plan 01 says "masked traceback in the trace (F36)": this is the stricter reading. |
| Who may open a trace | `/trace <id>` shows a turn only to the user who made it, or to the CEO (all brands). Anyone else gets the same "No trace found" message as for an unknown ID. The plain `trace` command is the operator view and has no limit. | A trace holds the question and the SQL, which name brands. A brand manager must not read another brand's turn (S1). |
| Who sees which metrics | In the chat, `/metrics` counts the user's own turns; the CEO sees all. So another user's error mix and PII-incident trace IDs stay hidden. The month-to-date BigQuery bytes always cover every user: the free tier is shared and one number identifies nobody. The `metrics` command is the operator view (all users). | Same rule as `/trace`. |
| Trace ID shape | Exactly 8 lowercase hex characters, what `new_trace_id()` makes (capitals typed by a user are lower-cased). One function (`trace_file`) turns an ID into a path, for the tracer and the viewer, and the purge pattern is built from the same shape. | A looser pattern accepted Windows device names (`/trace con` made the read block on the device), a trailing newline, and let `runs.jsonl` match the purge pattern. |
| Control flow | A LangGraph pause (`GraphInterrupt` and its parents, raised by `interrupt()`) is not an error: the span stays `ok` and gets `paused=<class>`. A turn that ends on one defaults to `answered`; plan 07 and 08 set the real outcome. Matched by class name, so `tracing.py` does not import langgraph. | The HITL gate would otherwise show as an internal crash in every delete flow. |
| Turn number | The turn number comes from the database inside the traced block (`TurnTrace.set_turn_id`). If that fails, the trace has turn `t-?` and a `failed` row. | The user is always shown a trace ID, so it must always have a stored trace. |
| Writes | The trace file and the metrics row are written independently, and a failure of any kind (not only `OSError`) in one never costs the other. A failed trace write removes its temp file, and the purge also removes old `*.jsonl.tmp` files. Text that cannot be encoded as UTF-8 (a lone surrogate) is replaced by `?` inside `pii.mask`, so no write path can fail on it. | A pasted lone surrogate used to lose the trace and the row. |
| Reading | Files are read as bytes and decoded with replacement, and split on `\n` only (not `str.splitlines()`, which also cuts at U+2028, U+2029, U+0085). A bad line is skipped and counted. A file that exists but cannot be opened gives a clear message, not an empty history and not a traceback. | A pasted U+2028 in a question cut the root span in two, so the owner got "No trace found" for their own turn. |
| Errors through nested spans | One error counts once in `error_kinds`. Every open span is marked `error`, but only the span that raised carries `error_type` and `error_detail`. | `/trace` then shows the failing step and the reason once, not on every parent. |
| Default outcome | A turn that nobody called `set_outcome` on is `answered` when it ends cleanly, `refused` for `OUT_OF_SCOPE`, `GUARD_VIOLATION` or `INPUT_REJECTED`, and `failed` for any other error. `REFUSAL_KINDS` moved to `src/errors.py` so the tracer and the CLI share it. | Plan 07 sets richer outcomes (`partial`, `action_done`, `action_cancelled`). |
| Cost | `cost_usd` is `null` whenever no prices are set, and `null` when a model used has no price. With prices set, a turn with no model call costs `0.0`. The summary adds up the priced turns and says how many were left out, or "not set" when none is priced. New config key `llm.pricing_usd_per_1m: {model: {in, out}}`, default `null`. | We never invent prices, and unpriced spend must not show as `$0.0000`. |
| Data folder | `observability.metrics_file` and `traces_dir` default to `null`: `runs.jsonl` and `traces/` inside `app.data_dir`, next to the database and the log. Set a path only to move them. | They used fixed `data/...` paths, so a changed `data_dir` split the runtime files across two folders. |
| Numbers in attributes | numpy and pandas scalars, `Decimal` and the other number types keep their value. A Series, array, DataFrame or any object is stored as its type name only. | `rows=np.int64(5)` showed as `<int64>`. A one-cell Series must not be unpacked into a cell value. |
| Free tier | 1 TiB (2^40 bytes), warning at 80% of it, counted over the calendar month in UTC across the whole `runs.jsonl`, also when `--last N` is used. | NFR7. Google's "1 TB" free tier is counted in TiB. |
| Percentiles | Nearest rank. | No extra dependency, easy to check by hand. |
| Log rotation | 5 files of 2 MB (the live file and 4 old ones). Plan 01 had 4 files of 1 MB. | This plan's design. |
| `bq_client.py` | Its `logging` calls now use a module logger under `src`, and its three error lines log the exception class and call sites (`crash_summary`) instead of `str(e)`. Nothing else in the file changed. | They used the root logger, so they skipped the masked `app.log` and went unmasked to stderr (S6). Masking alone is not enough for this text: a BigQuery error can quote `WHERE users.id = 12345` or a name, which no pattern finds. Plan 03 replaces the file. |
| Extra commands | Typer `trace [id] [--raw]` and `metrics [--last N]` (plan 01 listed them for this plan), and the `chat --debug-prompts` flag. | F43 and the prompt rule. |
| Echo turn | The stub turn emits one `echo` step span so `/trace` has something to show before plan 07. | Plan 07 replaces it. |
| What the patterns cover | Best effort, and tested on the shapes people actually write: `1-800-555-0199`, `(415) 555-2671`, `+49 30 123456`, `0049 30 123456`, a number next to a comma, dot, dash or suffix (`...,555-123-4567,NY`, `...x12`), `SSN-123-45-6789`, and email local parts with `'`, `&`, `!`, `+`, `=` and the other RFC 5322 characters. A bare run of digits is never a phone or an ID, so counts and revenue pass. Names, street addresses and bare customer IDs are **not** found: the design keeps them out of traces by never storing rows, cell values or exception text. | Plan 06 adds the output-only patterns on top. |
| Log masking cost | A log record is masked once, however many handlers see it (`--debug` adds a second one). | Each handler has its own filter, so the first one marks the record. |
| Limits | At most 500 spans per turn, 100 events per span, 50 list items per attribute, depth 3. Past that the data is dropped and counted (`spans_dropped`, `events_dropped`). | A runaway loop must not fill the disk. |
| Threads | `contextvars` do not cross into worker threads. A worker must run inside `contextvars.copy_context()` to see the turn. | Matters only if plan 07 sets `parallel_subqueries` above 1 (S12). |

## Open items and defaults used
| Item | Default used | Who answers |
|---|---|---|
| Q25 metrics and alert limits, log retention | Alert on any PII, 90 days, mask before writing | OpsFleet client |
| NFR22 traces and logs kept | 90 days (default only) | OpsFleet client |
| Cost prices | `null` until filled | Us |
| Phoenix (optional in §8) | Not used. The local span log is enough. | Us |

## Risks and pitfalls
- A trace that stores a prompt with guarded rows is still data at rest. Keep prompt storage off by default.
- Regex masking is best effort. It is a second line behind the SQL and Result Guards, not the first.
- Forgetting the `finally` flush loses the trace of exactly the turns you need to debug.

## Hand-off
- Plans 03 to 08 emit spans and call `guard_decision` using the names in the table above.
- Plan 06 imports `pii.py` and extends it.
- Plan 09 reads `runs.jsonl` to compute the PRD section 9 metrics.
- Plan 11 describes the production stack (OTel, Langfuse, Cloud Trace, Pub/Sub, BigQuery) as design.
