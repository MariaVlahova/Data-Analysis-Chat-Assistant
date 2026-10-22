# Plan 01: Foundation
Status: not started
Estimate: about 1 h (our estimate) · Depends on: none · Blocks: every other plan

**Goal:** Set up the Python project (with a requirements.txt), config, the users.yaml brand mapping (with a proposed, not-yet-client-confirmed explicit CEO all-brands flag and empty-means-no-access), the Typer and Rich CLI shell, SQLite storage, optional persona/style stand-in files if time allows, and setup instructions so the prototype runs on another machine.

Conventions for all plans: status tags are the ones in decision.md ([DECIDED], [PROPOSED], [OPEN], [NOT SPECIFIED], [CONFLICT]). `(impl)` marks an implementation choice that decision.md does not make. References like 3.4, S14, Q11 point to decision.md. F and NFR IDs point to PRD.md.

## Requirements covered
| ID | Requirement | Source |
|---|---|---|
| F1 | The CLI starts a chat for a named user (`--user alice`) and keeps the conversation history | PRD 6.1 |
| F10 | Each user sees only their brands. The CEO has an explicit all-brands flag. An empty brand list means no access. | decision 3.4 [DECIDED]; S14 fix [PROPOSED] |
| D3 | Setup instructions, `requirements.txt`, example run (this plan writes the setup part; plan 11 finishes the README) | PRD 8 |
| NFR19 | The interface never crashes: friendly message and trace ID | [DECIDED] |
| NFR28 | Runs on another machine: setup instructions, Docker optional | [DECIDED] |
| NFR29 | Each user sees only their brands. The CEO sees all. | [DECIDED] |
| F54 (stand-in) | Persona and "My style" files, only if time allows. Not required for any P0 item. | decision §8 |

## Decisions that apply
- Prototype stand-ins (§8): Typer and Rich CLI, `--user alice` from `users.yaml`, SQLite for app tables, `personas/*.yaml` with `/persona` and `/style` commands.
- 3.4 [DECIDED]: brand managers see only their brands. The CEO sees all brands.
- S14 [PROPOSED]: code must never read an empty brand list as "all brands". The CEO gets an explicit flag.
- 3.9 [DECIDED]: three prompt layers (locked rules, company persona, "My style"). A persona change touches only the Composer.
- A4: prototype users and brands come from `users.yaml`. In production the frontend sends a signed token with the brands (Q39 default: the token is the only source).
- A12 every role gets "My style" without approval. A16 English only. A17 USD and UTC. A18 the reviewer has a Google Cloud project and a Gemini key. A19 one user at a time on one machine. A24 brand lists are small and change rarely.
- Q14 default: the reviewer needs a Google Cloud project, a Gemini key and `pip install`.

## Scope
- In: project skeleton, config, user and brand mapping, typed errors, SQLite storage and migrations, CLI shell and slash commands, `doctor` command, setup instructions, optional persona stand-in.
- Out: any BigQuery call (plan 03), any Gemini call (plan 04), the chat loop that runs the graph (plan 07), tracing and metrics (plan 02, but the CLI shell leaves the hooks).
- Out (design-only): JWT parsing, Postgres access-rules table, Persona Admin UI, Memory Store.

## Layout this plan creates
Later plans add files under the same tree. Runtime files go in `data/`, which is git-ignored.

```text
requirements.txt  pytest.ini  .env.example  config.yaml  users.yaml
personas/default.yaml  personas/ceo-weekly.yaml      (optional stand-in)
src/__init__.py  config.py  errors.py  personas.py (optional)
src/yaml_utils.py           (safe YAML load that reports file, line and key on error)
src/access/users.py
src/storage/db.py  src/storage/migrations/001_conversations.sql
src/cli/main.py  src/cli/commands.py  src/cli/render.py
tests/test_config.py  test_users.py  test_storage.py  test_cli.py  test_errors.py
data/                       (runtime: db.sqlite3, runs.jsonl, traces/, logs/, cache/)
```

## Design
### Python and dependencies
- Target Python 3.11 or newer (`requires-python`). The local venv is 3.14.6. **Before building on any pin, install `requirements.txt` into a fresh venv on 3.14.** If a package has no wheel, fall back to 3.12 and say so in the README. (impl)
- Already installed in `venv/`: google-cloud-bigquery 3.46.2, pandas, db-dtypes, pyarrow, typer, rich, PyYAML, python-dotenv, pytest. Not installed yet: `langgraph`, `langgraph-checkpoint-sqlite`, `sqlglot`, `google-genai`, `pydantic`.
- `requirements.txt` uses compatible ranges (`>=`) at first. After plan 09 passes, freeze the working versions into the file so the other machine gets the same ones.
- One file only. pytest is included so a reviewer can run the tests with one install.

### Config (`config.yaml` → `src/config.py`)
- Loaded into pydantic models with `extra="forbid"`, so a typo in a key stops startup. Path from `--config`, else `OPSFLEET_CONFIG`, else `./config.yaml`.
- Secrets never live in YAML. `GEMINI_API_KEY`, `GOOGLE_CLOUD_PROJECT` and `GOOGLE_APPLICATION_CREDENTIALS` come from the environment or `.env` (loaded with python-dotenv, without overriding real env vars). Never log them. (Secret store is [NOT SPECIFIED]; Q16 default is a secret store in production.)
- Validation at load: `loop.max_replans` is 0 to 2 (Q22 ceiling), `guards.k_anonymity` ≥ 2, `guards.rows_to_model` ≥ 1, all budgets positive, `retention.*` ≥ 1.

Default keys. The last column names the plan that reads the key.

| Key | Default | Tag | Used by |
|---|---|---|---|
| `app.language / currency / timezone` | `en` / `USD` / `UTC` | A16, A17 | 07 |
| `app.data_dir` | `data` | (impl) | all |
| `bigquery.project_id` | `null` (default credentials) | | 03 |
| `bigquery.dataset` | `bigquery-public-data.thelook_ecommerce` | [DECIDED] | 03, 05 |
| `bigquery.max_bytes_billed` | 536870912 (512 MiB per query) | [OPEN] NFR6, Q3 | 03 |
| `bigquery.query_timeout_s` | 30 | [OPEN] Q3 | 03 |
| `bigquery.sql_row_limit` | 1000 (the forced `LIMIT`) | (impl) | 03, 05 |
| `bigquery.transient_retries` | 2 | (impl) | 03 |
| `guards.k_anonymity` | 10 | [PROPOSED] NFR13 | 06 |
| `guards.rows_to_model` | 50 | [PROPOSED] NFR14 | 06 |
| `guards.allow_ranked_customer_lists` | `true` | S7 rule still open | 05, 06 |
| `guards.max_input_chars` | 2000 | (impl) | 07 |
| `loop.max_subquestions` | 4 | [PROPOSED] | 07 |
| `loop.sql_repairs` | 2 | [PROPOSED] NFR5 | 07 |
| `loop.broaden_attempts` | 1 | [PROPOSED] 3.11 | 07 |
| `loop.max_replans` | 0 (production default 1, ceiling 2) | [PROPOSED] 3.10, Q22 | 07 |
| `loop.parallel_subqueries` | 1 | S12 | 07 |
| `composer.numeric_retry` | 0 (remove the number and say so; 1 allows one re-ask) | Q29 default | 07 |
| `budget.simple` | calls 8, seconds 60 | [OPEN] NFR4 | 04 |
| `budget.why` / `budget.report` | calls 20 / seconds 60 and calls 20 / seconds 90 (derivation in plan 04) | [OPEN] S3, NFR4 | 04 |
| `budget.bytes_per_turn` | 2147483648 (2 GiB) | [OPEN] Q3 | 03, 04 |
| `llm.*` (models, rate limits, retry, breaker) | see plan 04 | | 04 |
| `delete.large_batch_threshold` | 10 | (impl) | 08 |
| `delete.undo_days` | 30 | [PROPOSED] NFR17 | 08 |
| `retention.audit_days / checkpoint_days / trace_days` | 365 / 90 / 90 | [PROPOSED] NFR21; trace days [NOT SPECIFIED], Q25 default | 02, 08 |
| `observability.metrics_file / traces_dir` | `data/runs.jsonl` / `data/traces` | | 02 |
| `golden.enabled / top_k / min_similarity` | `false` / 3 / 0.25 | | 10 |
| `persona.enabled / default` | `false` / `default` | | 01 |

### Users and brand scope (`users.yaml` → `src/access/users.py`)
```yaml
users:
  alice: {name: Alice, role: brand_manager, brands: ["<verified brand>", "<verified brand>"]}
  bob:   {name: Bob,   role: brand_manager, brands: ["<verified brand>"]}
  ceo:   {name: CEO,   role: ceo, all_brands: true}
  nobrands: {name: No Brands, role: brand_manager, brands: []}   # fixture for S14
```
- `UserContext` is a frozen dataclass: `user_id, display_name, role, brands: tuple[str, ...], all_brands: bool, style: dict, user_hash`.
- `user_hash` is `sha256(salt + user_id)[:12]`. Traces, metrics, audit rows and BigQuery labels use the hash, never the login name. The salt comes from config or a generated file in `data/`. (impl; decision §6 says the audit log holds a "user hash".)
- `scope_mode` is `all` only when `all_brands` is true and role is `ceo`. Otherwise `brands` when the list is non-empty. Otherwise `none`.
- **S14 rule:** `none` means no access. The CLI refuses to start a chat for that user ("no brands are assigned to this user"). The graph also checks it again at the Policy Context step, so a bad object cannot slip through. A missing `users.yaml` or an unreadable one is `ACCESS_STORE_DOWN`: block (NFR15).
- Brand strings are compared exactly, case and spacing included (`products.brand` is free text, S1). Plan 03 checks them against the real brand list at startup.
- Choose the real brands in plan 03, from the top brands by item count, so each manager has meaningful volume. Do not guess brand names here. Tests use temporary YAML fixtures, never the real file.
- Roles beyond `brand_manager` and `ceo` (analyst, persona_editor, admin) are accepted by the schema but unused in the prototype (Q42 default: the four Phase1 roles plus CEO).
- Optional `style:` block per user (length, depth, charts, action_items, format). Fixed keys only (3.7 schema).

### Typed errors (`src/errors.py`)
One module, used by every plan, so errors travel through the graph state as data (3.11) and the CLI can always show a friendly message.

```python
class ErrorKind(str, Enum): ...
class AgentError(Exception):
    kind: ErrorKind; user_message: str; retryable: bool; repairable: bool
    detail: str          # masked, short, for traces only
    trace_id: str | None
def friendly(err, trace_id) -> str   # user_message + "Trace ID: ab12cd34"
class ConfigError(Exception): ...    # startup problems only (bad config, unknown brand). Not an AgentError.
```
`ConfigError` is raised before a chat starts. The CLI turns it into one clear sentence and exit code 2. `AgentError` is for problems inside a turn.

| Kind | Raised by | Handling (decision 3.11) |
|---|---|---|
| `INPUT_REJECTED` | 07 | Empty or too long input. Ask again. |
| `OUT_OF_SCOPE` | 07 | Polite refusal. Not a failure (F7). |
| `INPUT_GUARD_FAILED` | 07 | Block the request (S10, Q13 default). |
| `NO_ACCESS`, `ACCESS_STORE_DOWN` | 01, 07 | Block the request (S14, NFR15). |
| `GUARD_VIOLATION` | 05 | No retry. Explain the refusal (F32). Carries a rule ID. |
| `RESULT_GUARD_FAILED`, `OUTPUT_BLOCKED` | 06 | Fail closed. Nothing reaches the model or the user. |
| `SQL_SYNTAX`, `SQL_UNKNOWN_COLUMN` | 03 | Repair up to 2 times with the exact column list (F30). |
| `EMPTY_RESULT` | 07 | One check-and-broaden query, then an honest "no data" (F31). |
| `QUERY_TOO_EXPENSIVE`, `QUERY_TIMEOUT` | 03 | Ask the user to narrow the question (F33). |
| `REPEATED_SQL`, `REPEATED_ERROR` | 07 | Stop the loop (F34). |
| `BQ_UNAVAILABLE` | 03 | Transient retries done. Honest message. |
| `LLM_RATE_LIMITED`, `LLM_UNAVAILABLE`, `LLM_BAD_OUTPUT`, `LLM_BLOCKED`, `CIRCUIT_OPEN` | 04 | Retry, fall back, then honest message (F35). |
| `BUDGET_EXHAUSTED`, `TURN_TIMEOUT` | 04 | Stop. Say what was done so far (F34). |
| `NUMERIC_UNGROUNDED` | 07 | Remove the number and say so (S11, Q29 default). |
| `STALE_CONFIRMATION` | 08 | The ID set changed. Preview again (F24). |
| `TOOL_FAILED` | 07, 08 | Honest message with trace ID. Failed-job queue is design-only. |
| `INTERNAL` | any | Catch-all at the CLI. Friendly message, masked traceback in the trace (F36). |

### Storage (`src/storage/db.py`)
- SQLite file `data/db.sqlite3` (already git-ignored by name). WAL mode, `foreign_keys=ON`, `row_factory=sqlite3.Row`. A `transaction()` context manager commits or rolls back.
- Numbered migrations in `src/storage/migrations/*.sql`, applied in order and recorded in `schema_migrations`. Plan 01 ships `001_conversations.sql` (`conversations`: id, user_id, created_at, last_active_at). Plan 08 adds `002_saved_reports_audit.sql`. Plan 08 also lets LangGraph's `SqliteSaver` create its own checkpoint tables in the same file.
- Conversation IDs look like `conv-` plus 8 hex characters. Turn IDs are `t-<n>` inside a conversation. Plan 08 needs the conversation ID in saved reports (S8).
- A19: one user at a time, so no locking work beyond WAL.

### CLI shell (`src/cli/main.py`)
- Typer commands: `chat --user NAME [--chaos mild|harsh] [--chaos-seed N] [--conversation ID] [--config PATH] [--debug]`, `doctor`. Plans 02, 10 add `metrics`, `trace`, `golden validate`.
- Run as `python -m src.cli.main chat --user alice`. No install step beyond `pip install -r requirements.txt`. `pytest.ini` sets `pythonpath = .`.
- Slash commands inside the chat: `/help`, `/quit`, `/new` (new conversation), `/trace [id]` and `/metrics` (plan 02), and `/persona`, `/style` (optional). Anything else starting with `/` prints the help.
- Rich: a status spinner for progress, Markdown for answers, a Panel for refusals and errors. Output and progress separate: only progress text streams while a turn runs (S9). The answer prints once, after the Output Guard.
- **Top-level boundary (NFR19, F36):** the REPL wraps every turn in a catch-all. Any exception becomes `AgentError(INTERNAL)`, the CLI prints a friendly message and the trace ID, and the loop continues. Only `KeyboardInterrupt` and end-of-input quit, with a short goodbye. The masked traceback goes to the log file, never to the screen unless `--debug`.
- This plan builds the shell with a stub `run_turn()` that echoes. Plan 07 replaces it.
- Exit codes: 0 normal, 2 for a startup problem (bad config, unknown user, no access, brand check failed). Startup problems print one clear sentence and the fix.
- Logging: `setup_logging()` writes to `data/logs/app.log`. The console shows no log lines unless `--debug`. Plan 02 adds the PII masking filter to this logger.

### `doctor` command (supports NFR28, Q14)
Prints a pass/fail line per check and the fix for each failure:
- Python version, `pip` packages importable.
- `config.yaml`, `users.yaml` load and validate. Each non-CEO user has at least one brand.
- `GEMINI_API_KEY` is set (never print it).
- Google Cloud credentials found, and the project is set.
- Plan 03 adds: BigQuery reachable, the four tables readable, brand list check, schema versus allow-list check, A23 join check. Plan 04 adds: a one-token Gemini ping (counts against the quota, so only with `--live`).

### Persona and style stand-in (optional, only if time allows)
- Files: `personas/default.yaml`, `personas/ceo-weekly.yaml` with `id, version, status, tone, sections, max_words`. A pydantic model validates them (max 2000 characters of tone text).
- The loader re-reads the file by modification time on every turn, so editing a file changes the next answer with no restart. This shows "no redeploy" (NFR23, R8) in the cheapest way.
- Version string `id@vN+sha8` goes into the trace (F41).
- `/persona [name]` switches. `/style key=value` sets one of the fixed style keys (length, depth, charts, action_items, format) and stores it in an SQLite `user_style` table. This is the "explicit capture only" slice of the design-only memory (§8).
- The persona and style text goes only to the Composer prompt (3.9). Locked rules always win over persona text (plan 07).
- Skip all of this first if the time box gets tight.

## Tasks
- [ ] Create `requirements.txt`, `pytest.ini`, `.env.example`, `.gitignore` additions (`data/`, `.deepeval/`; `.env` and `venv/` are already ignored).
- [ ] Install into a fresh Python 3.14 venv. Record any package that fails and the fallback.
- [ ] `src/config.py` with the models, defaults and validation above. Write `config.yaml`.
- [ ] `src/errors.py` with `ErrorKind`, `AgentError`, the message table and `friendly()`.
- [ ] `src/access/users.py` and `users.yaml`. Cover the S14 cases.
- [ ] `src/storage/db.py`, the migration runner and `001_conversations.sql`.
- [ ] `src/cli/main.py`, `commands.py`, `render.py`: `chat` loop with stub turn, slash commands, catch-all boundary, `doctor`.
- [ ] README setup section: prerequisites, `pip install -r requirements.txt`, `gcloud auth application-default login`, `GEMINI_API_KEY`, `.env`, first run, `doctor`, running the tests.
- [ ] Optional: persona files, loader, `/persona`, `/style`.

## Tests and acceptance
- `test_config`: defaults load; unknown key fails; `max_replans: 3` fails; env overrides do not leak secrets into the model repr.
- `test_users`: CEO is `all`; manager with brands is `brands`; **empty list for a manager is `none` and never `all`**; a manager with `all_brands: true` but role not `ceo` is rejected; unknown user lists the known names; missing file is `ACCESS_STORE_DOWN`.
- `test_storage`: migrations apply once and are idempotent; a failed transaction rolls back.
- `test_errors`: every `ErrorKind` has a user message; `friendly()` always includes the trace ID.
- `test_cli`: `--help`; unknown user exits 2 with one clear sentence; `nobrands` exits 2; an exception inside a turn prints the friendly message and the loop continues; `/quit` exits 0.
- Acceptance: on a clean machine, the README steps reach a working `chat --user alice` prompt and `doctor` is green for the checks that exist so far.

## Open items and defaults used
| Item | Default used | Who answers |
|---|---|---|
| Q14 reviewer needs | Google Cloud project, Gemini key, `pip install` | Us |
| Q39 token vs access table | `users.yaml` only in the prototype | Us |
| Q42 other roles | Accepted by the schema, unused | OpsFleet client |
| S14 explicit CEO flag | Used. Not yet a client decision. | Our fix, needs client confirmation |
| NFR6, Q3 byte and time caps | Config values above, to be tuned in plan 03 | Us |

## Risks and pitfalls
- A new package may not install on Python 3.14. Catch it in the first task, not at the end.
- Unverified brand names in `users.yaml` give an empty scope. Plan 03's startup check is the guard (S1). Do not skip it.
- Printing a traceback to the executive breaks NFR19. Keep it in the log.

## Hand-off
- Plans 02 to 04 import `Settings`, `AgentError`, `UserContext` and `Database`.
- Plan 07 replaces the stub `run_turn()` and owns the chat loop.
- Plan 08 adds migration `002` and the checkpointer.
- Plan 11 finishes the README that this plan starts.
