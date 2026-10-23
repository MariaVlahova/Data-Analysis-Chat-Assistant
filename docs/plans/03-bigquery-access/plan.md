# Plan 03: BigQuery access
Status: built and tested offline against a fake BigQuery client. Four steps need a Google sign-in and are NOT done: the live column dump (Q4), real brands in `users.yaml`, the byte cap chosen from table sizes, and the live acceptance run (see "Still to do with a Google sign-in")
Estimate: about 1 h (our estimate) · Depends on: 01, 02 · Blocks: 05 (schema), 07

**Goal:** Wrap `requirements/bq_client.py` (already copied into the prototype package as `src/data_access/bq_client.py`; the two files differ only in whitespace) so every query goes through a dry-run, a byte cap, a timeout, labels and a row cap with masked error logs, add schema lookup for questions about the data structure, and check the configured brand mapping against the real brand list at startup so a typo never silently produces an empty scope.

Tags and `(impl)` marks are explained in plan 01.

## Requirements covered
| ID | Requirement | Source |
|---|---|---|
| F5 | The agent answers questions about the data structure (this plan provides the schema catalog; plan 07 adds the path) | PRD 6.1, decision 7.3 |
| F33 | A too expensive or too slow query asks the user to narrow the question (this plan raises the typed errors) | PRD 6.4 |
| NFR6 | Bytes billed per query: a hard cap in config [OPEN] | decision §4 |
| NFR7 | Stay inside the free 1 TB per month [DECIDED] | |
| S1 [High] | `products.brand` is free text. A typo gives an empty scope. Check mapped brands at startup. | decision 9.2 |
| S2 [High] | `bq_client.py` has no dry-run, byte cap, timeout, labels or row cap. Wrap it before any query runs. | decision 9.2 |
| S6 [High] | `bq_client.py` logs the full error text. Mask logs. | decision 9.2 |
| A23, Q4 | Check that every order item joins to a brand; check the real columns and the latest date | decision §5, §10 |

## Decisions that apply
- 3.5 [PROPOSED]: order of steps is structured output, sqlglot parse, checks, scope rewrite, **dry-run, then run with a byte cap**.
- 3.4: production uses BigQuery authorized views, policy tags, a byte cap and a read-only role. The prototype uses the SQL Guard (plan 05) instead. The wrapper still sets the byte cap.
- 3.11 table: syntax error or unknown column → repair. Query too expensive or too slow → ask the user to narrow the question. Gemini and BigQuery outages → retry with growing waits.
- §8: the prototype uses the public dataset directly. The BigQuery default credentials stay (`bq_client.py` already uses them).
- NFR7: monthly use inside 1 TB. NFR6 value is [NOT SPECIFIED]; Q3 default is a low byte cap in config.
- A5: "up-to-date" metrics anchor to the dataset's latest order date, and the answer says so.

## Scope
- In: a safe query runner, error classification, row cap, labels, schema catalog (live schema, brand list, latest date, table sizes), startup brand check, masked logging in `bq_client.py`, `doctor` additions.
- Out: SQL parsing and scope rewrite (plan 05), PII masking of rows (plan 06), retry of whole turns (plan 07).
- Out: any write to BigQuery. The runner opens read-only jobs only.

## Interfaces this plan provides
```python
class SafeQueryRunner:
    def __init__(self, settings, runner: BigQueryRunner | None = None): ...
    def dry_run(self, gq: GuardedQuery, budget: TurnBudget) -> DryRunInfo        # bytes_processed, raises typed errors
    def run(self, gq: GuardedQuery, budget: TurnBudget) -> QueryOutcome           # DataFrame + stats

@dataclass
class QueryOutcome:
    df: pandas.DataFrame          # raw rows. Goes straight to the Result Guard, never into graph state.
    total_rows: int               # full count reported by BigQuery
    bytes_processed: int; bytes_billed: int; elapsed_ms: int; cache_hit: bool

class SchemaCatalog:
    tables: dict[str, list[ColumnInfo]]           # live schema, from SafeQueryRunner.get_table (GoogleSQL type names)
    def prune(self, tables: list[str]) -> str     # compact slice for the SQL loop
    def overview(self) -> str                     # high-level list for the Planner and for schema questions
    brands: frozenset[str]; latest_date: date; table_bytes: dict[str, int]

def check_brand_mapping(users, catalog) -> None   # raises ConfigError with suggestions
```
`GuardedQuery` is defined in plan 05 (`sql`, `params`, tags). Until plan 05 exists, tests use a tiny fake with the same fields.

## Design
### Wrapping `bq_client.py` (S2)
- `BigQueryRunner` (`bq_client.py`) is the only place that builds a `bigquery.Client`. `SafeQueryRunner` keeps it private and builds its own `QueryJobConfig` for every query. `BigQueryRunner` has no `execute_query` and no `get_table_schema` any more, and no `dataset_id`: the dataset is `bigquery.dataset` in the settings, read by `SafeQueryRunner` only. All agent queries go through `SafeQueryRunner`.
- Instead of a docstring warning on `execute_query`, the method was deleted, and `tests/test_no_direct_execute.py` keeps the client and the system-query type inside `src/data_access/` (see "As built").
- `bq_client.py` logs through a module logger under `src` with `crash_summary` and no error text (S6; plan 02 had already done this). The 300-character cut of error text now happens in `errors_map.py` and `AgentError`.

### Query flow in `run()`
1. **Dry run:** `QueryJobConfig(dry_run=True, use_query_cache=False, query_parameters=gq.params)`. Free and does not scan data. It reports `total_bytes_processed` and catches syntax and unknown-column errors early.
2. **Cost gate:** if `bytes_processed > bigquery.max_bytes_billed` → `QUERY_TOO_EXPENSIVE` (no retry). If it would push the turn over `budget.bytes_per_turn` → also `QUERY_TOO_EXPENSIVE`.
3. **Execute:** `QueryJobConfig(maximum_bytes_billed=cap, labels=..., query_parameters=gq.params, use_query_cache=True)` with a job timeout (`job_timeout_ms` if the installed library supports it; verify against google-cloud-bigquery 3.46.2) and `result(timeout=query_timeout_s)`. On timeout, call `job.cancel()` and raise `QUERY_TIMEOUT`.
4. **Row cap:** `result(max_results=bigquery.sql_row_limit)`. The SQL Guard already forces a `LIMIT` of that size; this is the second layer. `total_rows` comes from the result iterator so the Result Guard can write "showing top 50 of N".
5. **Account:** add `total_bytes_billed` to the turn budget and the metrics (`budget.charge_bytes`).
6. **Spans:** `bq.dry_run` and `bq.query` as in plan 02. The SQL is stored masked. No cell values.

Why dry-run first: it parses the query in BigQuery itself, costs nothing, and is where cost and syntax problems show up before any money is spent (3.5).

### Job labels (cost attribution)
`{app: opsfleet-agent, env: prototype, user: <user_hash>, trace: <trace_id>, kind: <turn_kind>}`. BigQuery labels allow lowercase letters, digits, underscores and dashes, up to 63 characters. Use the hash, never the login name. (impl)

### Error classification
Map exceptions to `ErrorKind` in one function so the loop in plan 07 never inspects raw messages.

| BigQuery signal | Kind | Notes |
|---|---|---|
| 400 with "Syntax error", "Unexpected" | `SQL_SYNTAX` | Repairable. Message is normalized and cut. |
| 400 with "Unrecognized name", "Name … not found inside", "No matching signature" | `SQL_UNKNOWN_COLUMN` | Repairable. Plan 07 adds the exact column list. |
| Bytes over the cap, `maximumBytesBilled exceeded` | `QUERY_TOO_EXPENSIVE` | Not repairable. |
| Timeout | `QUERY_TIMEOUT` | Not repairable. |
| 403, 404 on the dataset | `BQ_UNAVAILABLE` with a clear fix hint (auth, project, API enabled) | Not retried |
| 5xx, connection reset, `ServiceUnavailable` | `BQ_UNAVAILABLE` after `transient_retries` | Retry with growing waits and jitter (brief R5: resilient to third-party downtime) |

Every message is passed through `pii.mask()` and cut to 300 characters before it enters an `AgentError`, a span or a log (S6). The SQL text inside an error message is masked too.

### Schema catalog (F5, Q4)
- At startup, read the four tables with `SafeQueryRunner.get_table` (metadata calls are free, run together) and cache the result in `data/cache/schema.json` with the check time. Refresh when older than 24 hours or on `doctor --refresh`.
- **Reconcile with the allow-list** from plan 05: an allow-listed column missing from the live schema stops startup (the file is wrong). A live column that is in neither the allowed nor the blocked list is logged as "unclassified, treated as blocked" (fail closed; Q11 is open).
- `overview()` lists only allow-listed tables and columns with our own one-line descriptions (BigQuery descriptions are often empty). It never lists blocked columns. For a user question like "what data do you have?", plan 07 answers from this text, and adds the line "personal contact details are not available to this agent".
- `prune(tables)` returns just the columns of the tables a sub-question needs (the SQL loop input in the step-inputs table).
- Record `table.num_bytes` per table. Set `bigquery.max_bytes_billed` to about 5 times the largest full-table scan once these are known, so the cap stops runaway queries but never normal ones. (NFR6 is [OPEN]; document the chosen number.)

### Data freshness (A5, Q4)
- `SELECT MAX(created_at) FROM order_items` once per process, cached to the same file. It scans one column.
- Provide `reference_date = min(today_utc, latest_date)`. The Planner resolves "last month" and "up-to-date" from this and the answer states the resolved range (F6). If the data lags the calendar by more than a week, the answer says the data ends on `latest_date`.

### Startup brand check (S1)
- Query once: `SELECT DISTINCT brand FROM products WHERE brand IS NOT NULL`. Cache to `data/cache/brands.json`.
- For each user in `users.yaml` with a brand list, every brand must match a real brand exactly. A miss stops startup with: the user, the unknown brand, and up to 3 close matches from `difflib.get_close_matches` ("did you mean 'Levi's'?"). Never drop a brand silently.
- If BigQuery is unreachable at startup, use the cached list and say so. With no cache, refuse to start (fail closed, NFR15). (impl)
- Also run once in `doctor`: count order items whose product is missing or has no brand (A23). Report the count. Items without a brand are invisible to brand managers and visible only to the CEO, so a high count is a finding to write down.
- Free-text check for A21: list string columns of the four tables so a reviewer can confirm there are no free-text cells that carry PII.

## Files
```text
src/data_access/bq_client.py            (existing: masked logging only)
src/data_access/safe_runner.py  catalog.py  brand_check.py  errors_map.py
tests/test_safe_runner.py  test_catalog.py  test_brand_check.py  test_no_direct_execute.py
```

## Tasks
- [ ] Run the `bq show --schema` equivalent (`SafeQueryRunner.get_table`) for the four tables. Save the real columns. This closes Q4 and feeds plan 05's allow-list. **Needs a Google sign-in.** `python -m src.cli.main doctor --refresh` reads them and saves `data/cache/schema.json`; `pytest -m live -s` prints them.
- [x] Mask logging in `bq_client.py`. Plan 02 already did this (module logger, `crash_summary`, no error text). The unguarded `execute_query` and `get_table_schema` were then removed (see "As built").
- [x] `errors_map.py`: classify exceptions, normalize and cut messages.
- [x] `safe_runner.py`: dry run, cost gate, execute, timeout, cancel, labels, row cap, transient retries, spans, budget accounting.
- [x] `catalog.py`: live schema, cache, prune, overview, freshness, table sizes.
- [x] `brand_check.py` and the startup call in the CLI (`chat` and `doctor`).
- [ ] Choose real brands for `users.yaml` from the top brands by item count. **Needs a Google sign-in.** `doctor` prints the top brands when the brand check fails.
- [ ] Set the byte cap from the table sizes. Write the number in config. **Needs a Google sign-in.** `doctor` prints the largest table and 5 times its size; the config keeps the starting 512 MiB until then.
- [x] Extend `doctor`.

## As built
Deviations and additions, so the next plans do not rely on the old text.

| Topic | What was built |
|---|---|
| Old client | `bq_client.py` keeps only the code that creates the client (no `dataset_id`, no query or schema methods). `execute_query` and `get_table_schema` were removed instead of being fenced with a warning: nothing needed them, and a method that does not exist cannot be called by mistake. `SafeQueryRunner` keeps its client private (`_bq`). `tests/test_no_direct_execute.py` parses every file outside `src/data_access/` and fails on any import of the BigQuery library or `bq_client` (any spelling: `from google.cloud import storage, bigquery`, `import_module(...)`, an attribute chain), any `BigQueryRunner`, and any mention of `execute_query` or `SystemQuery`. The scanner is itself tested on the spellings it must catch. Python cannot stop deliberate access to a private attribute; the tests catch the accidental routes. |
| Schema read | `SafeQueryRunner.get_table()` calls `client.get_table` once per table and reads columns and `num_bytes` from it. |
| Accepted queries | `run()` and `dry_run()` accept only the types in `safe_runner.ACCEPTED_QUERY_TYPES`, now `(SystemQuery,)`. A structural match (anything with `.sql` and `.params`) is refused with `GUARD_VIOLATION`, rule `UNGUARDED_QUERY`, counted in `guard_blocks.sql`, and nothing is sent to BigQuery. **Plan 05 must add its `GuardedQuery` to that tuple**; the runner tests register their stand-in `Query` the same way. |
| Fixed system SQL | `SystemQuery(sql, params, max_rows)` is for SQL our code wrote (latest date, brand list, `doctor`). It skips the SQL Guard on purpose (the guard would scope it to one user) and keeps the dry run, byte cap, timeout and labels. Its optional `max_rows` is the only way to read more than the 1000-row answer cap, and `run()` honors it only on a `SystemQuery`. A short brand list stops startup instead of rejecting real brands. |
| Read-only (added) | After the dry run, `statement_type` must be exactly `SELECT`, else `GUARD_VIOLATION` with rule `NOT_SELECT`; a missing type is refused too (fail closed). `total_bytes_processed` that is missing (not 0) is refused as well, so a dry run that reports nothing cannot pass the cost gate or the turn budget as "free". A `NOT_SELECT` refusal is a `guard.decision` block for the SQL guard, so it shows in `guard_blocks.sql` and on the span (`statement_type`). `DryRunInfo` holds only `bytes_processed`: a value that can only be `SELECT` is not carried. **Unverified:** we could not confirm without credentials that a real dry run reports the type and the estimate. If it does not, every query is refused (`statement type None` or "did not report" in the detail), and the live test `test_a_real_dry_run_reports_the_statement_type_and_bytes` shows it first. |
| One retry loop | The library's `retry` and `job_retry` are switched off on every call. Transient failures (503, 429, `rateLimitExceeded`, connection resets, broken downloads, SSL errors, a stalled job submission) are retried by `SafeQueryRunner` with 1, 2, 4, 8 s waits scaled by 50 to 100% jitter, up to `transient_retries`. With a turn budget, every BigQuery call gets `min(query_timeout_s, time left)`, the deadline is checked before each wait, the wait is cut to the time left, and it is checked again before the retry. A timeout caused by the turn's own deadline is `TURN_TIMEOUT`, not "the question is too slow". |
| Cancel | A timeout, Ctrl+C, or a transient failure while the job runs cancels that job (best effort), so a retry never leaves a second job billing. A job BigQuery reports as failed is not cancelled. A cancelled job is charged at its dry-run estimate (BigQuery may bill a cancelled job up to its full cost): it counts in `bq_queries` and `bytes_billed` of `runs.jsonl`, in the turn budget, and on the span (`abandoned_jobs`), so a loop of timeouts is visible and the free-tier warning errs on the safe side. If the HTTP call that submits the job stalls there is no job handle to cancel; that job ends at its server-side `job_timeout_ms` (present in 3.46.2), set 5 s after the client timeout. |
| Messages | `SQL_SYNTAX` and `SQL_UNKNOWN_COLUMN` keep BigQuery's normalized message because the repair step needs the position and the unknown name. Quoted tokens are blanked first (`Unexpected identifier "Maria"` becomes `"…"`, since a quoted token can be a string literal), then it is masked and cut to 300 characters. This is the one deliberate exception to "no exception text" (recorded in `CLAUDE.md`). Every other kind gets a fixed hint plus the HTTP code and reason slug, never the message. Other 400s are treated as repairable SQL errors. |
| Phases and 404 | `classify(exc, phase)` knows where the call happened: `metadata`, `dry_run` or `query`. BigQuery's reason slug is read before the HTTP code, because several job failures that are not the query's fault come back as a 400: `timeout` is `QUERY_TIMEOUT` while the query runs; `jobBackendError`, `jobInternalError`, `tableUnavailable`, `backendError`, `resourceInUse` and `rateLimitExceeded` are retryable `BQ_UNAVAILABLE`; they are never "syntax errors" for the repair loop. A 404 on a metadata call (the configured dataset) is a non-retried setup problem with a setup message. A 404 on a query, whether it names a missing table or a missing dataset, is a repairable `SQL_UNKNOWN_COLUMN`: the metadata phase has already proven the configured dataset exists, so this is the model's mistake. A timeout is `QUERY_TIMEOUT` only in the `query` phase. A client that cannot be created at all (no project, bad key file) is a typed `BQ_UNAVAILABLE` that names GOOGLE_CLOUD_PROJECT and keeps only the exception class. |
| Types | The client library reports REST type names (`INTEGER`, `FLOAT`, `BOOLEAN`, `RECORD`). The catalog converts them to GoogleSQL names (`INT64`, `FLOAT64`, `BOOL`, `STRUCT`, `ARRAY<...>` for repeated) before they reach prompts or plan 05's allow-list. |
| Cache | `data/cache/schema.json` (columns, sizes, latest date) and `brands.json`, each valid for 24 hours, written with `fileutil.atomic_write_text` (a temp file of its own name per write, removed on failure). A fresh copy means startup makes no BigQuery call. The four table reads run together; the two system queries run one after the other because the brand query needs the column check first. When the copy is older and BigQuery cannot be reached (an outage or a timeout), a copy up to 7 days old is used and a warning is printed; the first part that finds BigQuery down is the only one that waits through the retries. Beyond 7 days, with no copy, or on any error that is not an outage (revoked access, wrong project, a cap that is too low), startup stops (fail closed): a saved copy must not hide a broken setup. With no copy, a refusal is reported as a refusal with a setup fix, an outage as an outage with a connection fix. A copy dated more than 5 minutes ahead of the clock is ignored. The startup SQL's column names are checked against the schema first, so a rename reads as "no column X", not as an outage. `SchemaCatalog` compares and hashes by identity (its dict fields rule out a field-wise hash), so prompt builders can cache on it. |
| Startup check | `chat` checks the brands of the user who is starting (S1); `doctor` checks every user. One manager's typo does not take the chat offline for the others. The chat's message names the user and the brand but no "did you mean" names, because those would be other managers' brands; `doctor` shows them. The shipped `<verified brand N>` placeholders stop the chat for alice and bob until real names are in; the CEO can start. `nobrands` is refused first, before any BigQuery call. The cheap local checks (database, persona, `--conversation`) run before the network call, which is the last step before a conversation is created; the spinner covers the imports it needs as well. `BrandMappingError` carries the problems as a list. |
| `doctor` | Five checks: BigQuery dataset (with latest date), Byte cap, Brand mapping (all users, with close matches and the top brands on failure), Items without a brand (A23), Text columns (A21: STRING, ARRAY<STRING> and JSON). `--refresh` reads again. The catalog is loaded once; any failure is reported once (and the crash class and call sites go to the masked log) and the other four are skipped. The checks do not wait for the credentials check: BigQuery's own lookup is the test. During an outage the live follow-up queries are skipped. A hint or a count that crashes never replaces the result it decorates. A placeholder brand is now a FAIL here (plan 01 had a warning), because the chat refuses to start for that user. Byte sizes use the same formatter as `metrics`. |
| Credential probe | google-auth looks for credentials on a Compute Engine metadata server when no key file or login is found: 3 s, 3 tries, about 9 s of silence on a laptop. `src/gcp_env.py` shortens it to 1 s and 1 try (`GCE_METADATA_TIMEOUT`, `GCE_METADATA_DETECT_RETRIES`, a value you set yourself is kept) before google-auth is imported, because google-auth reads the limits once at import. The probe is not switched off, so credentials from a metadata server (Compute Engine, Cloud Run, workload identity) still work. The `doctor` credentials check used to set `NO_GCE_CHECK`, which hid those credentials from the BigQuery checks in the same run; it no longer does. |
| Imports | Nothing in the CLI imports BigQuery or pandas at module level: `main.py` loads the catalog code when a chat starts, `commands.py` imports `SchemaCatalog` for type checking only, and `doctor_bigquery.py` imports inside its checks. `trace`, `metrics` and a `doctor` on a broken install stay fast and working (`tests/test_cli_imports.py`). Shared `doctor` types are in `doctor_core.py`, and `doctor.py` registers `BIGQUERY_CHECKS` explicitly after the setup checks. |
| System queries | `SystemQuery` skips the SQL Guard, so `tests/test_no_direct_execute.py` fails if any file outside `src/data_access/` names it, as it does for `execute_query`. |
| Allow-list hook | `SchemaCatalog.with_policy(policy)` reconciles and attaches it. `overview()`, `prune()` and `visible_columns()` raise until a policy is attached, so nothing unclassified is ever shown. Plan 05's `Allowlist` needs `table_names()`, `allowed(table)`, `blocked(table)` and `describe(table, column)`, and plan 07 or the CLI must call `with_policy` at startup. |
| Budget hook | `SafeQueryRunner` needs from plan 04's `TurnBudget`: `bytes_remaining`, `charge_bytes(n)`, `check_time()` and `remaining_seconds()`. Only `bytes_remaining` is missing from plan 04's interface list. `budget=None` means "outside a turn". |
| Session | `ChatSession.catalog` holds the loaded catalog for plan 07. |
| Tests | `tests/fake_bigquery.py` is the scripted client (rules, error lists, a `down` switch). An autouse fixture installs it for every test, so no test reaches BigQuery. `pytest.ini` deselects `-m live` by default. |

### Known gaps
- Startup and `doctor` queries run outside a turn, so they are not in `runs.jsonl` and not in the month total shown by `metrics`. They scan a few MB.
- The error patterns follow BigQuery's documented messages and the fake. The live tests check the real ones with free dry runs.

### Still to do with a Google sign-in
1. `gcloud auth application-default login`, set `GOOGLE_CLOUD_PROJECT` in `.env`.
2. `python -m src.cli.main doctor --refresh`. Read the dataset, byte cap and text-column lines. Copy the real columns into plan 05's allow-list (Q4) and note the latest date.
3. Put real brand names from the "Top brands" hint into `users.yaml`. Run `doctor` again until Brand mapping passes.
4. Set `bigquery.max_bytes_billed` from the printed table sizes (about 5 times the largest) and write the reason in the config comment (NFR6).
5. Write the A23 count and the A21 text-column review in `docs/decision.md`.
6. `pytest -m live -s`. Mark Status as done when it passes.

## Tests and acceptance
Unit tests use a fake `bigquery.Client`. One small live test (marked `live`) runs against the real dataset.
- Dry run over the cap raises `QUERY_TOO_EXPENSIVE` and the query job is never started.
- Timeout cancels the job and raises `QUERY_TIMEOUT`.
- Each row of the error table maps to the right kind. A canary email inside an error message is masked in the `AgentError`, the span and the log.
- Rows above the row cap are not fetched; `total_rows` is still correct.
- Labels use the user hash and are valid BigQuery labels.
- Brand check: exact match passes; `levis` fails with a suggestion; BigQuery down with a cache passes with a warning; BigQuery down with no cache stops startup.
- `test_no_direct_execute`: the old client has no `execute_query` or `get_table_schema`, `SafeQueryRunner` has no public client, and no file outside `data_access/` references `execute_query`, `SystemQuery`, `BigQueryRunner` or a BigQuery client import.
- Acceptance (live): a simple aggregate over `order_items` runs, reports bytes, and shows up in a trace and in `runs.jsonl`.

## Open items and defaults used
| Item | Default used | Who answers |
|---|---|---|
| NFR6, Q3 byte cap | About 5 times the largest table scan, in config (starting value 512 MiB per query, 2 GiB per turn) | Us |
| Query timeout | 30 s, in config | Us |
| Q4 real columns and latest date | Read live in the first task | Us |
| Brand check when BigQuery is down | Use the cache, else refuse | Us |

## Risks and pitfalls
- `job_timeout_ms` or other job options may differ by library version. Check on the installed version and fall back to `result(timeout=...)` plus `job.cancel()`.
- Parameterized queries (`@brands`) need `query_parameters` on both the dry run and the real run. Forgetting one makes the dry run fail.
- The cost cap protects the 1 TB budget only if the dry run is truthful. Query cache hits bill zero bytes; record them as such.

## Hand-off
- Plan 05 reads the live schema and writes the allow-list that this plan reconciles.
- Plan 07 calls `SafeQueryRunner.run` inside the same node that calls the Result Guard, so raw rows never reach graph state.
- Plan 09 uses the fake client and the live marker.
