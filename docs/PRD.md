# OpsFleet Data Analysis Agent: Product Requirements Document
Status: draft v0.1, 2026-10-22. Built from [decision.md](decision.md), [Original_Requirements.md](requirements/Original_Requirements.md) and [client-QA.md](requirements/client-QA.md).

Status tags keep the meaning from decision.md: [DECIDED], [PROPOSED], [OPEN], [NOT SPECIFIED].

## Contents
[1. Summary](#1-summary) · [2. Problem and goals](#2-problem-and-goals) · [3. Users](#3-users) · [4. Scope](#4-scope) · [5. User stories](#5-user-stories) · [6. Functional requirements](#6-functional-requirements) · [7. Non-functional requirements](#7-non-functional-requirements) · [8. Deliverables](#8-deliverables) · [9. Success metrics](#9-success-metrics) · [10. Assumptions and defaults](#10-assumptions-and-defaults) · [11. Risks](#11-risks) · [12. Open questions](#12-open-questions) · [13. Implementation plan](#13-implementation-plan)

## 1. Summary
- We build a chat agent for non-technical executives of a retail company. It answers sales questions in plain English on BigQuery data (`bigquery-public-data.thelook_ecommerce`).
- It discusses results, explains causes, and writes reports with action items.
- Each user is a brand manager and sees only data for their brands. The CEO sees all brands.
- PII never appears in an answer. Code, not the AI model, enforces every security rule.
- Users can delete their own saved reports, after one clear confirmation.
- The prototype is a CLI. It covers four areas: Safety and PII, High-Stakes Oversight, Resilience, Observability. The other four requirements are design-only [DECIDED, client Q&A].

## 2. Problem and goals
#### Problem
Executives ask questions like "Why did our churn rate spike last month?" or "create a report for Q1 including insights and actions items for Q2" [brief, verbatim — kept including the brief's own "actions items" wording]. Today an analyst must write the SQL by hand (our own framing), and the executive will not notice a wrong number [DECIDED, decision.md §1].

#### Goals
| ID | Goal |
|---|---|
| G1 | Executives get correct, query-backed answers without writing SQL |
| G2 | Answers explain causes, not only totals, and say how vague words were read |
| G3 | Zero PII in any answer. Each user sees only their brands. |
| G4 | Destructive actions need one clear confirmation and can be undone |
| G5 | Failures give an honest message and a trace ID. The interface never crashes. |
| G6 | The team can see when and why the agent fails |
| G7 | The design shows how the agent learns from reviewed feedback and is evaluated |

#### Non-goals
- Writes to BigQuery. The database is read-only.
- Data governance, the ingestion pipeline and data modeling. Another team owns them [DECIDED, client Q&A]. Whether this also covers the Golden Ingest Job is still open (Q40, section 12).
- A web frontend in the prototype; the prototype uses a CLI [DECIDED, brief]. Production uses a React chat on Cloud Run [PROPOSED, decision.md §2 diagram — not stated by the client].
- An outside search service for the Golden Bucket. We use our own mock trios [DECIDED, client Q&A].

## 3. Users
| Persona | Needs | Data access |
|---|---|---|
| Brand manager (executive) | Plain answers about their own brands | Only their brands |
| CEO | All brands. | All brands, by an explicit "all brands" flag [PROPOSED, our fix for S14 — not yet a client decision] |
| Analyst | Curates the Golden Bucket. Wants the agent to reuse their logic. | Design-only; access model not specified |
| Admin (OpsFleet) | Controls access, audit and budgets. Debugs failures. | Audit log; traces (via trace ID); metrics dashboards |

## 4. Scope
| Brief requirement | Prototype | Design-only |
|---|---|---|
| R1 Hybrid Intelligence (Golden Bucket) | Optional: small seeded trio set with search ("a nice touch") | Yes |
| R2 Safety and PII masking, brand scope | Yes | |
| R3 High-Stakes Oversight (delete saved reports) | Yes | |
| R4.1 User-level memory | Out of scope (owner decision, 2026-10-22) | No |
| R4.2 System-level learning | | Yes |
| R5 Resilience and graceful errors | Yes | |
| R6 Quality assurance | A pytest suite for our own guard/safety code, as part of testing R2/R3/R5/R7 — decision.md's own §8 already proposes this stand-in, but it is unapproved by the client, not a confirmed R6 prototype slice [PROPOSED] | Full eval pipeline |
| R7 Observability | Yes (local) | Production stack |
| R8 Persona management | Out of scope (owner decision, 2026-10-22). One static persona file only. | No |

Constraints: 6-12 hour time box [brief]; CLI interface [brief]; runs on another machine [brief]. The brief says to preferably use a Gemini model and be mindful of its free-tier rate limits, not that Gemini is mandatory. Python is our own choice, inferred from the brief's `pip install -r requirements.txt` setup step, not a stated requirement.

## 5. User stories
| ID | As a... | I want to... | So that... |
|---|---|---|---|
| US1 | Brand manager | ask "What was monthly revenue for my brands this year?" | I get a number without SQL |
| US2 | Brand manager | ask "Why are users in state X underspending compared to state Y?" | I learn causes, not only totals |
| US3 | Brand manager | ask "create a report for Q1 including insights and actions items for Q2" | I can act on it |
| US4 | Brand manager | ask "Who are my top customers?" | I see spend ranking, without names or emails |
| US5 | Brand manager | ask "What data do you have?" | I know which questions I can ask |
| US6 | Brand manager | ask follow-up questions in the same conversation | I can discuss the result |
| US7 | Brand manager | save a report and later say "Delete all the reports we made in this conversation" or "Delete all reports mentioning Client X" | My library stays clean |
| US8 | Brand manager | see exactly which reports will be deleted before it happens | I never delete the wrong thing |
| US9 | Brand manager | undo a delete | a mistake is not final |
| US10 | Any user | get an honest message when something fails | I am never left with a crash |
| US11 | CEO | ask about any brand | I see the whole company |
| US12 | Admin | open a full trace from a trace ID | I can debug a failed turn |
| US13 | Admin | see metrics per turn (success, guard blocks, cost) | I know when the agent is failing |
| US14 | Brand manager | ask "Compare product X and product Y, and why do they perform differently" | I understand relative product performance, not only one number |
| US15 | Brand manager | ask "What is up-to-date revenue by product?" | I get a current figure, not a stale report |

## 6. Functional requirements
Priority: P0 = prototype must have, P1 = prototype should have, D = design-only.

### 6.1 Conversation and analysis
| ID | Requirement | Priority |
|---|---|---|
| F1 | The CLI starts a chat for a named user (`--user alice`) and keeps the conversation history | P0 |
| F2 | The agent turns a question into SQL, runs it on BigQuery, and answers in plain English | P0 |
| F3 | The agent splits "why" and multi-step questions into at most 4 sub-questions | P0 |
| F4 | The agent writes reports with insights and action items on request | P0 |
| F5 | The agent answers questions about the data structure (tables, columns, what is possible) | P0 |
| F6 | Every number in an answer comes from a query result. The agent says how it read vague words (for example "last month"). | P0 |
| F7 | Out-of-scope requests (not data analysis) get a polite refusal | P0 |
| F8 | The agent uses similar past trios (question, SQL, report) as examples | P1 (optional) / D |
| F9 | The agent compares two products' performance and says why they differ, and reports up-to-date revenue by product | P0 |

### 6.2 Safety, PII and access (R2)
| ID | Requirement | Priority |
|---|---|---|
| F10 | Each user sees only data for their brands [DECIDED]. The CEO has an explicit "all brands" flag; an empty brand list means no access, never "all" [PROPOSED, our fix for risk S14 — not a client decision]. | P0 |
| F11 | Code adds the brand filter to every query. The model cannot remove it. | P0 |
| F12 | Only one `SELECT` or `WITH` statement runs. Only the four approved tables. A `LIMIT` is always added. | P0 |
| F13 | PII columns are blocked in SQL, also through aliases, `CONCAT` and `SELECT *` | P0 |
| F14 | PII is masked in result rows before the model sees them. Each real customer ID becomes a fixed fake ID for the session (`Customer #48213`). | P0 |
| F15 | Groups smaller than k = 10 people are hidden. Rows sent to the model are capped (50, given as an example in the source). Exception: ranked spend lists may show one row per customer under a fake session ID, never joined to demographic columns [PROPOSED, S7/A3 — decision.md's own verdict (§9.3) says the PRD still needs to define this customer-row rule; we keep it open ourselves, not as a pending client approval]. | P0 |
| F16 | The final answer is scanned for PII before it is shown | P0 |
| F17 | Prompt injection in user text or data does not change the rules, because code enforces them | P0 |

### 6.3 Saved Reports and destructive actions (R3)
| ID | Requirement | Priority |
|---|---|---|
| F20 | Users can save a report. A report stores owner, conversation ID and text [client default, Q5]. A title field also appears in Phase1-decisions.md's own design (matching and preview use it), but it was dropped from decision.md's condensed Q5 default — Saved Reports' full field list is still open (Q5). | P0 |
| F21 | Users can list and delete only their own reports | P0 |
| F22 | Delete flow: find, preview, confirm, then delete. The model extracts only intent and a selector; a tool finds the IDs, always filtered to the user's own reports. The brief's own examples give both a scope ("the reports we made in this conversation" — needs a conversation ID, see risk S8) and a text match ("mentioning Client X"). decision.md keeps the scope half (via Q5's conversation-ID field, A20, risk S8), but its own §3.6 summary never carries over the text-match mechanism — that is designed in Phase1-decisions.md (full-text match on title, body and extracted entities, demonstrated against this exact example). | P0 |
| F23 | Friction by risk: one report needs y/N. A list needs "yes". A large batch or "all" needs typing `DELETE <n>` (n = the count being deleted). | P0 |
| F24 | The confirmation is bound to a hash of the exact ID set. If the set changes, the user must confirm again. | P0 |
| F25 | Deletes are soft, with 30-day undo | P0 |
| F26 | Every save, delete and undo goes to an append-only audit log | P0 |
| F27 | A retry never repeats an effect (idempotency key including the turn ID) | P0 |
| F28 | The confirmation pause survives a restart (saved checkpoint) | P0 |

### 6.4 Resilience (R5)
| ID | Requirement | Priority |
|---|---|---|
| F30 | Syntax errors and unknown-column errors are repaired up to 2 times with the exact column list; other SQL errors follow F31-F33. | P0 |
| F31 | An empty result gets one check-and-broaden query, then an honest "no data" | P0 |
| F32 | A guard violation is never retried. The agent explains the refusal. | P0 |
| F33 | A too expensive or too slow query asks the user to narrow the question | P0 |
| F34 | Loops stop on: max tries, call budget, timeout, same SQL twice, same error twice | P0 |
| F35 | Gemini 429 and 5xx errors: retry with growing waits, pause a failing provider, fall back to a sibling model | P0 |
| F36 | Every failure ends with a friendly message and a trace ID. The CLI never crashes. | P0 |
| F37 | A `--chaos` flag injects 429s, timeouts and bad SQL to show the resilience | P1 |

### 6.5 Observability (R7)
| ID | Requirement | Priority |
|---|---|---|
| F40 | Each turn has a short trace ID. Every step, model call, tool call and query is a span. | P0 |
| F41 | Traces record model, prompt and persona versions, plus latency and errors. (Token counts are our own addition, not in the source.) | P0 |
| F42 | Each turn writes one metrics row to `runs.jsonl`: task success, guard blocks, PII at output and cost per question [decision.md's own list]. Latency is our own addition, sourced from Phase1-decisions.md's fuller metrics list, not decision.md's condensed one. | P0 |
| F43 | `/trace` shows the last turn's full step list; a metrics summary command shows totals. Looking up any past turn by trace ID is our own extension, pending time. | P0 |
| F44 | Logs and traces are masked so they never carry raw PII [PROPOSED, fix for risk S6 and the open default for Q25 — log/trace PII handling is otherwise [NOT SPECIFIED]] | P0 |
| F45 | Production: OpenTelemetry to Langfuse and Cloud Trace; alerts on any PII at output | D |

### 6.6 Design-only requirements
| ID | Requirement | Design summary |
|---|---|---|
| F50 | R1 Golden Bucket update and retrieval | Trios in Cloud Storage, pgvector hybrid search, top 3 above a threshold. Filtering by brand is [PROPOSED] (risk S5 / Q21), not yet decided. New trios: thumbs-up, saved reports and good runs become candidates (F52); an analyst approves each one; a nightly job re-checks SQL. |
| F51 | R4.1 User memory | Out of scope (owner decision, 2026-10-22). No memory agent, queue or Memory Store, and no learned user profile. |
| F52 | R4.2 System learning | Thumbs-up, saved reports and good runs become candidate trios. An analyst approves each one. Weekly failure mining. |
| F53 | R6 Quality assurance | Three layers: code checks, checks against analyst answers, AI judge from a different model. One PII leak fails the run. Intent is checked with four signals (stated interpretation, execution accuracy against analyst SQL, requirement coverage, and judge rubrics), not analyst-answer matching alone. UX is 5-8 executive sessions plus the System Usability Scale — the scale is named, but its target score is not set yet, and which evalkit layer runs each rubric is open (Q32). |
| F54 | R8 Persona management | Out of scope (owner decision, 2026-10-22). No editor UI, registry, versions, approval, rollback or per-user style. The persona is one static file, `personas/default.yaml`, next to the locked rules (git). It touches only the Composer's prompt; Context Assembly loads it. A tone change is a code change. |

## 7. Non-functional requirements
| ID | Requirement | Target | Status |
|---|---|---|---|
| NFR1 | Response time, simple question | 95% under 30 s | [PROPOSED] |
| NFR2 | Response time, multi-step question | 20-60 s, with progress shown | [PROPOSED] |
| NFR3 | Time limit per turn | About 60 s | [PROPOSED] |
| NFR4 | AI calls per turn | About 8 for a simple turn. "Why" and report turns need their own number. | [OPEN] |
| NFR5 | SQL attempts per sub-question | 1 attempt plus 2 repairs | [PROPOSED] |
| NFR6 | Bytes billed per query | Hard cap in config | [OPEN] |
| NFR7 | BigQuery monthly use | Inside the free 1 TB | [DECIDED] |
| NFR9 | Gemini rate limits | Read from config, not published | [PROPOSED] |
| NFR11 | PII in final output | Zero | [DECIDED] |
| NFR12 | Attack test pass rate | 100% | [PROPOSED] |
| NFR13 | Smallest group shown | k = 10 | [PROPOSED] |
| NFR14 | Rows sent to the model | 50 | [PROPOSED] |
| NFR15 | Access rules unavailable | Block the request | [PROPOSED] |
| NFR16 | The model is never the only checker | Code checks every model output | [PROPOSED] |
| NFR17 | Delete confirmation | One confirmation turn, 30-day undo | [PROPOSED] |
| NFR18 | No duplicate effects on retry | Zero duplicate saves | [PROPOSED] |
| NFR19 | Interface never crashes | Friendly message and trace ID | [DECIDED] |
| NFR21 | Data kept | Audit log 1 year. Transcripts and checkpoints 90 days, without PII. | [PROPOSED] |
| NFR22 | Traces and logs kept | [NOT SPECIFIED] | [NOT SPECIFIED] |
| NFR23 | Persona change | Out of scope (owner decision, 2026-10-22). One static persona file. | [DECIDED] |
| NFR24 | New tool | No graph change | [PROPOSED] |
| NFR28 | Runs on another machine | Setup instructions; Docker optional | [DECIDED] |
| NFR29 | Who sees which data | Only the user's brands; the CEO sees all | [DECIDED] |

IDs match decision.md section 4. Production-only targets (NFR8 daily budget, NFR10 scale, NFR20 alerts, NFR25 release safety, NFR26-27 quality and usability) stay in the architecture document.

## 8. Deliverables
| # | Deliverable | Covered by plan |
|---|---|---|
| D1 | Architecture diagram in Mermaid showing the building blocks, services, compute and flow, including the Input-Guard-to-Tool-Router delete-or-save shortcut; each infra/framework/service choice named with an explanation | 11 |
| D2 | Technical explanation: reasoning for each chosen service/model/framework, data flow and storage between components, error handling **and fallback strategies**, how each of the 8 brief requirements is solved, written to be detailed enough for a reader to understand how the system runs in production | 11 |
| D3 | Setup instructions (including `requirements.txt` / `pip install -r requirements.txt`) and an example run | 01, 11 |
| D4 | Working CLI prototype for R2, R3, R5, R7 | 01-09 |
| D5 | Public GitHub repository with docs, code and diagram | 11 |
| D6 | Framework choice (LangGraph) with an honest experience statement | 11 |

D2 and D3 jointly cover the brief's technical-explanation sub-items 2.1-2.5 (brief Deliverable 2); sub-item 2.4 (setup instructions and an example run) is reorganized into D3 for clarity, so D2 itself only covers 2.1, 2.2, 2.3 and 2.5. D2's "data flow and storage" wording is grounded in the brief's intro note on where data is stored and handled, not sub-item 2.2 alone.

## 9. Success metrics
| Metric | Target | How we measure it |
|---|---|---|
| PII leaks in the eval and attack sets | 0 | Pytest, attack tests, output scanner |
| Numbers grounded in a real query result | 100% | Numeric-grounding check, eval suite |
| Groups below k = 10 suppressed | 100% | Eval suite |
| Duplicate saves/deletes on retry | 0 | Idempotency pytest |
| Budget respected (calls, time, bytes) | 100% of turns stay inside the configured budget | `runs.jsonl`, eval suite |
| Brand scope escapes (our own derived metric; no source names this exact metric) | 0 | Attack tests built from other users' brands |
| Golden test cases answered correctly | No lower than the first baseline run minus a tolerance, set after that run — not a fixed number yet [OPEN, NFR26] | Result checks against reference SQL |
| Turns that crash the CLI | 0 | `--chaos` runs |
| Delete flows that remove a report not in the preview | 0 | Pytest on the confirmation flow |
| Simple turns under 30 s | 95% [PROPOSED, NFR1 — depends on open Q30] | `runs.jsonl` |
| Failed turns that produce a trace ID | 100% | `--chaos` runs and `runs.jsonl` |

The 100%/0 targets above translate README-evaluation.md's pass/fail floors (numeric_grounding, small_groups_suppressed, duplicate_effect, budget_respected) into percentages; the source states them as binary rules ("every," "never," "no group below k"), not as percentages.

## 10. Assumptions and defaults
We proceed with these defaults until the client answers otherwise.

| ID | Assumption |
|---|---|
| A1 | Confirmed by the client: brand scope is the user's brands. Assumed, not yet confirmed: the filter applies per order item, so a mixed order's total is split and only the user's-brand share counts; answers say so. |
| A2 | The four-layer access strategy is decided (decision.md §3.4), but the exact PII/column split behind it is still open (Q11), and the real `users` columns are not yet checked against the live schema (Q4 is also open). The source's own best guess, not confirmed: likely identifiers are name, email, address fields and lat/long; likely quasi-identifiers are age, gender, state, city and traffic source. The real customer ID (`users.id`) is not blocked — the Result Guard masks it with a fixed fake ID per session instead (see A3), because blocking it outright would break "top customers" questions. |
| A3 | A fake ID per session is not PII; the real `users.id` never leaves the Result Guard (owner decision). Per-customer rows are given only as an example for ranked spend lists, never joined to demographic columns — decision.md's own verdict (§9.3) says the PRD still needs this customer-row rule (S7), so we keep it open (see section 11, "top customers" risk). |
| A4 | Prototype users and brands come from `users.yaml`. Production uses a signed login token (the client said JWT) with the user's brands. Beyond CEO, production roles default to the Phase1 draft's four roles — exec (i.e., brand manager), analyst, admin — pending Q42. |
| A5 | Churn is no order in 90 days. "Last month" is the calendar month. We anchor "up-to-date" metrics to the dataset's latest order date and say so, since the public dataset may lag behind the real calendar (Phase1-decisions.md's note to verify the dataset's latest date). Currency and time zone are A17, below. |
| A6 | Users delete only their own reports. Whether any report is under a legal hold is unconfirmed (Phase1-decisions.md asks us to confirm this); we assume not for the prototype. |
| A7 | Production runs on Vertex AI in the US region |
| A8 | About 50 users and 99.5% availability (see NFR10, Q30 — still open) |
| A9 | Production trios: one per line, up to about 10k, in English; analysts review a few dozen candidates a week |
| A10 | Withdrawn (owner decision, 2026-10-22): there is no persona editing or approval flow |
| A12 | Withdrawn (owner decision, 2026-10-22): there is no per-user "My style" |
| A13 | The dataset is synthetic, so free-tier Gemini terms are fine |
| A14 | The Gemini model IDs named in decision.md §3.2 exist at build time; we re-check them before relying on them |
| A15 | Confirmed by the client: the Golden Bucket does not exist. We define the trio format and seed a small set ourselves. |
| A16 | English only |
| A17 | Currency is USD and time zone is UTC (not stated by any source) |
| A18 | The reviewer has a Google Cloud project and a Gemini API key; BigQuery access is implied but not separately stated in the source |
| A19 | One user at a time on one machine in the prototype |
| A20 | Saved reports hold no PII and record their conversation ID |
| A21 | The dataset has no free-text cells that carry PII (not verified; if wrong, value-level scanning becomes critical, not just column-level blocking) |
| A23 | Every `order_items` row joins to `products.brand`, so the brand filter covers all order data; unjoined rows would escape the filter |
| A24 | Brand lists are small and change rarely, so a config file (`users.yaml`) is enough for the prototype |

A11 (retention) and A22 (another team owns ingestion) are carried in this PRD under NFR21 and the section 2 non-goals, respectively, rather than repeated as separate rows here.

## 11. Risks
| Risk | Severity | Mitigation |
|---|---|---|
| The full design does not fit 6-12 hours | High | Build only the four prototype areas. Everything else is design. |
| `bq_client.py` has no dry-run, byte cap, timeout, labels or row cap (S2) | High | Wrap it before any query runs (plan 03) |
| About 8 calls per turn cannot cover a multi-step "why" or report turn (S3 — a top risk per decision.md) | High | A separate, larger call budget for "why" and report turns, config-driven (plan 04) |
| `bq_client.py` logs the full error text, so logs and traces can carry data values (S6) | High | Mask logs (plan 03); mask PII from every span and trace, not only the metrics row (plan 02) |
| Free-tier Gemini limits break the demo | High [our editorial judgment; no source severity] | Rate limiter, fewer calls per turn, Flash to Flash-Lite fallback |
| An empty brand list is read as "all brands" (S14) | High | Explicit CEO flag; empty means no access; startup check of mapped brands against real brand names (S1) — both [PROPOSED], not yet a client decision |
| "Top customers" shows groups of one, which k = 10 would hide (S7) | High | Fake per-session IDs; ranked spend lists only, no demographics — decision.md's own verdict (§9.3) says this rule still needs defining; we keep it open ourselves (see A3) |
| A wrong but valid query gives a believable number | High [our editorial judgment; no source severity] | Numbers only from results, numeric grounding check, state how vague words were read |
| Prompt injection in user text, retrieved trios or database text | — | Untrusted tags around retrieved/DB text, fixed structured-output formats at each guard, confirmation needed only from the user — not zero, so code enforces every rule rather than trusting the model |
| Text streams before the Output Guard runs (S9) | Medium | Stream progress only until the scan passes |
| A customer buys from many brands; a manager sees only part of that spend (S15) | Medium | Label customer totals "spend on your brands" |

## 12. Open questions
decision.md §11 marks Q5, Q6, Q10, Q11, Q15, Q30 and Q41 as the questions to answer before this PRD (none are answered yet), and separately asks the PRD to carry every OpsFleet-client question from its section 10. Both lists are below, plus Q3, which we own ourselves and need answered early for plan 04's budget design.

| ID | Question | Default if unanswered | Owner | Priority |
|---|---|---|---|---|
| Q3 | Turn budget: calls, tokens, seconds, bytes per turn and per query | Simple turn about 8 calls, 60 s; low byte cap in config | Us | P0 |
| Q5 | Saved Reports: fields, storage, soft or hard delete, undo time, legal hold? | Owner, conversation ID, text. Soft delete, 30-day undo. (Our PRD adds "title" as a candidate field, not yet confirmed.) | Client | P0 |
| Q6 | What do churn, underspending, "last month", currency and time zone mean? | Churn 90 days, calendar month, USD, UTC | Client | P0 |
| Q10 | Which Gemini models; is Pro in the routing table? | Flash only, Pro escalation off, Pro for the AI judge | Client | P0 |
| Q11 | Which columns are PII? Which does the SQL Guard block, and which does the Result Guard mask? | Block direct identifiers; others only in groups of 10 or more (see A2 for the unverified column names) | Client | P0 |
| Q15 | What is the daily budget per user? | Alert at 80%; amount from client | Client | P1 |
| Q17 | Where does history live? How does a user delete it? | Checkpoints 90 days | Client | P1 |
| Q18 | What is the production target and region? | Cloud Run, Cloud SQL, Vertex AI, US | Client | P1 |
| Q19 | Who builds and labels the golden test set and reference SQL? | Analysts, a few dozen items a week | Client | P1 |
| Q25 | Which metrics and alert limits? How long are logs kept? | Alert on any PII, 90 days, mask PII before writing | Client | P1 |
| Q26 | How do persona versions, rollback and editing work? | They do not exist. One static persona file (owner decision, 2026-10-22) | Us | Answered |
| Q30 | How many users, what availability, and which response time (20 s or 30 s)? | 50 users, 99.5%, 95% under 30 s | Client | P1 |
| Q31 | Is there a path to a human analyst for failed turns? | No, honest message only | Client | P2 |
| Q32 | How do we measure user experience? Which evalkit layer runs each rubric? | 5-8 executive sessions; layers set from the rubric file. (The System Usability Scale is named in NFR27, but no target score is set yet.) | Client | P2 |
| Q33 | How do web, email and chart extensions stay safe? Who owns the email domain list? | Same confirmation step and PII scan | Client | P2 |
| Q34 | Do we support only English? | Yes | Client | P2 |
| Q40 | Does "ingestion is out of scope" also cover the Golden Ingest Job? | No — keep a short Golden update design, because requirement 1 asks for it | Client | P1 |
| Q41 | In mixed orders, does the brand filter apply per item? Do order totals cover only the user's brands? | Yes to both, and answers say so | Client | P1 |
| Q42 | Which roles exist besides brand manager and CEO (analyst, admin)? | Analyst and admin, plus CEO | Client | P2 |

## 13. Implementation plan
The prototype is split into eleven plans in [plans/](plans/). Each plan is one `plan.md`. Plans run roughly in order; 10 is optional.

Plan 07 owns the full graph, including the Result Guard, Output Guard, Tool Router and HITL Gate wiring, the Input-Guard-to-Tool-Router delete-or-save shortcut (decision.md's diagram labels this edge "delete or save") and the numeric grounding check — against a stubbed Saved Reports tool. Plan 08 then builds the real Saved Reports store and tools behind that stub, plus checkpoint persistence and the retry-idempotency test. This ordering (option A, chosen over having plan 08 add the routing itself) keeps the graph's shape in one place.

F5, F33 and F35 appear in two plans because the wrapper/gateway plan (03/04) builds the mechanism and plan 07's graph calls it; that is intended, not a conflict. Plan 08 and plan 09 both touch the idempotency retry test for the same reason: plan 08 builds the feature and its test, and plan 09's suite exercises it as part of proving the whole prototype.

| Plan | Goal | Requirements |
|---|---|---|
| [01-foundation](plans/01-foundation/plan.md) | Project skeleton, config, user and brand mapping, CLI shell, setup | F1, F10, D3 |
| [02-observability](plans/02-observability/plan.md) | Trace IDs, spans, `runs.jsonl`, `/trace` and a metrics summary command | F40-F44 |
| [03-bigquery-access](plans/03-bigquery-access/plan.md) | Safe BigQuery wrapper with dry-run, byte cap, timeout, row cap, schema lookup, startup brand-list check (S1) | F5, F33, NFR6 |
| [04-llm-gateway](plans/04-llm-gateway/plan.md) | Gemini client with routing, rate limiting, retries, circuit breaker, fallback, budget | F35, F37, NFR4 |
| [05-sql-guard](plans/05-sql-guard/plan.md) | Parse-tree SQL checks, PII column block, forced brand filter and LIMIT | F11-F13 |
| [06-result-output-guards](plans/06-result-output-guards/plan.md) | PII masking, fake customer IDs, k-anonymity (with the top-customer exception), row cap, output scan | F14-F16 |
| [07-agent-graph](plans/07-agent-graph/plan.md) | Full LangGraph pipeline (guards, routing, HITL, delete-or-save shortcut) from Input Guard to Composer, with repair and bounded replan loops, against a stubbed Saved Reports tool | F2-F7, F9, F17, F30-F36 |
| [08-saved-reports-hitl](plans/08-saved-reports-hitl/plan.md) | Saved Reports store and tools, checkpoint persistence, confirmed delete, undo, audit log, idempotency retry test | F20-F28 |
| [09-evaluation](plans/09-evaluation/plan.md) | Pytest suite, golden cases, attack tests, chaos runs | Section 9 |
| [10-golden-bucket](plans/10-golden-bucket/plan.md) | Seeded trios and glossary with in-memory search (optional) | F8 |
| [11-documentation](plans/11-documentation/plan.md) | HLD, Mermaid diagram, technical explanation, README, example run | D1, D2, D3, D5, D6, F45, F50, F52, F53 |

The static persona file (`personas/default.yaml`, loaded under plan 01) is all that exists of F54. It is not required for any P0 item.
