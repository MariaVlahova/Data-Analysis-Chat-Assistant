# Plan 03: BigQuery access
Status: not started
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
    tables: dict[str, list[ColumnInfo]]           # live schema, from get_table_schema
    def prune(self, tables: list[str]) -> str     # compact slice for the SQL loop
    def overview(self) -> str                     # high-level list for the Planner and for schema questions
    brands: frozenset[str]; latest_date: date; table_bytes: dict[str, int]

def check_brand_mapping(users, catalog) -> None   # raises ConfigError with suggestions
```
`GuardedQuery` is defined in plan 05 (`sql`, `params`, tags). Until plan 05 exists, tests use a tiny fake with the same fields.

## Design
### Wrapping `bq_client.py` (S2)
- Keep `BigQueryRunner` as the only place that builds a `bigquery.Client`. `SafeQueryRunner` reuses `runner.client` and builds its own `QueryJobConfig`, because `execute_query(sql)` cannot carry a config.
- Add a docstring warning to `execute_query` and a test that no module under `src/` outside `data_access/` calls it. All agent queries go through `SafeQueryRunner`.
- Edit `bq_client.py` only to mask its log lines with `pii.mask()` and to cut error text to 300 characters (S6). Do not change its public methods.

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
- At startup, call `get_table_schema` for the four tables (metadata calls are free) and cache the result in `data/cache/schema.json` with the check time. Refresh when older than 24 hours or on `doctor --refresh`.
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
- [ ] Run `bq show --schema` equivalents through `get_table_schema` for the four tables. Save the real columns. This closes Q4 and feeds plan 05's allow-list.
- [ ] Mask logging in `bq_client.py`.
- [ ] `errors_map.py`: classify exceptions, normalize and cut messages.
- [ ] `safe_runner.py`: dry run, cost gate, execute, timeout, cancel, labels, row cap, transient retries, spans, budget accounting.
- [ ] `catalog.py`: live schema, cache, prune, overview, freshness, table sizes.
- [ ] `brand_check.py` and the startup call in the CLI (`chat` and `doctor`).
- [ ] Choose real brands for `users.yaml` from the top brands by item count.
- [ ] Set the byte cap from the table sizes. Write the number in config.
- [ ] Extend `doctor`.

## Tests and acceptance
Unit tests use a fake `bigquery.Client`. One small live test (marked `live`) runs against the real dataset.
- Dry run over the cap raises `QUERY_TOO_EXPENSIVE` and the query job is never started.
- Timeout cancels the job and raises `QUERY_TIMEOUT`.
- Each row of the error table maps to the right kind. A canary email inside an error message is masked in the `AgentError`, the span and the log.
- Rows above the row cap are not fetched; `total_rows` is still correct.
- Labels use the user hash and are valid BigQuery labels.
- Brand check: exact match passes; `levis` fails with a suggestion; BigQuery down with a cache passes with a warning; BigQuery down with no cache stops startup.
- `test_no_direct_execute`: no file outside `data_access/` references `execute_query`.
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
