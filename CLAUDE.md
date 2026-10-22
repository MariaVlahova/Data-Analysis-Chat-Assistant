# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

A CLI chat agent for non-technical retail executives. It answers sales questions on BigQuery (`bigquery-public-data.thelook_ecommerce`: `orders`, `order_items`, `products`, `users`), explains causes and writes reports with action items. Each user is a brand manager and sees only their brands. The CEO sees all brands. PII never appears in an answer.

It is a 6 to 12 hour take-home prototype for the client OpsFleet. The assessment weighs system design, the technical explanation and an elegant prototype.

## Current state

**Plan 01 (foundation) is built:** config, `users.yaml` brand scope, typed errors, SQLite storage and the CLI shell (`chat` with a stub turn that only echoes, and `doctor`). The agent behind it is not: plans 02 to 11 are described in `docs/plans/` and not built yet. `src/data_access/bq_client.py` is the older BigQuery client that plan 03 replaces. Check the plan's `Status:` line and the file tree before assuming something exists.

## Where to look

| File | Use it for |
|---|---|
| `docs/decision.md` | Decision record. Source of truth for design choices, risks (S1 to S15), assumptions (A1 to A24), NFRs and open questions (Q1 to Q43). |
| `docs/PRD.md` | Requirement IDs (F1 to F54, NFR1 to NFR29), deliverables D1 to D6, success metrics. |
| `docs/plans/NN-*/plan.md` | The 11 build plans: scope, interfaces, file layout, config keys, tasks, tests. Build in number order. Plan 10 is optional. |
| `docs/requirements/` | The original brief and the client Q&A. |
| `../requirements/` (outside this repo) | Source notes behind decision.md: `Phase1-decisions.md`, `README-evaluation.md`, the Result Guard note, and the step-input PNGs. |

## Commands

Commands marked "works" run today (plan 01). The others are planned (plans 03, 04, 09 and 10). Update this section as they land.

```bash
pip install -r requirements.txt                        # works
python -m src.cli.main doctor                          # works: Python, packages, config, users, keys, credentials, database (brand list check: plan 03)
python -m src.cli.main chat --user alice [--chaos mild|harsh]   # works, but a turn only echoes until plan 07; --chaos does nothing until plan 04
pytest                                                 # works: offline, no network or keys
pytest -m live                                         # real BigQuery and Gemini, manual, rate-limited
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
- **All agent queries go through `SafeQueryRunner`** (plan 03), after the SQL Guard. Never call `BigQueryRunner.execute_query` from agent code.
- **Mask before writing.** Spans, logs and metrics go through `src/pii.py`. Identity in traces, metrics, audit rows and BigQuery labels is a user hash, not the login name.
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
