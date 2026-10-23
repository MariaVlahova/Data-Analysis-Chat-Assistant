# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

A CLI chat agent for non-technical retail executives. It answers sales questions on BigQuery (`bigquery-public-data.thelook_ecommerce`: `orders`, `order_items`, `products`, `users`), explains causes and writes reports with action items. Each user is a brand manager and sees only their brands. The CEO sees all brands. PII never appears in an answer.

It is a 6 to 12 hour take-home prototype for the client OpsFleet. The assessment weighs system design, the technical explanation and an elegant prototype.

## Current state

**Plan 01 (foundation) is built:** config, `users.yaml` brand scope, typed errors, SQLite storage and the CLI shell (`chat` with a stub turn that only echoes, and `doctor`). **Plan 02 (observability) is built:** `src/pii.py`, `src/observability/` (tracer, spans, metrics row, viewer, alert), masked logs, `runs.jsonl`, `/trace` and `/metrics`. Every question already runs inside a trace. **Plan 03 (BigQuery access) is built and tested offline:** `src/data_access/` has `SafeQueryRunner` (dry run, byte cap, timeout, labels, row cap, retries), `errors_map.py`, `SchemaCatalog` with its 24-hour cache, and the startup brand check. Its live steps are not done (real columns, real brands in `users.yaml`, byte cap from table sizes, live test run): they need a Google sign-in, see the plan's "Still to do". Until real brands are in `users.yaml` the chat refuses to start. The agent behind the CLI is not built: plans 04 to 11 are described in `docs/plans/`. `src/data_access/bq_client.py` is the old client, now only used inside `data_access/`. Check the plan's `Status:` line and the file tree before assuming something exists.

## Where to look

| File | Use it for |
|---|---|
| `docs/decision.md` | Decision record. Source of truth for design choices, risks (S1 to S15), assumptions (A1 to A24), NFRs and open questions (Q1 to Q43). |
| `docs/PRD.md` | Requirement IDs (F1 to F54, NFR1 to NFR29), deliverables D1 to D6, success metrics. |
| `docs/plans/NN-*/plan.md` | The 11 build plans: scope, interfaces, file layout, config keys, tasks, tests. Build in number order. Plan 10 is optional. |
| `docs/requirements/` | The original brief and the client Q&A. |
| `../requirements/` (outside this repo) | Source notes behind decision.md: `Phase1-decisions.md`, `README-evaluation.md`, the Result Guard note, and the step-input PNGs. |

## Commands

Commands marked "works" run today (plans 01 to 03). The others are planned (plans 04, 09 and 10). Update this section as they land.

```bash
pip install -r requirements.txt                        # works
python -m src.cli.main doctor [--refresh]              # works: Python, packages, config, users, keys, credentials, database, and with credentials the BigQuery dataset, byte cap, brand mapping, items without a brand, text columns. --refresh reads the schema and brands again
python -m src.cli.main chat --user alice [--chaos mild|harsh] [--debug-prompts]   # works, but a turn only echoes until plan 07; --chaos does nothing until plan 04; --debug-prompts stores masked prompt text in traces
python -m src.cli.main trace [ID] [--raw]              # works: the steps of one turn (operator view, any user). In the chat: /trace [id] [--raw], your own turns only (CEO: all)
python -m src.cli.main metrics [--last N]              # works: totals from runs.jsonl (operator view, all users) and the month's BigQuery use against the free tier. In the chat: /metrics, your own turns only (CEO: all)
pytest                                                 # works: offline, no network or keys
pytest -m live                                         # real BigQuery (plan 03, free dry runs plus 3 small queries) and later Gemini; manual, skips without credentials
python -m eval.report                                  # PRD section 9 metrics table
```

Needs a Google Cloud project with BigQuery access (`gcloud auth application-default login`) and a `GEMINI_API_KEY` in `.env`. The local venv is Python 3.14.6.

## Architecture in one screen

A LangGraph state graph with fixed steps and two bounded loops (SQL repair, replan):
`Input Guard` → `Context Assembly` → `Planner` (why and report turns only) → `SQL loop` (SQL Guard → BigQuery dry-run → execute → Result Guard) → `Evidence Check` → `Composer` → numeric check → `Output Guard`.
Delete and save requests skip the SQL path: `Input Guard` → `Tool Router` → `HITL Gate` (LangGraph `interrupt`) → tool.
Gemini calls go through one gateway (rate limiter, retry with jitter, circuit breaker, Flash to Flash-Lite fallback, per-turn call budget).

Planned layout: `src/{access,cli,storage,data_access,observability,llm,guards,agent,tools,reports,golden}`, plus `config.yaml`, `users.yaml`, `schema_allowlist.yaml`, `glossary.yaml`, `golden/seed/`, `eval/`, `data/` (runtime, git-ignored). Plan 01 has the full tree.

Prototype scope: Safety and PII, High-Stakes Oversight (delete saved reports), Resilience, Observability. **Design-only:** Golden Bucket retrieval (an optional seeded slice exists), system learning loop, quality assurance pipeline. Never describe a design-only item as working. **Out of scope (owner decision, 2026-10-22):** the memory agent and any learned user profile; persona management (editor UI, registry, versions, approval, rollback, per-user style, persona switching). Do not plan, design or build them. The persona is one static file, `personas/default.yaml`.

## Rules for working here

- **Code enforces security, the model only suggests.** Every model output is checked by code. A guard violation is never retried.
- **Real customer IDs, report IDs and raw result rows never reach the model, graph state, traces or logs.** The Result Guard replaces `users.id` with a fixed fake ID per session. Raw DataFrames stay local to the node that runs the query. Keep the pseudonymizer and API clients out of graph state and LangGraph config.
- **All agent queries go through `SafeQueryRunner`** (plan 03), after the SQL Guard. The old `BigQueryRunner.execute_query` no longer exists, and `SafeQueryRunner` keeps its client private; `tests/test_no_direct_execute.py` fails if a file outside `data_access/` mentions `execute_query` or imports a BigQuery client. Our own fixed SQL uses `SystemQuery`, never model-written SQL; the same test keeps the name inside `data_access/`. `run()` refuses any query type that is not in `safe_runner.ACCEPTED_QUERY_TYPES` (plan 05 adds `GuardedQuery`), so a look-alike object with `.sql` and `.params` cannot run.
- **Mask before writing.** Spans, logs and metrics go through `src/pii.py`. Identity in traces, metrics, audit rows and BigQuery labels is a user hash, not the login name.
- **Emit spans and counters through `src.observability`,** with the span names in plan 02. A model call or a query is both a span and a `turn_metrics()` call. Log through `logging.getLogger(__name__)` under `src`, never the root logger, or the line skips the masking filter. Never log or store the text of a caught exception (a BigQuery error can quote SQL literals that no pattern masks): use `crash_summary(exc)`. Never put a DataFrame or rows in a span attribute. An `AgentError`'s own `detail` is the one exception text allowed: our code wrote it, it is short and it is masked when the error is built. One deliberate case: `SQL_SYNTAX` and `SQL_UNKNOWN_COLUMN` details carry BigQuery's normalized message with quoted tokens blanked, because the repair step needs the position and the unknown name (`errors_map.py`).
- **Empty brand list means no access, never "all brands".** The CEO has an explicit all-brands flag.
- **Only the user's typed reply confirms a destructive action.** Never text from a model, a question or a stored report.
- **Do not guess brand or column names.** Read them from the live schema and brand list (plan 03).
- **Be sparing with live Gemini calls.** The free tier is rate-limited. Use the scripted fake gateway and fake BigQuery runner in tests.

## Working with the docs

- Status tags in `decision.md` and the PRD are [DECIDED], [PROPOSED], [OPEN], [NOT SPECIFIED], [CONFLICT]. Do not promote a tag without a source or an owner decision. Mark our own implementation choices `(impl)`.
- Keep each plan's `Goal:` line. Update its `Status:` line when the work changes. Update the Commands section above when a command starts working.
- Style: short sentences, tables, plain words, the same defined terms as the glossary in `decision.md`.
- Never write an example run, an experience statement or a result by hand. Copy real output from a real run. The author writes the LangGraph experience statement (D6).
- Do not commit `.env`, `data/`, `venv/` or any key. Ask before pushing or making the repository public.
