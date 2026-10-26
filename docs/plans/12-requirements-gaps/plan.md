# Plan 12: Gaps found by the requirements check
Status: parts A to F built and tested offline (2026-10-26); part G (what needs a live run, the owner or the author) is not done, and only one turn of it has run on a model (the local model, sample data, 2026-10-26: the exchange file and the table looked right). See "As built" at the end for what differs from the text above. It comes from the requirements check of 2026-10-26 (commit `a64b84d`), which found R2a and R7c only partly met, assumption A23 contradicted by the real data, wrong key figures on a real model (T5, CAP0, CAP1, CAP3a), an audit log that is never purged (NFR21, A11) and settings that nothing reads (A16, A17). Parts A to F are code; part G lists what needs a live run, the owner or the author.
Estimate: about 6 h (our estimate) · Depends on: 05, 06, 07, 08, 09 · Blocks: nothing (without it, the documents keep listing these gaps)

**Goal:** Put code checks behind the rule that the agent answers only analysis questions (R2a), let an operator rebuild the full message exchange of a turn from its trace (R7c), label the order items that have no brand in the CEO's answers (A23), stop the two errors behind the wrong figures of the real-model run (summaries that leave figures out, and a revenue that counts returned items), keep the audit log for one year and no longer (NFR21), and make the language, currency and time zone settings mean what they say (A16, A17).

Tags and `(impl)` marks are explained in plan 01.

## Requirements covered
| ID | Requirement | Source |
|---|---|---|
| R2a | "The agent is only allowed to answer analysis questions" | Brief, requirement 2 |
| NFR16 | The model is never the only checker | decision 4 |
| R7c | "how you would support deep dive analysis/debugging (understanding what the message correspondence is and what went wrong)" | Brief, requirement 7 |
| A23 | Every order item joins to `products.brand` (restated 2026-10-26: 139 of 181,777 items do not) | decision 5 |
| T5, CAP0, CAP1, CAP3a | A Q1 report with insights and action items; customer behaviour; time-based metrics | Brief, the task and the expected capabilities |
| NFR21, A11 | Audit log kept 1 year | decision 4 and 5 |
| A16, A17 | English only; USD and UTC | decision 5 |
| NFR2, NFR26, S13, D6c, Q8 | Not code: part G | decision 4, 9.2, 10; brief D6 |

## What the check found
- **R2a.** Only the Input Guard's classifier decides what counts as an analysis question (`src/agent/prompts/locked_rules.py:73`). Code refuses when the classifier says out of scope, but nothing in code stops an off-topic request the classifier lets through. The SQL Guard accepts a query that reads no table (`SELECT 1 AS one`, `tests/test_sql_guard.py:67`), and the numeric check only looks at numbers, so a draft with no number passes it. A chain of fooled model steps can therefore answer "write a poem". No test covers this: the compromised-model test of the Input Guard (`tests/test_compromised_model.py:37`) asks for personal data, which the SQL Guard stops.
- **R7c.** `--debug-prompts` stores each step's system text only (`PromptParts.attrs`, `src/agent/prompts/templates.py:36-38`). The contents sent in the turn and the model's replies are stored nowhere, so the exchange of a turn cannot be rebuilt. The rule that an answer the Output Guard held back is stored nowhere must stay.
- **A23.** `doctor` counts 139 of 181,777 order items (0.08%) on the real dataset whose product has no brand (technical-explanation.md, section 9). The scope views inner-join the products with the brand filter, so these items are hidden from every brand manager (fail closed) and only the CEO sees them. In a CEO answer by brand they appear as a group with an empty brand, and nothing says what it is.
- **Wrong figures on a real model.** The golden run on real BigQuery with the local model (2026-10-25, git-ignored `data/bigquery/eval/golden-20261025-161509`) failed three cases on their key figures, for three different reasons:
  - `rev-monthly-alice` (0 of 5 figures). The SQL grouped by month and brand (24 rows), and the answer gave a range ("from $850.96 to $3,864.28") instead of the months. "The monthly revenue of my brands" can mean one total per month or one per brand and month, so the case itself is ambiguous. Both readings were valid; the summary was the model's choice.
  - `report-q1-alice` (0 of 2 figures). The sub-question the Composer took revenue from excluded only `'Cancelled'`, because the same query also counted returns, so returned items were counted as revenue ($7,259 for Calvin Klein against $6,370.78). Other sub-questions of the same turn used the right filter. The definition drifted from one sub-question to the next, and nothing in code noticed.
  - `top-customers-alice` (1 of 5 figures). Not a wrong figure. The verdict lists the figures the answer did **not** state, so $274.62 is the first of the four it left out, not the reference's top. The answer was a summary: the top spender ($298.99), the lowest of the ten and their total, without the ten rows. Settled on real BigQuery as `alice` on 2026-10-26 (part G): the reference SQL, the model's SQL as trace `feb55005` shows it (a join to the `orders` scope view and a timestamp range) and each of the two changes alone return the same ten customers in the same order, and the join keeps all 1,588 items. The run kept no result rows, so the reference's five figures of that day cannot be listed. The cause is the form of the answer, which the code-built table of part D (made after the run) fixes: the case passed when it was run alone on 2026-10-26 (1 of 1, 84 s, answer with the ten rows; that one-case run's folder was not kept, only the full run of 2026-10-25 is). The live data also moved between the two days: alice's 2025 revenue was $48,378.70 in the answer of 2026-10-25 and the reference gives $47,216.70 now; the CEO's, $1,853,076.41 then and $1,791,899.71 now. A recorded figure of a live run is true for that day only.
  - Both recorded golden runs failed only on key figures. Technical-explanation.md, section 7.7, names four kinds; the first is summary answers that state fewer figures than the case asks for.
- **NFR21, A11.** Migration `002` adds a trigger that refuses every delete on `audit_log`, and nothing reads `retention.audit_days`. The audit log is kept forever, not for one year.
- **A16, A17.** `app.language`, `app.currency` and `app.timezone` are read into `AppConfig` and used by nothing. English, USD and UTC are fixed in the prompts (`locked_rules.py:29` and `:31`), so changing a setting changes nothing, silently. No test checks that the USD rule reaches a prompt.

## Decisions that apply
- NFR16 and decision 3.5: code checks every model output; a guard violation is never retried. `SQL_SYNTAX` and `SQL_UNKNOWN_COLUMN` may be repaired (decision 3.11).
- Plan 02: a span never holds a DataFrame, a row or a cell value; every string attribute is masked and cut to 2000 characters; an exception is stored as its class and call sites.
- Plan 06 and 07: an answer the Output Guard held back is never stored in a span, a log or a state field, not even masked. Text the Output Guard has not passed lives in `TurnRuntime.scratch`.
- Plan 07: every number in the answer must come from a result (the numeric check). A code-built answer from the guarded figures is the fallback.
- Plan 08: the chat keeps only the newest checkpoint of a conversation. The audit log is append-only, and an effect and its audit row are one transaction.
- Plan 09: the golden cases are not changed to suit a model. A changed case changes `cases_hash`, so its baselines must be made again.
- Glossary (`glossary.yaml`, [PROPOSED], Q6): "Revenue is the sum of item sale prices, without cancelled or returned items. Say what was counted when the question asks for something else."
- Client Q&A: each user analyzes only data tied to their brands; the CEO sees everything.

## Design

### Part A: three code checks behind the Input Guard (R2a)
A turn can end in an answer about data only if all three hold. None of them asks a model.

**A1. Domain words in the user's own text (Input Guard).** After the classifier says "in scope" with a data intent (`data_question`, `why_question`, `report_request`, `schema_question`), code looks for at least one domain word in what the user typed, not in the model's rewrite. The word list is built at startup from what the app already has, never typed per question:
- the table and column names of `schema_allowlist.yaml`, allowed and blocked, split on underscores (`email` is still a question about customers; the SQL Guard decides what may be read);
- the glossary terms and their aliases (`glossary.yaml`);
- the user's own brand names;
- the product categories and departments of the data, read once with the brand list and cached the same way (one more `SystemQuery`) (impl);
- a short fixed list in code: business words (revenue, sales, spend, order, customer, product, item, brand, return, churn, margin, profit, price, cost, discount, units, inventory, report, insight, performance, trend, growth, decline, top, rank, compare) and words about the data itself (data, table, column, field, available).

A follow-up such as "and in 2024?", "why?" or "what about last year?" has no domain word. It passes when the conversation's previous turn was an accepted data turn and every word of the message is a follow-up word: a stop word, a time word, a number or year, a comparison word, or a pronoun.

With no domain word, the turn ends with a question that code wrote, not a refusal: "I can answer questions about your store's orders, products, customers and sales. Which data should I look at?" The guard decision is `input` `block` with the rule `NO_DATA_WORDS`. A false refusal therefore costs the user one extra turn. Config: `guards.require_domain_words: true`.

**A2. Every query reads a table of the dataset (SQL Guard, step 4).** A query that reads none of the four tables (`SELECT 1 AS one`, a CTE made of literals) is refused with the new rule `NO_DATA_TABLE`. It is a guard violation, so it is never retried. Its text goes into `src/guards/refusals.py`.

**A3. A data answer uses the data (`numeric_check` node).** For a turn with at least one result of status `ok`, the checked draft must state at least one value of the guarded results: a figure the numeric check matched (`NumberCheck.matched`), or a text value of a result cell such as a brand, a category or a month. If it states none, the body is replaced by `fallback_body(state)` (the guarded figures as tables, written by code). The answer is marked degraded, the guard decision is `numeric` `block` with the rule `ANSWER_NOT_FROM_DATA`, and the model's draft is dropped and stored nowhere.

**What remains, and the documents must say it.** If every model step is compromised, an answer can still carry off-topic text next to real figures from a real, scoped query. Code cannot judge free text. The Output Guard still blocks personal data, and the brand scope still holds.

### Part B: the message exchange of a turn (R7c)
**B1. The exchange file.** With `--debug-prompts`, every model attempt adds one line to `data/traces/<trace_id>.exchange.jsonl`: step, model, attempt, the span ID of its `llm.call`, the system text, the contents sent, the reply, the tokens and the error kind. It does not go into the spans, because a span attribute is cut at 2000 characters and never holds rows (plan 02).
- **The contents sent.** `PromptParts` gets a second rendering of its contents, `debug_contents`. The same template code builds it, with each guarded result table replaced by one line that names it: `[rows of q1: 12 rows, columns brand, revenue, units; not stored]`. Rows are never written. The checkpoint keeps only the newest turn, so the file cannot point to them later either. To see them, run again the guarded SQL that the trace holds.
- **The reply.** The reply text of every step except the Composer. The Composer's draft follows the Output Guard rule: the file gets the answer only after `output_guard` passes it (the text the user saw), and nothing when the guard blocks it.
- Every message passes `pii.mask(..., extended=True)` before it is written and is cut at 20,000 characters (impl).
- Off by default. The startup purge of `retention.trace_days` removes the file with its trace.

**B2. The view.** `trace ID --exchange` (operator) and `/trace ID --exchange` in the chat (the user's own turns; the CEO sees all) print the attempts in order. Without the file, the view says to start the chat with `--debug-prompts`.

**B3. The conversation.** `trace --conversation ID` lists the turns of one conversation from `runs.jsonl` (trace ID, time, intent, outcome) and the masked question of each from its trace's root span. A reader goes from the conversation to a turn, and from the turn to its exchange.

### Part C: order items with no brand (A23)
**C1. Say what the empty brand is.** When a result for an all-brands user has a `brand` column with an empty or null value, the Result Guard writes `(no brand)` in that cell and adds one notice: "Some order items have no brand. Brand managers do not see them; they are counted here." A brand manager never gets such a row, because the scope views leave those items out.

**C2. No change to the scope.** An item with no brand belongs to no brand manager (fail closed), which follows the client's rule "data tied to those brands". If the client wants someone to own them, the design is an explicit flag beside `all_brands` (for example `unbranded: true`). It is not built; it waits for Q45.

### Part D: the figures of an answer (T5, CAP0, CAP1, CAP3a)
**D1. Code shows the figures of a simple turn.** For a simple turn with one result of status `ok`, the answer ends with that result as a table, built by code from the guarded rows (`render_table`, `src/agent/envelope.py`), after the body and before the notes. Up to `composer.result_table_rows` rows (default 20, impl), then one line with how many more. The table comes from guarded results, so the numeric check does not need to hold it; the Output Guard checks it with the rest of the answer. The Composer's text becomes the commentary, and a summary can no longer hide the figures the question asked for. Why and report turns keep their figures in the text: several results as tables would bury the answer (open item).

**D2. A revenue figure follows the revenue definition (`src/agent/definition_check.py`, new).** The check runs when the question uses "revenue" or one of its glossary aliases, or says "not cancelled or returned", and does not ask for something else ("including returns", "gross"). On the sqlglot tree of the model's SQL, after the SQL Guard passes and before the dry run, every plain `SUM(<alias>.sale_price)` (not inside a `CASE` or `IF`) must sit in a `SELECT` whose `WHERE` excludes both `'Cancelled'` and `'Returned'`. A sum inside a `CASE` (the value of the returned items, for example) is not revenue and is left alone.
- If the check fails, the new error kind `SQL_DEFINITION` sends the query back to the SQL repair step with the definition and the failing SQL. It is repairable, not a guard violation, and it counts against `loop.sql_repairs`.
- After the last repair, the sub-question fails with a gap line in the answer: a missing figure, not a wrong one (open item).
- CLAUDE.md's rule that only `SQL_SYNTAX` and `SQL_UNKNOWN_COLUMN` are repaired gains `SQL_DEFINITION`.

**D3. One metric per sub-question (Planner rule, a suggestion only).** The Planner's locked rules ask for one sub-question per metric when two metrics need different filters (revenue apart from returns). D2 still checks the result, because the rule is a prompt.

**D4. The ambiguous golden case.** `rev-monthly-alice` asks "the monthly revenue of my brands", which has two valid readings. The question is reworded to "the total monthly revenue of my brands together"; the reference SQL stays the same. This removes an ambiguity the case had; it does not suit the case to a model. The change is recorded in the case's `notes`, and it changes `cases_hash`, so both baselines of `eval/baseline.json` must be made again by a real run (part G). The alternative, accepting both readings, needs a second reference per case in `eval/metrics.py` (open item).

### Part E: the audit log is kept one year (NFR21, A11)
**E1. Migration `005`.** It replaces the `audit_no_delete` trigger with one that refuses a delete only for a row younger than 365 days: `BEFORE DELETE ON audit_log WHEN OLD.ts > <now minus 365 days>`, written in the format `AuditLog` uses for `ts`. `audit_no_update` stays as it is. Rows younger than a year stay append-only.

**E2. The startup purge.** Beside the checkpoint and trace purges (`_housekeeping` in `src/cli/main.py`), one more deletes the audit rows older than `retention.audit_days`. `src/config.py` validates `audit_days >= 365`, so the config cannot ask for less than the trigger allows. The purge adds one audit row (`audit_purged`, with the count and a fixed system user hash, impl) in the same transaction.

**E3. The report purge does not change.** `ReportStore.purge_expired` still never touches the audit log, and `tests/test_audit.py::test_the_startup_purge_does_not_touch_the_audit_log` stays as it is.

### Part F: the settings mean what they say (A16, A17)
**F1. Only the supported values load.** In `AppConfig`, `language` becomes `Literal["en"]`, `currency` `Literal["USD"]` and `timezone` `Literal["UTC"]`. Another value stops the app at start with a config error that names the key and says what is supported. The prompts keep their fixed English, USD and UTC rules.

**F2. The rules reach the prompts.** A test checks that the money rule (USD) and the time rule (UTC) reach the prompts of the steps that need them (the Composer and the SQL writer).

### Part G: what code cannot do
| Item | Who | What |
|---|---|---|
| CAP1: the top-customers figures differ though the SQL looks the same | Google sign-in | **Done 2026-10-26.** The queries return the same rows (see "What the check found"); the failure was a summary that stated one figure of five. |
| T5, CAP0, CAP3a, NFR26: is it the model? | Gemini key and Google sign-in | `eval.run_golden --update-baseline --config <Gemini on BigQuery>` after parts D1 to D4; it also gives the Gemini baseline NFR26 lacks |
| NFR26: the tolerance of one case | The owner | Confirm or change it; then make the local baselines again, because D4 changed a case |
| NFR2: why and report turns took 311 s (median) on the local model | Gemini key | Measure on Gemini first. If still over 60 s, run sub-questions in parallel through `parallel_limit` (built and tested, never called, S12): a plan of its own |
| S13: the judge's scores can shift | The owner | Accept it as a risk in decision.md: the ID is pinned, the judge is advisory and never gates, and recalibrating needs human labels (design-only). With Pro off the judge is Flash, not the preview Pro model |
| D6c, Q8: the experience statement | The author | Done 2026-10-26: the statement names the LangGraph projects of the author's CV. The author reads it once more before submission |

### Not in this plan
- A judge model chosen apart from `llm.pro_enabled` (an `evaluation.judge_model` key). Today a Gemini setup with Pro off judges with Flash, the model that answered (DM-3.13).
- Running anything on Gemini or with `pytest -m live` (part G).
- Detecting a question that is not in English (A16 stays an assumption; F1 only stops the setting from pretending otherwise).

## Files
| File | Change |
|---|---|
| `src/guards/domain_words.py` (new) | A1: the word list and the check. Pure functions; no model, no BigQuery library. |
| `src/agent/nodes/input_guard.py`, `src/agent/nodes/failure.py` | A1: the check after the classifier, and the question text |
| `src/data_access/catalog.py` | A1: categories and departments, cached with the brands |
| `src/guards/sql_guard.py`, `src/guards/refusals.py` | A2: `NO_DATA_TABLE` |
| `src/agent/nodes/composer.py` | A3: `ANSWER_NOT_FROM_DATA` |
| `src/agent/prompts/templates.py` | B1: `debug_contents` |
| `src/llm/gateway.py`, `src/observability/tracing.py` | B1: one exchange line per attempt; the purge |
| `src/observability/viewer.py`, `src/cli/main.py`, `src/cli/commands.py` | B2, B3: the views |
| `src/guards/result_guard.py` | C1 |
| `src/agent/envelope.py`, `src/agent/nodes/composer.py` | D1: the code-built table of a simple turn |
| `src/agent/definition_check.py` (new), `src/agent/nodes/sql_loop.py`, `src/errors.py` | D2: `SQL_DEFINITION` |
| `src/agent/prompts/locked_rules.py` | D3: the Planner rule |
| `eval/datasets/golden_cases.yaml` | D4: `rev-monthly-alice` reworded, with a note |
| `src/storage/migrations/005_audit_retention.sql` (new), `src/storage/audit.py`, `src/cli/main.py` | E1, E2 |
| `src/config.py`, `config.yaml` | `guards.require_domain_words`, `composer.result_table_rows`, `retention.audit_days >= 365`, F1 |
| `eval/datasets/attacks.yaml` | Two attacks with a hijacked classifier (an off-topic request with and without a domain word) |

## Tasks
1. Write the word list and run it over every question in the repository before wiring it in (the first test below). Fix the list until no question there is missed.
2. Wire A1 into the Input Guard behind `guards.require_domain_words`, with the question text.
3. Add A2 to the SQL Guard. Change the two rows of the test table that accept a query with no table (`tests/test_sql_guard.py:65` and `:67`) into refusals. Check that every reference SQL in `eval/datasets/golden_cases.yaml` and `golden/seed/` still passes.
4. Add A3 to the numeric check.
5. Add the two attacks to `eval/datasets/attacks.yaml`, each with its scripted model, and run `pytest -m attack`.
6. Build B1, then B2 and B3.
7. Build C1.
8. Build D1, then D2 with D3. Run D2 over the reference SQL of every golden case and seed trio first: none may be flagged.
9. Reword `rev-monthly-alice` (D4) and write the note.
10. Build E1 and E2.
11. Build F1 and F2.
12. Read `tests/test_trajectory_contract.py` and `eval/trajectory.py` for any new span name, route or error kind, and update their tables if needed.
13. Documents: technical-explanation.md 7.2 (the three checks and what remains), 7.7 (the figures of an answer), 7.8 (the exchange file), section 5 (retention: the audit log is purged after a year), the section 9 table; README.md (`--debug-prompts`); the docstring of `src/scenarios/recording.py`; decision.md 7.1 (the Fit of requirements 2 and 7) and A11, A16, A17; CLAUDE.md (the repairable kinds, the audit log rule, this plan); this plan's Status line.

## Tests and acceptance
| Test | Shows |
|---|---|
| Every question in `eval/datasets/golden_cases.yaml`, `trajectories.yaml`, the data turns of `scenarios/*.yaml` and `golden/seed/*.yaml` passes the domain-word check; every off-topic message of `attacks.yaml` and of `scenarios/injection-ignore-rules.yaml` fails it | A1 refuses no real question of the repository |
| A classifier that calls "Write a poem about the sea" an in-scope data question: no query, no Composer call, the code's question is printed | A1 holds when the model is fooled |
| The same classifier, and a SQL writer that returns `SELECT 1 AS one`, on "Write a poem about our orders": refused with `NO_DATA_TABLE`, no query reaches the data, no repair | A2 |
| A fooled classifier, SQL writer and Composer (a real query, then a poem with no figure): the poem is not printed, the guarded figures are, the outcome is partial | A3 |
| The oracle model of `tests/test_golden_cases.py` still passes all 16 cases | A3, D1 and D2 break no correct answer |
| Each of A1, A2, A3 and D2 switched off makes its test fail | The tests look |
| With `--debug-prompts`, one exchange line per model attempt, with contents and reply; without it, no file | B1 |
| `tests/test_canary_pipeline.py` and `tests/test_graph_state.py` also search the exchange file, with `--debug-prompts` on | No real customer ID, report ID or canary reaches it |
| A guarded cell value is not in the exchange file; its placeholder is | No rows |
| An answer the Output Guard blocked is not in the file; a passed answer is | Plan 06 rule kept |
| The startup purge removes the exchange file with its trace | Retention |
| `/trace ID --exchange` shows only the user's own turns | Plan 02 rule kept |
| A CEO result with an empty brand shows `(no brand)` and the notice; a brand manager's result never has such a row | C1 |
| A simple turn whose Composer writes only a range still shows every month's figure, from the table; a result of 30 rows shows 20 and says 10 more | D1 |
| A why or report turn has no code-built table | D1 stays on simple turns |
| A scripted SQL writer that sums `sale_price` excluding only `'Cancelled'` on a revenue question is sent back once with `SQL_DEFINITION`, and the repaired query runs | D2 |
| The same query that is never repaired leaves a gap line, not a figure | D2 fails closed |
| A sum of the returned items' value inside a `CASE`, and a question that asks for revenue including returns, are not flagged | D2 has no false alarm there |
| A guard violation in the same query is still never retried | Decision 3.11 kept |
| An audit row 366 days old is deleted at startup and an `audit_purged` row is added; a row 364 days old stays, and a direct `DELETE` of it is refused | E1, E2 |
| `retention.audit_days: 200` fails to load | E2 |
| An `UPDATE` of any audit row is still refused (`tests/test_audit.py`) | E1 keeps append-only |
| `currency: EUR`, `timezone: CET` or `language: de` fails to load with a message that names the key | F1 |
| The USD and UTC rules are in the Composer's and the SQL writer's prompts | F2 |

The offline suite stays green. No live command is needed for parts A to F.

## Open items and defaults used
| Item | Default |
|---|---|
| A1 with no domain word: ask a question or refuse | Ask a question (impl) |
| A1: read the product categories and departments for the word list | Yes, one cached `SystemQuery` (impl) |
| B1: store guarded rows in the exchange file | No (plan 02) |
| D1: a code-built table on why and report turns too | No: simple turns only (impl) |
| D2: a revenue query that is still wrong after the repairs | The sub-question fails with a gap line (impl). The other choice is to run it with a notice that the figure includes returns |
| D4: reword the ambiguous case, or accept both readings | Reword (impl). Accepting both needs a second reference per case |
| E2: the user hash of the purge's audit row | A fixed system value (impl) |
| Q45: who owns order items with no brand | Nobody: brand managers do not see them, the CEO does |

## Risks and pitfalls
- A1 can miss unusual wording. The repository-wide test catches what we know, and the cost of a miss is one question back, not a refusal.
- A3 can replace a correct answer that states no value (for example "Yes, it went up"). The user then gets the figures as tables, which are still true.
- D1 makes simple answers longer. The table is capped at 20 rows, and it is what the question asked for.
- D2 reads only the revenue definition. Another definition (churn, average order value) can drift the same way; the same pattern extends to it, one definition at a time.
- D4 changes a golden case, so every baseline is void until a real run makes it again. Until then a golden run has no baseline to fall below.
- E1 lets old audit rows go. A legal hold, if the client ever needs one, would have to switch the purge off (A6 says there is none).
- The exchange file holds more text than a trace. It is masked, off by default, an operator flag, and kept as long as the trace.
- A new rule, error kind or span name may need the trajectory tables (`eval/trajectory.py`); `tests/test_trajectory_contract.py` fails until they agree.

## As built (2026-10-26)
Everything in parts A to F is built, with the tests in "Tests and acceptance". What differs from the text above, and what the plan did not say:

| Part | As built |
|---|---|
| A1 | `src/guards/domain_words.py`. A follow-up passes when the conversation has an answered data turn on record (`last_answer`, which carries over), not when "the previous turn" was one: no new field of the state, so `fresh_turn_input` and the trajectory tables did not change. The turn that is stopped uses the existing `refuse` node (rule `NO_DATA_WORDS`, outcome `refused`, error kind `OUT_OF_SCOPE`) with a text that is a question. Pieces of column names that are not about the store (`first`, `last`, `code`, `name`, `source`, `created`, `num`) and pieces of two letters are not words. The categories and departments are cached in `cache/product_words.json`, a file of their own, and a failure to read them leaves the list empty and never stops the chat. The glossary is the Golden Bucket's when it is open, else `glossary.yaml`. Several attacks of `attacks.yaml` and the scenario `injection-ignore-rules` name a table ("run SELECT * FROM users"), which is a word of the store: the classifier calls those injections, and the SQL Guard stops what it misses, so the test of the repository asserts the messages that have no word of the store (the poem, the system prompt, the phone numbers) and not every attack. |
| A2 | `NO_DATA_TABLE` in step 4 of the SQL Guard, text in `refusals.py`. |
| A3 | `states_result_value` in `numeric_check.py`; the Composer node judges the draft after the numeric check, and only for a turn with an `ok` result. A figure counts when it was matched to a result (not to the question, the plan, a date or a row count). The model's title is dropped with the draft. |
| attacks | Two attacks and a new category, `off_topic` (`off-topic-poem-with-no-store-word`, `off-topic-poem-with-a-store-word`). The attack `inject-through-a-result-row` now repeats the injected cell in its draft, as the Composer it scripts is talked into, because a draft that states nothing from the data is replaced. |
| B1 | `src/observability/exchange.py`. `PromptParts.debug_contents` is built with `_Sections` (a part and the text a debug rendering shows instead); the builders of the Planner (replan), the SQL writer (earlier results), the Evidence Check and the Composer have a placeholder for each result, and totals are replaced too. The Input Guard's history is shown as it was sent: it holds answers the user already saw. The gateway takes the rendering out of the prompt attributes before the span is made. |
| B2, B3 | `trace ID --exchange`, `trace --conversation ID`, and `/trace [id] --exchange`, `/trace --conversation [id]` (the current conversation by default). |
| C1 | `(no brand)` and the notice in the Result Guard, for a result whose scope label is "all brands"; counted after the small groups are removed. A rule `NO_BRAND_LABELLED` in the guard decision. |
| D1 | `envelope.result_table`, `render_table(..., more_note=True)`. The Composer is told in its contents (not its instructions, so the prompt version did not change) that a table follows. |
| D2 | `src/agent/definition_check.py`, error kind `SQL_DEFINITION`, checked in `guard_run` after the SQL Guard and the label checks. The question it reads is the sub-question's text, which is all the SQL writer sees. A status filter may be a `NOT IN`, one `<>` for each status, a list or an equality of the statuses kept, or `returned_at IS NULL`; a sum over a subquery or a CTE is not judged. Every reference SQL of the golden cases, the trajectories and the seed trios passes it except one, `no-data-carol`, whose SQL had no status filter at all. |
| D3 | A bullet in the Planner's rules; the Planner prompt is version 2. |
| D4 | `rev-monthly-alice` reworded, `GoldenCase.notes` added to the case schema for the note, and the reference SQL of `no-data-carol` given the status filter (with a note). |
| E | Migration `005_audit_retention.sql`, `purge_old_entries` in `storage/audit.py`, `_purge_old_audit_rows` in `cli/main.py`, `retention.audit_days >= 365`. |
| F | `AppConfig` validators (a message that names the key and the supported value; a `Literal` would not say it as plainly). The test of F2 checks the prompts of the SQL writer and the Composer for a question that has a money word and a date word, because the two rules are sent with the question that uses them. |
| tests | The shared test SQL for revenue (`REVENUE_SQL`, `REVENUE_2025`) now follows the definition, and the Composer of the fake SDK client states the first revenue figure it is given. Questions in older tests got a word of the store. `test_golden_cases.py` plays its wrong answers with a figure of the data, because a draft with none is replaced. |

Not done, as in part G: the live checks and the owner's and the author's items. Two things the plan could not know are worth a live look: whether the word list refuses a question a real user asks, and whether a real Composer, told that a table follows, writes commentary and not a copy of it.
