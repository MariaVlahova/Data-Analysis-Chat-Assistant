# Plan 02: Observability
Status: not started
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
| `tracer.start_turn(user_hash, conversation_id, turn_id)` | Context manager. Creates the trace ID and the turn root span. Always writes the trace file and the metrics row, even if the turn crashes (`finally`). |
| `span(name, kind, **attrs)` | Context manager. Kinds: `step`, `llm`, `tool`, `query`, `guard`, `hitl`. Nesting uses `contextvars`. |
| `event(name, **attrs)` | A point-in-time record inside the current span. |
| `guard_decision(guard, outcome, rule, **detail)` | Event `guard.decision` with outcome `pass`, `rewrite` or `block`. Also increments the turn's guard-block counters. |
| `current_trace_id()` | For `friendly()` error messages and BigQuery labels. |
| `turn_metrics` (current `TurnMetrics`) | Other plans call `.add_llm_call()`, `.add_query()`, `.add_guard_block()`, `.set_outcome()`. |
| `pii.scan(text)` and `pii.mask(text)` | Shared by this plan, plan 03 (error masking) and plan 06 (Result Guard and Output Guard). |
| `PIIMaskingFilter`, `setup_logging()` | Attached to the root logger. |

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
- It is attached to the root logger's handlers in `setup_logging()` (plan 01). This covers `bq_client.py`'s `logging.error(f"... {str(e)}")` lines (S6) even before plan 03 edits them.
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
- `metrics [--last N]` and `/metrics`: totals and rates from `runs.jsonl`: turns, success, refusal and failure rates, guard blocks by type, `pii_at_output` total, latency p50 and p95, average calls per turn, top error kinds, total estimated cost, and **month-to-date bytes billed against the 1 TB free tier with a warning at 80%** (NFR7).
- Alert (NFR20 stand-in): when any row has `pii_at_output > 0`, log CRITICAL, add an `ALERT` line to `/metrics` and print a one-line warning after the turn. Production sends a Pub/Sub event and a Cloud Monitoring alert (design-only).

### Retention (NFR22 is [NOT SPECIFIED])
- Default 90 days for trace files (Q25 default). A purge runs at startup and deletes older files. `runs.jsonl` rows carry no PII and are kept. Config `retention.trace_days`.

## Files
```text
src/pii.py
src/observability/__init__.py  tracing.py  metrics.py  logging_setup.py  viewer.py  alerts.py
tests/test_pii.py  test_tracing.py  test_metrics.py  test_logging_mask.py
```

## Tasks
- [ ] `src/pii.py` with the four patterns, `scan`, `mask`, and the false-positive fixture.
- [ ] `tracing.py`: trace and span IDs, `contextvars` nesting, `span`, `event`, `guard_decision`, masking and truncation of attributes, flush in `finally`.
- [ ] `metrics.py`: `TurnMetrics`, `write_run_row`, `summarize`, month-to-date bytes.
- [ ] `logging_setup.py`: filter and rotating handler. Wire it into plan 01's `setup_logging()`.
- [ ] `viewer.py` and the `/trace` and `metrics` commands in the CLI.
- [ ] Retention purge at startup. Alert helper.
- [ ] Document the span names and fields in this plan's table so plans 03 to 08 emit the same names.

## Tests and acceptance
- `test_pii`: each pattern masks a sample; revenue and count numbers do not match; masking is idempotent.
- `test_tracing`: nested spans have correct parents; an exception inside a span records `status=error` and the error kind, and the turn still writes its file and metrics row; a canary string such as `canary@example.test` placed in a question, a SQL literal and an error message never appears in the trace file or the log file.
- `test_metrics`: a fixture `runs.jsonl` gives the expected rates, percentiles and month-to-date bytes; a row with `pii_at_output = 1` triggers the alert line.
- `/trace` renders a stored turn and prints a clear message for an unknown ID.
- Acceptance: after a failed chaos turn, the user sees a trace ID, `/trace <id>` shows which step failed and why, and `runs.jsonl` has a matching row.

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
