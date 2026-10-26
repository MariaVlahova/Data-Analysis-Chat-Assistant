# Requirements ledger (seed)

Copy this file to your working folder before you fill it. Do not fill it in place. `SKILL.md` explains the method.

- `Src`: `brief` is `docs/requirements/Original_Requirements.md`, `qa` is `docs/requirements/client-QA.md`, `decision` is `docs/decision.md`.
- `Quote`: words copied from the source. `…` spans from the words before it to the next place the words after it appear.
- `Class`: what the sources ask of the repository. `PROTO` the prototype does it. `DESIGN` the design documents explain it (building it is allowed). `DELIV` a file or document must exist and be in git. `OOS` the sources exclude it, so the code must not build it and the documents must say so. `CTX` not a requirement; the note says why.
- `Look`: where to start. A starting point is not evidence.
- `Verdict`, `Evidence`, `Note`: empty until verified. Verdicts and evidence rules are in `SKILL.md`.
- The brief rows were written against the brief and Q&A of 2026-10-22. If a quote is no longer found, or words are under no row, the source changed: fix the rows first (`--stage extract`).
- Brief rows keep the class the brief and the client's answers give them. Where an owner decision in `decision.md` goes against that (R4.1, R8), the verdict is `CONFLICT` and the owner rules; do not turn it into `OOS-OK`.


## Context (no verification: the lead audits each reason)

These rows are not requirements. The report lists every one with its reason.

| ID | Src | Quote | Class | Look | Verdict | Evidence | Note |
|---|---|---|---|---|---|---|---|
| T0 | brief | AI Retail Company The Challenge: Design and Build a Data Analysis Chat Assistant | CTX | | | | title and heading of the brief |
| T0b | brief | (OpsFleet is the client for this project and the Team Lead + Harry are the Project managers) | CTX | | | | names the client and the project managers |
| T6 | brief | Feel free to ask about any assumptions you need to make, or follow ups on requirements, we (OpsFleet team) are the client for this project. | CTX | | | | invitation to ask questions; the answers are the qa rows |
| ASSESS | brief | Our assessment will focus mainly on system design, the technical explanation, and an elegant Prototype that fits the Prototype requirements | CTX | | | | how the client will judge; the testable parts are the D and R rows |
| TIME | brief | Time Expectation We expect this assignment to take between 6 to 12 hours of work. | CTX | | | | effort estimate, not a property of the code |
| LLM2 | brief | You can get a free API key from Google AI Studio. | CTX | | | | information for the candidate |
| R2c0 | brief | The raw transaction logs contain sensitive data. | CTX | | | | the reason for R2c, not an instruction |
| QA0 | qa | Client Q&A (OpsFleet) Thread between Mariya Vlahova and Harry (OpsFleet, AI Assistant). Copied verbatim. Recorded on 2026-10-22; the message dates were not given. | CTX | | | | heading and provenance of the thread |
| QA0b | qa | Mariya Vlahova Questions: … extract glossary, prefernce? | CTX | | | | the author's three questions; Harry's answers are QA1 to QA3 |
| QA0c | qa | Harry - AI Assistant [9:44 AM] @Mariya Vlahova happy to answer these: | CTX | | | | reply header |
| QA2b | qa | You can assume another team handles ETL and data modeling; focus on the agent itself. | CTX | | | | guidance on where to spend effort |
| QA3d | qa | Hope that helps — let us know if anything else comes up. Thanks, and good luck! | CTX | | | | closing line of the thread |
| QA4 | qa | Mariya Vlahova [9:56 AM] ok please explain … displaying these PIIs in the final output. Each user should only be able to analyze data on products related to him. | CTX | | | | the author's question; Harry's answer is QA6 to QA8 |
| QA5 | qa | Harry - AI Assistant [9:57 AM] @Mariya Vlahova sure — here's how to think about it: | CTX | | | | reply header |
| QA9 | qa | Thanks, and good luck! | CTX | | | | closing line of the thread |

## Task and architecture (brief)

| ID | Src | Quote | Class | Look | Verdict | Evidence | Note |
|---|---|---|---|---|---|---|---|
| T1 | brief | You are building data analysis agent for a Retail Company's non-technical executive teams | PROTO | src/agent/nodes/composer.py; personas/default.yaml; tests/test_prompt_contracts.py | | | |
| T2 | brief | These executives need to ask complex questions about sales, and performance | PROTO | src/agent/graph.py; src/agent/nodes/planner.py; scenarios/ | | | |
| T3 | brief | e.g., "Why are the users for sate X underspending? And how does it compare to users in state Y?" | PROTO | src/agent/nodes/planner.py; scenarios/why-casa-lino-returns.yaml; scenarios/why-zephyr-decline.yaml | | | |
| T4 | brief | Why did our churn rate spike last month? | PROTO | glossary.yaml; scenarios/dates-last-month.yaml; src/agent/nodes/planner.py | | | |
| T5 | brief | create a report for Q1 including insights and actions items for Q2 | PROTO | scenarios/report-first-quarter.yaml; src/agent/nodes/composer.py | | | |
| DB1 | brief | The agent will have access to: * The Database: A read-only connection to a SQL database containing raw transaction logs. | PROTO | src/data_access/safe_runner.py; src/guards/sql_guard.py; tests/test_safe_runner.py; tests/test_sql_guard.py | | | |
| TASK1 | brief | The Task: Design a production-ready full High-Level Design (HLD), accompanied by a detailed technical explanation | DELIV | docs/architecture.md; docs/technical-explanation.md | | | |
| TASK2 | brief | and build a data analysis chat-agent prototype that allows executives to ask question about this data naturally and discuss about it. | PROTO | src/cli/commands.py; src/agent/runtime.py; tests/test_local_e2e.py | | | |
| TASK3 | brief | The system should be easily extendable for new capabilities (generating graphs, sending reports via mail, searching the web for trends, etc). | DESIGN | src/tools/registry.py; src/tools/base.py; tests/test_tool_router.py; docs/architecture.md | | | |
| HLD1 | brief | Note: We expect to see what services you aim to use, what the communication is between the components, how and where data is stored and handled. | DELIV | docs/architecture.md; docs/technical-explanation.md | | | |
| HLD2 | brief | The "detailed technical explanation" needs to be detailed enough for us to understand how this system will function in production. | DELIV | docs/technical-explanation.md | | | |

## R1 Hybrid Intelligence and the Golden Bucket (design; a seeded slice is optional), with the client's answers

| ID | Src | Quote | Class | Look | Verdict | Evidence | Note |
|---|---|---|---|---|---|---|---|
| R1a | brief | Requirements: 1. Hybrid Intelligence: The agent cannot rely on BQ data alone. It must use the "Golden Bucket" to understand how analysts previously interpreted questions | DESIGN | src/golden/; src/agent/nodes/context.py; tests/test_golden_graph.py; docs/technical-explanation.md | | | |
| R1b | brief | and apply similar logic to new queries | DESIGN | src/golden/index.py; src/agent/prompts/; tests/test_golden_index.py | | | |
| R1c | brief | Explain how you will update the golden bucket over time | DESIGN | docs/decision.md; docs/technical-explanation.md; src/golden/validate.py | | | |
| R1d | brief | and how you will provide relevant data at user query time | DESIGN | src/golden/index.py; src/golden/load.py; docs/technical-explanation.md | | | |
| DB2 | brief | The "Golden Knowledge" Bucket: A data lake containing historical "Trios" (Question → SQL Query → Analyst Report) created by human experts (Theoretical). | DESIGN | src/golden/schema.py; golden/seed/; docs/technical-explanation.md | | | |
| QA1a | qa | 1. Golden Bucket — it's entirely theoretical; there's no real dataset or predefined schema. Define the Trio format yourself (Question → SQL Query → Analyst Report) | DESIGN | src/golden/schema.py; docs/decision.md | | | |
| QA1b | qa | and seed a small sample set as you see fit. | DESIGN | golden/seed/; glossary.yaml; src/golden/load.py | | | |
| QA1c | qa | The design requirement is to explain how you'd update the bucket over time and retrieve relevant Trios at query time | DESIGN | docs/decision.md; docs/technical-explanation.md | | | |
| QA1d | qa | a small seeded set with retrieval in the prototype is a nice touch but not required. | DESIGN | src/golden/index.py; tests/test_golden_index.py | | | optional; MET if built and tested |
| QA2a | qa | 2. Data governance / ingestion pipeline / RAG — out of scope. | OOS | src/golden/validate.py; src/cli/golden_cmd.py | | | check that no ingestion pipeline is built or claimed; the Ingest Job checks (golden validate) are a design choice to judge |
| QA2c | qa | There's no external RAG to connect to — work with mock data you create yourself (tied to the Golden Bucket above). | DESIGN | golden/seed/; src/localdata/ | | | |

## R2 Safety and PII masking (prototype), with the client's scope answer

| ID | Src | Quote | Class | Look | Verdict | Evidence | Note |
|---|---|---|---|---|---|---|---|
| R2a | brief | 2. Safety & PII Masking: The agent is only allowed to answer analysis questions | PROTO | src/agent/nodes/input_guard.py; tests/test_routing.py; tests/test_graph_routing.py; scenarios/injection-ignore-rules.yaml | | | |
| R2b | brief | and needs to be safeguarded against malicious users | PROTO | src/guards/; eval/datasets/attacks.yaml; tests/test_attack_offline.py; tests/test_compromised_model.py; tests/test_sql_guard.py | | | |
| R2c | brief | The agent is strictly forbidden from displaying these PIIs in the final output. | PROTO | src/pii.py; src/guards/result_guard.py; src/guards/output_guard.py; src/guards/pseudonymizer.py; tests/test_output_guard.py; tests/test_result_guard.py; tests/test_pii.py; tests/test_canary_pipeline.py | | | |
| R2d | brief | Each user should only be able to analyze data on products related to him. | PROTO | src/access/users.py; users.yaml; src/guards/scope_views.py; tests/test_scope_rewrite.py; tests/test_scope_users.py; tests/test_users.py | | | |
| QA6a | qa | Each user in the system is a brand manager responsible for one or more brands. | PROTO | src/access/users.py; users.yaml; tests/test_users.py | | | |
| QA6b | qa | They should only be able to analyze data tied to those brands — meaning customer behavior, orders, and product performance for their brands only. | PROTO | src/guards/scope_views.py; tests/test_scope_rewrite.py; tests/test_scope_users.py | | | customer behaviour, orders and product performance are three separate paths |
| QA6c | qa | The CEO is the exception and sees everything. | PROTO | src/access/users.py; scenarios/scope-ceo-total.yaml; tests/test_users.py | | | an empty brand list must never mean all brands (S14) |
| QA7a | qa | The mapping between managers and brands is not in the thelook_ecommerce dataset — you'll need to define your own structure for that (a config file, an access table, or similar). | PROTO | users.yaml; users.local.yaml; src/access/users.py | | | |
| QA7b | qa | In production, the frontend would send a JWT containing the user's scopes (e.g., which brands they can access) | DESIGN | docs/architecture.md; docs/technical-explanation.md | | | |
| QA7c | qa | and the agent enforces access based on those scopes. | PROTO | src/guards/scope_views.py; src/agent/nodes/policy.py; tests/test_scope_rewrite.py | | | enforced in code, not in the prompt |
| QA8 | qa | So for the prototype, a small hardcoded or config-driven mapping is perfectly fine — just make sure the agent filters data accordingly. | PROTO | src/access/users.py; users.yaml; tests/test_scope_users.py | | | |

## R3 High-stakes oversight, destructive operations (prototype)

| ID | Src | Quote | Class | Look | Verdict | Evidence | Note |
|---|---|---|---|---|---|---|---|
| R3a | brief | 3. High-Stakes Oversight (Destructive Ops): While the DB is read-only, the agent manages a "Saved Reports" library. | PROTO | src/reports/store.py; src/tools/saved_reports.py; tests/test_reports_store.py | | | |
| R3b | brief | The agent must support inputs like "Delete all reports mentioning Client X" | PROTO | src/tools/saved_reports.py; src/agent/nodes/tools.py; scenarios/delete-all-mentioning.yaml; tests/test_delete_flow.py | | | |
| R3c | brief | Delete all the reports we made in this conversation | PROTO | src/tools/saved_reports.py; scenarios/delete-this-conversation.yaml; tests/test_delete_flow.py | | | |
| R3d | brief | This is a destructive action and requires a strict confirmation flow before execution | PROTO | src/tools/confirmation.py; src/agent/nodes/tools.py; tests/test_delete_flow.py; tests/test_restart_resume.py; scenarios/delete-confirmation-spoofing.yaml | | | |
| R3e | brief | without breaking UX | PROTO | src/tools/confirmation.py; src/reports/messages.py; scenarios/delete-needs-confirmation.yaml | | | |
| R3f | brief | the users are allowed to delete their own reports. | PROTO | src/reports/store.py; tests/test_reports_store.py; tests/test_audit.py; scenarios/scope-other-manager.yaml | | | |

## R4 and R8 (design), and the client's design-only answer; the owner decisions of 2026-10-22 go against it

| ID | Src | Quote | Class | Look | Verdict | Evidence | Note |
|---|---|---|---|---|---|---|---|
| R4a | brief | 4. Continuous Improvement (The Learning Loop): 1. User Level: The agent should remember that "User A" prefers tables while "User B" prefers bullet points | DESIGN | docs/decision.md; docs/technical-explanation.md; docs/architecture.md | | | |
| R4b | brief | as well as learn user preferences over time for example: how deep they like the analysis, do they prefer charts or text, etc. | DESIGN | docs/decision.md; docs/technical-explanation.md | | | |
| R4c | brief | 2. System Level: The agent should be able to learn from past interactions and improve itself. | DESIGN | docs/decision.md; docs/technical-explanation.md | | | |
| R8a | brief | 8. Agility (Persona Management): The CEO wants to change the "tone" of the reports weekly. | DESIGN | personas/default.yaml; src/personas.py; tests/test_personas.py; docs/technical-explanation.md | | | |
| R8b | brief | The system must allow non-developers to update the agent's instructions without redeployment. | DESIGN | personas/default.yaml; src/personas.py; docs/technical-explanation.md | | | |
| QA3a | qa | 3. Memory / preference extraction — design only. | DESIGN | docs/decision.md; docs/technical-explanation.md | | | the owner decision of 2026-10-22 says out of scope, the client says design only |
| QA3b | qa | Continuous Improvement and Persona Management are not prototype requirements; cover them in your HLD and technical explanation. | DESIGN | docs/architecture.md; docs/technical-explanation.md | | | |

## R5 Resilience and graceful error handling (prototype)

| ID | Src | Quote | Class | Look | Verdict | Evidence | Note |
|---|---|---|---|---|---|---|---|
| R5a | brief | 5. Resilience & Graceful Error Handling: The system must detect syntax errors | PROTO | src/data_access/errors_map.py; src/agent/nodes/sql_loop.py; tests/test_bq_errors.py; tests/test_graph_loop.py | | | |
| R5b | brief | or empty returns and attempt to self-correct before giving up | PROTO | src/agent/nodes/sql_loop.py; src/agent/nodes/evidence.py; tests/test_graph_loop.py | | | |
| R5c | brief | without crashing the user interface | PROTO | src/agent/boundary.py; src/agent/nodes/failure.py; tests/test_hardening.py; tests/test_chaos_offline.py; tests/test_cli_agent.py | | | |
| R5d | brief | and without inflating costs | PROTO | src/llm/budget.py; src/data_access/safe_runner.py; tests/test_budget.py; tests/test_safe_runner.py; tests/test_usage_budget.py | | | |
| R5e | brief | The system should be resilient to API/3rd party services failures/downtime | PROTO | src/llm/gateway.py; src/llm/breaker.py; src/llm/limiter.py; src/llm/chaos.py; tests/test_gateway.py; tests/test_breaker.py; tests/test_chaos.py | | | |

## R6 Quality assurance (design) and R7 Observability (prototype)

| ID | Src | Quote | Class | Look | Verdict | Evidence | Note |
|---|---|---|---|---|---|---|---|
| R6a | brief | 6. Quality Assurance: How do you evaluate the agent before deployment? | DESIGN | eval/; evalkit/; docs/technical-explanation.md; tests/test_golden_cases.py | | | |
| R6b | brief | How do you verify that the generated reports answer users intent correctly? | DESIGN | eval/metrics.py; eval/datasets/golden_cases.yaml; src/agent/numeric_check.py; docs/technical-explanation.md | | | |
| R6c | brief | How do you evaluate UX? | DESIGN | docs/technical-explanation.md; docs/decision.md | | | |
| R7a | brief | 7. Observability: We need to know when the agent is failing and why. | PROTO | src/observability/; src/errors.py; tests/test_tracing.py; tests/test_metrics.py | | | |
| R7b | brief | Define the metrics you would track at the agent level | PROTO | src/observability/metrics.py; tests/test_metrics.py; docs/technical-explanation.md | | | |
| R7c | brief | and how you would support deep dive analysis/debugging (understanding what the message correspondence is and what went wrong). | PROTO | src/observability/viewer.py; src/observability/tracing.py; tests/test_viewer.py; tests/test_observability_cli.py; tests/test_logging_mask.py | | | |

## Deliverables and the prototype list (brief)

| ID | Src | Quote | Class | Look | Verdict | Evidence | Note |
|---|---|---|---|---|---|---|---|
| D1a | brief | Deliverables: 1. Architecture Diagram (Mermaid etc): Highlighting the building blocks, services, compute and flow of the system based on the requirements above. | DELIV | docs/architecture.md | | | |
| D1b | brief | In case you are planning to use a infrastructure/framework/service/data store for one of the building blocks, specify which one and include an explanation. | DELIV | docs/architecture.md; docs/technical-explanation.md | | | |
| D2a | brief | 2. Detailed technical explanation covering: 1. Reasoning for the chosen Cloud services / LLM models/frameworks used. | DELIV | docs/technical-explanation.md | | | |
| D2b | brief | 2. Data flow between components (if needs to elaborate on HLD). | DELIV | docs/technical-explanation.md; docs/architecture.md | | | |
| D2c | brief | 3. Error handling and fallback strategies. | DELIV | docs/technical-explanation.md | | | |
| D2d | brief | 4. Setup Instructions and example run | DELIV | README.md; docs/local-testing.md | | | the example run must be copied from a real run, never written by hand |
| D2e | brief | 5. Make sure to include a detailed explanation of how you handle/solve each of the requirements | DELIV | docs/technical-explanation.md | | | check that each of R1 to R8 has its own explanation; a missing one makes this PARTIAL |
| D3a | brief | 3. Working Code/Prototype: Build a chat agent that allows users to ask data related questions naturally, discuss about it and create a report with action items when asked to. | PROTO | src/cli/commands.py; tests/test_local_e2e.py; scenarios/report-first-quarter.yaml | | | |
| D3b | brief | The prototype needs to support all of these following requirements | PROTO | docs/PRD.md | | | umbrella for D3d to D3g; MET only if R2, R3, R5 and R7 are all satisfied |
| D3c | brief | (defined above, rest of the requirements are for design only): | DELIV | docs/technical-explanation.md | | | R1, R4, R6, R8 each designed in the documents |
| D3d | brief | 1. Safety & PII Masking | PROTO | | | | mirrors R2 |
| D3e | brief | 2. High-Stakes Oversight | PROTO | | | | mirrors R3 |
| D3f | brief | 3. Resilience & Graceful Error Handling | PROTO | | | | mirrors R5 |
| D3g | brief | 4. Observability | PROTO | | | | mirrors R7 |
| QA3c | qa | The four requirements that do need to be prototyped are: Safety & PII Masking, High-Stakes Oversight, Resilience & Graceful Error Handling, and Observability. | PROTO | | | | mirrors R2, R3, R5, R7 |
| D4 | brief | 4. CLI-based interface for chat interactions. | PROTO | src/cli/main.py; src/cli/commands.py; tests/test_cli.py | | | |
| D5a | brief | 5. Your solution must be runnable on another machine | DELIV | requirements.txt; .env.example; README.md; tests/test_cli_imports.py | | | check that requirements.txt covers every import; no machine-specific paths |
| D5b | brief | (Docker is not a must, just proper setup instructions). | DELIV | README.md; docs/local-testing.md | | | |
| D6a | brief | 6. Use a framework of your choice | PROTO | src/agent/graph.py; requirements.txt | | | |
| D6b | brief | explain why you choose it | DELIV | docs/technical-explanation.md | | | |
| D6c | brief | and your level of experience with it. | DELIV | docs/technical-explanation.md | | | the statement is the author's own words; never accept one written by the agent |

## Dataset, capabilities and setup (brief)

| ID | Src | Quote | Class | Look | Verdict | Evidence | Note |
|---|---|---|---|---|---|---|---|
| DS1 | brief | Dataset Specification: * Dataset: bigquery-public-data.thelook_ecommerce | PROTO | config.yaml; src/data_access/catalog.py; schema_allowlist.yaml; tests/test_catalog.py | | | |
| DS2 | brief | Required Tables: * orders - Customer order information | PROTO | schema_allowlist.yaml; tests/test_allowlist.py | | | |
| DS3 | brief | order_items - Individual items within orders | PROTO | schema_allowlist.yaml; tests/test_allowlist.py | | | |
| DS4 | brief | products - Product catalog and details | PROTO | schema_allowlist.yaml; tests/test_allowlist.py | | | |
| DS5 | brief | users - Customer demographics and information | PROTO | schema_allowlist.yaml; tests/test_allowlist.py | | | |
| CAP0 | brief | Expected Agent Capabilities: Your prototype chat agent should be able to perform data analysis and generate insightful reports such as: | PROTO | src/agent/nodes/composer.py; scenarios/; eval/datasets/golden_cases.yaml | | | |
| CAP1 | brief | Customer behavior (e.g., top customers, total spend) | PROTO | scenarios/privacy-top-customers.yaml; eval/datasets/golden_cases.yaml; tests/test_result_guard.py | | | |
| CAP2a | brief | Product performance (e.g compare performance of product X and Y | PROTO | eval/datasets/golden_cases.yaml; src/agent/nodes/planner.py | | | |
| CAP2b | brief | and why do they perform differently) | PROTO | scenarios/why-casa-lino-returns.yaml; src/agent/nodes/planner.py; src/agent/nodes/evidence.py | | | |
| CAP3a | brief | Time-based metrics (e.g., monthly revenue, | PROTO | scenarios/smoke-revenue-by-brand.yaml; eval/datasets/golden_cases.yaml | | | |
| CAP3b | brief | up-to-date revenue by product) | PROTO | eval/datasets/golden_cases.yaml; golden/seed/ | | | the data has a latest date; "up-to-date" must not read a stale report |
| CAP4 | brief | Answer questions about the general structure of the database (what data is available, what can we do with it) | PROTO | src/agent/nodes/schema_answer.py; scenarios/schema-question.yaml | | | |
| CAP5 | brief | Multi step queries (as described above) | PROTO | src/agent/nodes/planner.py; src/agent/nodes/sql_loop.py; tests/test_graph_planner.py | | | |
| BQ1 | brief | 1. Use BigQuery integration to query and analyze the specified tables. | PROTO | src/data_access/safe_runner.py; src/data_access/bq_client.py; tests/test_live_bigquery.py | | | live proof needs a Google sign-in; do not run pytest -m live |
| BQ2 | brief | Your agent should be able to construct and execute SQL queries dynamically based on the analysis requirements. | PROTO | src/agent/nodes/sql_loop.py; src/guards/sql_guard.py; tests/test_graph_loop.py | | | |
| LLM1 | brief | 2. You should preferably use one of the newer Google Gemini models. | PROTO | config.yaml; src/llm/routing.py; src/llm/gateway.py; tests/test_gateway.py | | | model IDs against models.list() are a live step |
| LLM3 | brief | Please be mindful of the rate limits. | PROTO | src/llm/limiter.py; src/llm/budget.py; config.yaml; tests/test_limiter.py | | | |
| SET1 | brief | Setup Instructions Environment Setup 1. Install Python dependencies: pip install -r requirements.txt | DELIV | requirements.txt; README.md | | | |
| SET2 | brief | BigQuery Setup 1. Set up BigQuery access by following the BigQuery Client Libraries documentation if you don't already have BigQuery access configured. | DELIV | README.md; docs/local-testing.md | | | |
| SET3 | brief | 2. Free Tier: Google Cloud provides 1TB of free BigQuery compute per month, which is more than sufficient for this challenge. | PROTO | config.yaml; src/data_access/safe_runner.py; src/observability/metrics.py | | | the byte cap keeps use inside the free tier (NFR7) |
| SET4 | brief | 3. Authentication: Ensure your environment is authenticated with Google Cloud to access the public datasets. | DELIV | README.md; src/cli/doctor.py; tests/test_doctor.py | | | |
| SUB1 | brief | Submission Share with us a your public GitHub repository with: | DELIV | README.md | | | git remote -v, and gh repo view for visibility if the CLI is signed in; do not push |
| SUB2 | brief | Documentation * Source code * Architecture diagram | DELIV | docs/; src/; docs/architecture.md | | | all three must be committed, not only present |

## decision.md (every NFR, assumption, risk, decision, prototype stand-in and answered question)

Open questions are not expectations and have no row. Rows were made with `check_ledger.py --init-decision`; the classes are a first guess, change one when the document says otherwise.

| ID | Src | Quote | Class | Look | Verdict | Evidence | Note |
|---|---|---|---|---|---|---|---|
| DM-3.1 | decision | Agent framework | PROTO |  |  |  |  |
| DM-3.2 | decision | AI model choice and routing | PROTO |  |  |  |  |
| DM-3.3 | decision | Golden Bucket: storage, search and updates | DESIGN |  |  |  |  |
| DM-3.4 | decision | Who can see what: access rules and PII | PROTO |  |  |  |  |
| DM-3.5 | decision | SQL safety and checks | PROTO |  |  |  |  |
| DM-3.6 | decision | Destructive actions and user confirmation | PROTO |  |  |  |  |
| DM-3.7 | decision | Learning from each user: out of scope | OOS | docs/decision.md; docs/technical-explanation.md; src/ |  |  |  |
| DM-3.8 | decision | Learning for the whole system: feedback, review, failure mining | DESIGN |  |  |  |  |
| DM-3.9 | decision | Persona: one static file | PROTO | personas/default.yaml; src/personas.py; tests/test_personas.py |  |  |  |
| DM-3.10 | decision | Agent loop: limits, saved state, duplicate protection | PROTO |  |  |  |  |
| DM-3.11 | decision | Resilience and cost control | PROTO |  |  |  |  |
| DM-3.12 | decision | Observability, monitoring and logging | PROTO |  |  |  |  |
| DM-3.13 | decision | Quality checks and evaluation | DESIGN |  |  |  |  |
| DM-3.14 | decision | Extensibility (tool registry) | PROTO |  |  |  |  |
| DM-3.15 | decision | Caching | PROTO |  |  |  |  |
| NFR1 | decision | Response time, simple question | PROTO | eval/baseline.json; docs/technical-explanation.md |  |  |  |
| NFR2 | decision | Response time, multi-step question | PROTO |  |  |  |  |
| NFR3 | decision | Time limit per turn | PROTO |  |  |  |  |
| NFR4 | decision | AI calls per turn | PROTO |  |  |  |  |
| NFR5 | decision | SQL attempts per sub-question | PROTO |  |  |  |  |
| NFR6 | decision | Bytes billed per query | PROTO | config.yaml; src/data_access/safe_runner.py |  |  |  |
| NFR7 | decision | BigQuery monthly use | PROTO | config.yaml; src/observability/metrics.py |  |  |  |
| NFR8 | decision | Daily budget per user | PROTO |  |  |  |  |
| NFR9 | decision | Gemini rate limits | PROTO |  |  |  |  |
| NFR10 | decision | Scale and availability | DESIGN |  |  |  |  |
| NFR11 | decision | PII in final output | PROTO | src/guards/output_guard.py; tests/test_output_guard.py |  |  |  |
| NFR12 | decision | Attack test pass rate | PROTO |  |  |  |  |
| NFR13 | decision | Smallest group shown | PROTO | config.yaml; src/guards/result_guard.py; tests/test_result_guard.py |  |  |  |
| NFR14 | decision | Rows sent to the model | PROTO |  |  |  |  |
| NFR15 | decision | Access rules store down | PROTO |  |  |  |  |
| NFR16 | decision | The model is never the only checker | PROTO |  |  |  |  |
| NFR17 | decision | Delete confirmation | PROTO |  |  |  |  |
| NFR18 | decision | No duplicate effects on retry | PROTO |  |  |  |  |
| NFR19 | decision | Interface never crashes | PROTO |  |  |  |  |
| NFR20 | decision | Alerts | PROTO |  |  |  |  |
| NFR21 | decision | How long we keep data | PROTO | src/storage/db.py; src/agent/checkpointer.py; tests/test_storage.py |  |  |  |
| NFR22 | decision | Traces and logs kept | CTX |  |  |  | decision.md itself says [NOT SPECIFIED]: there is no target to check |
| NFR23 | decision | Persona change | OOS |  |  |  |  |
| NFR24 | decision | New tool | PROTO |  |  |  |  |
| NFR25 | decision | Release safety | DESIGN |  |  |  |  |
| NFR26 | decision | Answer quality | PROTO |  |  |  |  |
| NFR27 | decision | Usability score | DESIGN |  |  |  |  |
| NFR28 | decision | Runs on another machine | DELIV |  |  |  |  |
| NFR29 | decision | Who sees which data | PROTO |  |  |  |  |
| A1 | decision | Confirmed: scope is the user's brands. Assumed: the filter applies per item in mixed orders. | PROTO |  |  |  |  |
| A2 | decision | The SQL Guard blocks direct identifiers. Other attributes appear only in groups of 10 or more. | PROTO |  |  |  |  |
| A3 | decision | A fake ID per session (for example `Customer #48213`) is not PII. Real `users.id` never leaves the Result Guard. Owner decision. | PROTO |  |  |  |  |
| A4 | decision | The frontend sends a signed token with the user's brands. Roles beyond manager and CEO are Phase1 defaults. | DESIGN |  |  |  |  |
| A5 | decision | Churn is no order in 90 days. "Last month" is the calendar month. | PROTO |  |  |  |  |
| A6 | decision | Users may delete only their own reports. No legal hold. | PROTO |  |  |  |  |
| A7 | decision | Production uses Vertex AI in the US | DESIGN |  |  |  |  |
| A8 | decision | About 50 users and 99.5% availability | DESIGN |  |  |  |  |
| A9 | decision | Production trios: one per line, up to about 10k, in English. Analysts review a few dozen weekly. | DESIGN |  |  |  |  |
| A10 | decision | Withdrawn (owner decision, 2026-10-22): there is no persona editing or approval flow. | OOS |  |  |  |  |
| A11 | decision | Transcripts 90 days, audit log 1 year | PROTO |  |  |  |  |
| A12 | decision | Withdrawn (owner decision, 2026-10-22): there is no per-user "My style". | OOS |  |  |  |  |
| A13 | decision | The dataset is synthetic, so free-tier Gemini terms are fine | PROTO |  |  |  |  |
| A14 | decision | The listed Gemini model IDs exist at build time | PROTO |  |  |  |  |
| A15 | decision | Confirmed: the Golden Bucket does not exist. We seed a small set of our own trios. | PROTO |  |  |  |  |
| A16 | decision | Questions and answers are in English only | PROTO |  |  |  |  |
| A17 | decision | Currency is USD and time zone is UTC | PROTO |  |  |  |  |
| A18 | decision | The reviewer has a Google Cloud project and a Gemini key | DELIV |  |  |  |  |
| A19 | decision | One user at a time on one machine in the prototype | PROTO |  |  |  |  |
| A20 | decision | Saved reports hold no PII and record their conversation ID | PROTO |  |  |  |  |
| A21 | decision | The dataset has no free-text cells with PII | PROTO |  |  |  |  |
| A22 | decision | Another team owns data governance, ingestion and data modeling | OOS |  |  |  |  |
| A23 | decision | Every order item joins to `products.brand`, so the brand filter covers all data | PROTO |  |  |  |  |
| A24 | decision | Brand lists are small and change rarely, so a config file is enough | PROTO |  |  |  |  |
| P8-1 | decision | Cloud Run, login token with the user's brands | PROTO |  |  |  |  |
| P8-2 | decision | LangGraph on Cloud Run | PROTO |  |  |  |  |
| P8-3 | decision | Cloud SQL Postgres (checkpoints, reports, audit) | PROTO |  |  |  |  |
| P8-4 | decision | Golden Index, Cloud Storage bucket, ingest job, glossary | PROTO |  |  |  |  |
| P8-5 | decision | BigQuery authorized views and policy tags | PROTO |  |  |  |  |
| P8-6 | decision | Cloud Sensitive Data Protection, Model Armor | PROTO |  |  |  |  |
| P8-7 | decision | Vertex AI fallback | PROTO |  |  |  |  |
| P8-8 | decision | OpenTelemetry to Langfuse and Cloud Trace; Pub/Sub metrics to BigQuery | PROTO |  |  |  |  |
| P8-9 | decision | Cloud Tasks background workers | PROTO |  |  |  |  |
| P8-10 | decision | Idempotency keys, failed-job queues | PROTO |  |  |  |  |
| P8-11 | decision | Bounded replan | PROTO |  |  |  |  |
| P8-12 | decision | Eval pipeline | PROTO |  |  |  |  |
| P8-13 | decision | Charts, email, web search | DESIGN |  |  |  |  |
| S1 | decision | `products.brand` is free text. A typo in the brand mapping gives an empty scope without warning. | PROTO |  |  |  |  |
| S2 | decision | `bq_client.py` has no dry-run, byte cap, timeout, labels or row cap. | PROTO |  |  |  |  |
| S3 | decision | About 8 calls per turn cannot cover up to 12 SQL calls plus other steps. | PROTO |  |  |  |  |
| S4 | decision | The full design does not fit 6-12 hours. | CTX |  |  |  | scope advice about the time box; the four prototype areas are the R2, R3, R5 and R7 rows |
| S5 | decision | Search filters trios by table, not by brand. A report excerpt can show other brands' numbers. | PROTO |  |  |  |  |
| S6 | decision | `bq_client.py` logs the full error text. Errors and traces can hold data values. | PROTO |  |  |  |  |
| S7 | decision | "Top customers" returns groups of one. k = 10 would hide them all. | PROTO |  |  |  |  |
| S8 | decision | Saved reports have no conversation ID. "Delete reports from this conversation" cannot work. | PROTO |  |  |  |  |
| S9 | decision | Streaming text before the Output Guard runs could show PII. | PROTO |  |  |  |  |
| S10 | decision | Nobody says what happens if the Input Guard fails. | PROTO |  |  |  |  |
| S11 | decision | Nobody says what happens when the numeric check fails. | PROTO |  |  |  |  |
| S12 | decision | Parallel sub-queries plus retries can cause a burst of 429 errors. | PROTO |  |  |  |  |
| S13 | decision | The AI judge uses a preview Pro model. Its scores can shift. | PROTO |  |  |  |  |
| S14 | decision | If code reads an empty brand list as "all brands", a missing mapping shows everything. | PROTO | src/access/users.py; tests/test_users.py |  |  |  |
| S15 | decision | A customer buys from many brands. A manager sees only part of that customer's spend. | PROTO |  |  |  |  |
| Q1 | decision | **Access:** What does "products related to him" mean without an owner column? Does scope apply per item? | PROTO |  |  |  |  |
| Q2 | decision | **Access:** How do users log in, in the CLI and in production? | PROTO |  |  |  |  |
| Q8 | decision | **Framework:** Is LangGraph fine? What is the author's experience level? | DELIV |  |  |  |  |
| Q9 | decision | **Golden Bucket:** Does it exist? What format? Do we fake it and search it in the prototype? | PROTO |  |  |  |  |
| Q12 | decision | **PII:** Do we show "Customer #id" or a new fake ID per session? May per-customer rows reach the model? | PROTO |  |  |  |  |
| Q23 | decision | **Loop:** Do we add the turn ID to the idempotency key? | PROTO |  |  |  |  |
| Q24 | decision | **Memory:** Do we build a memory agent? | OOS |  |  |  |  |
| Q26 | decision | **Persona:** How do versions, rollback and editing work? | OOS |  |  |  |  |
| Q38 | decision | **Memory:** Are the promotion numbers right? | OOS |  |  |  |  |
