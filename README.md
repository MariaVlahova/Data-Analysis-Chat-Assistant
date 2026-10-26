# Data Analysis Chat Assistant

A chat agent for non-technical retail executives. It answers business questions about the `bigquery-public-data.thelook_ecommerce` dataset (`orders`, `order_items`, `products`, `users`) in plain English: totals, rankings, trends, comparisons, causes ("why did revenue drop?") and reports with action items.

- Each user is a brand manager and sees only the data of their own brands. The CEO sees all brands.
- Personal data never appears in an answer. Code enforces this, not the model: the model only suggests, and every suggestion is checked.
- Saved reports can be listed, deleted and restored. A delete shows exactly what it found and waits for one typed confirmation.
- Failures end in an honest message and a trace ID. The chat does not crash.

It is a command-line prototype built on [LangGraph](https://github.com/langchain-ai/langgraph), written for the OpsFleet design challenge.

| Read | For |
|---|---|
| [docs/architecture.md](docs/architecture.md) | The Mermaid diagrams, the service table, how the components talk (deliverable D1) |
| [docs/technical-explanation.md](docs/technical-explanation.md) | Why each choice, the data flow, storage, error handling and fallbacks, and how each of the eight requirements is solved (D2, D6) |
| [docs/PRD.md](docs/PRD.md) and [docs/decision.md](docs/decision.md) | Requirements, assumptions, risks and open questions, with the status of each |
| [docs/plans/](docs/plans/) | The eleven build plans, with what is done and what is not |
| [docs/local-testing.md](docs/local-testing.md) | Running everything on one machine, the scenarios, the evaluation |

## What is real and what is design-only

The client asked the prototype to cover four of the eight requirements [DECIDED]. The rest are design.

| Requirement of the brief | In this repository |
|---|---|
| 1. Hybrid Intelligence (Golden Bucket) | **Design.** An optional small slice is built: 12 seed trios, a glossary and an in-memory search (off in `config.yaml`, on in `config.local.yaml`). |
| 2. Safety and PII masking | **Built.** SQL Guard, Result Guard, Output Guard, brand scope per user. |
| 3. High-Stakes Oversight | **Built.** Find, preview, one typed confirmation, soft delete, undo, audit log. |
| 4.1 User memory | **Out of scope** (owner decision). |
| 4.2 System learning | **Design.** |
| 5. Resilience and graceful errors | **Built.** Typed errors, repair loop, retries, circuit breaker, fallback model, `--chaos`. |
| 6. Quality assurance | **Design**, with a stand-in: a pytest suite, 16 golden cases, 27 attacks, chaos runs, an advisory AI judge. |
| 7. Observability | **Built** locally: trace IDs, spans, a metrics row per turn, `/trace`, `/metrics`. The production stack is design. |
| 8. Persona management | **Out of scope** (owner decision). One static file, `personas/default.yaml`. A tone change is a code change, so this requirement of the brief is not met. |

### What has been run, and what has not

Read this before you trust a sentence in this repository.

- **Tested offline:** 4,004 tests pass with no network and no keys (`python -m pytest`, about 10 minutes). 69 more are marked `live`: they need Google credentials or a Gemini key, skip without them, and have not been run against Google.
- **Run end to end on a local model and generated sample data:** yes. The example run below is real output.
- **Run on real BigQuery, with the local model answering:** yes. On 2026-10-24 and 25 the app ran against the real dataset (`config.bigquery.yaml`): 61 turns, about 48 dry runs and queries, under 1 GB billed. `doctor` reads the live schema and finds that every allowed column exists and every live column is classified. The six brands of `users.bigquery.yaml` match the real brand list (2,756 brands). BigQuery rejected none of the generated SQL at the dry run. The full evaluation ran this way: 9 of 16 golden cases, 25 of 25 attacks, the chaos run, and every hard gate held.
- **Run on Gemini:** **no, because the author's free Gemini quota ran out.** The brief asks for a Gemini model, and Gemini is the default: `config.yaml` and every environment except `local` use it. With no quota left, the live runs used a local model through the same gateway, graph and guards. Switching to Gemini is a setting, not a code change: put `GEMINI_API_KEY` in `.env` and run with `config.yaml`, or set `OPSFLEET_ENV=default` on any config (Paths C and D). The Gemini client is built and tested against a scripted client. The model IDs, a real structured call and the rate limits have not been checked against a live key; `doctor --live` checks the first two on yours.
- **Not signed off by a person:** `schema_allowlist.yaml` still says `reviewed_against_live: false`. No one has agreed the PII split (Q11) or the customer-row rule (S7), and no one has tried to break `src/guards/`. With the default config (`config.yaml`) the chat **refuses to start** until the flag is set (see Path D). `config.bigquery.yaml` starts anyway and writes a warning to the log (not to the screen).
- The plans' "Still to do" sections were written before the BigQuery runs and are not updated. [docs/technical-explanation.md](docs/technical-explanation.md) section 9 has the current list of live steps.

## Prerequisites

- **Python 3.11 or newer.** The requirements were installed into a fresh virtual environment and the app was run on Python 3.14.6 on Windows 10. No other Python version or operating system has been tried. If a package has no wheel for your Python, use 3.12.
- **Git.** No Docker file is provided.
- For a first look (Path A below): [LM Studio](https://lmstudio.ai/), or any OpenAI-compatible model server, and a model that can return JSON. The tested model is `qwen/qwen3-30b-a3b-2507`, which needs a machine with plenty of memory and writes about 10 tokens a second on a laptop.
- For Gemini (Paths C and D): a **Gemini API key** from [Google AI Studio](https://aistudio.google.com/).
- For real BigQuery (Paths B and D): a **Google Cloud project** (BigQuery jobs run, and are billed, in it) and the [`gcloud` CLI](https://cloud.google.com/sdk/docs/install). The free tier is 1 TiB of queries a month, and the app caps every query.

## Setup

Common to every path:

```bash
git clone https://github.com/MariaVlahova/Data-Analysis-Chat-Assistant.git
cd Data-Analysis-Chat-Assistant

# 1. Create and activate a virtual environment
python -m venv venv
source venv/bin/activate            # Windows PowerShell: venv\Scripts\Activate.ps1

# 2. Install everything (one file, the tests included)
pip install -r requirements.txt
```

On Windows, if the console shows `?` or a plain `+---+` box instead of lines, set `PYTHONUTF8=1` first (PowerShell: `$env:PYTHONUTF8 = "1"`).

There are four ways to run the app. Pick one.

| Path | Data | Model | Needs | Status |
|---|---|---|---|---|
| **A. Local** | Generated sample data (DuckDB) | A model on your machine through an OpenAI-compatible server | LM Studio. No Google sign-in, no key, no cost. | **Verified end to end.** The example run comes from here. |
| **B. Local model, real BigQuery** | `bigquery-public-data.thelook_ecommerce` | A model on your machine | LM Studio, and a Google Cloud project | **Verified**: the evaluation of 2026-10-25 ran this way |
| **C. Gemini on sample data** | Generated sample data | Gemini | A Gemini key. No Google sign-in. | Built, **not run on a live key** (the author's free quota ran out). Path A with `OPSFLEET_ENV=default`. |
| **D. Gemini on real BigQuery** | `bigquery-public-data.thelook_ecommerce` | Gemini | A Gemini key and a Google Cloud project | Built, **not run with Gemini** (the author's free quota ran out). The default config. Two manual steps are needed first. |

### Path A: local model, sample data

```bash
# 3. Build the sample data (about a second; the same --seed gives the same rows)
python -m src.cli.main local-data build --config config.local.yaml
python -m src.cli.main local-data seed-reports --config config.local.yaml   # a few saved reports, to try delete and undo

# 4. In LM Studio: load qwen/qwen3-30b-a3b-2507 and start the local server (http://localhost:1234/v1)

# 5. Check the setup. It prints one line per check and the fix for each failure.
python -m src.cli.main doctor --live --config config.local.yaml

# 6. Chat
python -m src.cli.main chat --user alice --config config.local.yaml
```

To avoid typing `--config` each time: `export OPSFLEET_CONFIG=config.local.yaml` (PowerShell: `$env:OPSFLEET_CONFIG = "config.local.yaml"`).

The banner says `LOCAL SAMPLE DATA` and `LOCAL MODEL`, so a local run is never mistaken for a live one. The sample data is invented: brands, people and e-mails do not exist anywhere. It shows that the guarded SQL means what it should on real tables. It does **not** show that BigQuery accepts the SQL, how Gemini behaves, or what the real schema holds ([docs/local-testing.md](docs/local-testing.md) section 6). A 30B model on a laptop takes minutes per question, so `config.local.yaml` raises the time limits. To use another model or server, change `llm.environments.local` (`model`, `base_url`).

### Path B: local model, real BigQuery

```bash
# 3. Sign in to Google Cloud for BigQuery (application default credentials)
gcloud auth application-default login

# 4. Name your Google Cloud project (BigQuery jobs run, and are billed, in it)
cp .env.example .env                # Windows: copy .env.example .env
#    GOOGLE_CLOUD_PROJECT=your-project-id

# 5. Check the setup. This reads the live schema and the real brand list (free metadata calls and three small queries).
python -m src.cli.main doctor --refresh --config config.bigquery.yaml

# 6. In LM Studio: load qwen/qwen3-30b-a3b-2507 and start the local server, then chat
python -m src.cli.main chat --user alice --config config.bigquery.yaml
```

`users.bigquery.yaml` ships with real brand names that were read from the dataset on 2026-10-24 (the busiest brands by item count). To write it again from the live brand list: `python -m src.cli.main bigquery init-users --config config.bigquery.yaml --force`. `config.bigquery.yaml` sets `guards.require_reviewed_allowlist: false`, so the chat starts and writes a warning to the log (not to the screen) that the allow-list has not been signed off. Every query goes through a dry run first and is refused above 512 MiB, so a mistake cannot cost more than that per query. `pytest -m live` runs free dry runs and three small real queries; we have not run it. The steps in order are in [docs/local-testing.md](docs/local-testing.md) section 7.

### Path C: Gemini on the sample data

```bash
cp .env.example .env                # Windows: copy .env.example .env
#   GEMINI_API_KEY=...
python -m src.cli.main local-data build --config config.local.yaml
OPSFLEET_ENV=default python -m src.cli.main doctor --live --config config.local.yaml   # PowerShell: $env:OPSFLEET_ENV = "default"
OPSFLEET_ENV=default python -m src.cli.main chat --user alice --config config.local.yaml
```

`OPSFLEET_ENV` overrides the environment of the config, so the sample data is read with Gemini answering. `doctor --live` compares the model IDs in `config.yaml` with `models.list()` for your key and makes one small call. If an ID is missing it prints the fix. The free tier is rate-limited: set `llm.default_rpm` from your AI Studio limits page, or a "why" turn will wait on the limiter.

### Path D: Gemini on real BigQuery

```bash
# 3. Sign in to Google Cloud for BigQuery (application default credentials)
gcloud auth application-default login

# 4. Secrets: copy the template and fill it in. Real environment variables win over .env.
cp .env.example .env                # Windows: copy .env.example .env
#    GEMINI_API_KEY=...
#    GOOGLE_CLOUD_PROJECT=your-project-id

# 5. Check the setup. It prints one line per check and the fix for each failure.
python -m src.cli.main doctor --refresh
python -m src.cli.main doctor --live          # also checks the Gemini model IDs and makes one small call
```

Then two steps only a person can do. They are why the chat refuses to start out of the box:

1. **Real brands.** `users.yaml` holds placeholders (`<verified brand N>`). Copy the exact names from `users.bigquery.yaml` (case and spacing count), or run `python -m src.cli.main bigquery init-users --config config.yaml --force` to write them from the live brand list. The Brand mapping check of `doctor` also lists the busiest brands.
2. **Review the allow-list.** `doctor` compares `schema_allowlist.yaml` with the live columns. Read it, agree the PII split with the owner (Q11), then set `reviewed_against_live: true` in that file. Until then the chat refuses to start. (`guards.require_reviewed_allowlist: false` in `config.yaml` starts it with a warning in the log instead.)

```bash
# 6. Chat
python -m src.cli.main chat --user alice
```

Secrets never go into `config.yaml`. A mistyped key in `config.yaml` stops startup on purpose and names the file, line and key.

`pytest -m live` then runs the free dry runs and three small real queries against BigQuery, as a formal check that BigQuery accepts the SQL the guard writes (the evaluation of 2026-10-25 already sent about 48 of them through the dry run, with none rejected).

## Using the chat

```bash
python -m src.cli.main chat --user alice            # a brand manager
python -m src.cli.main chat --user ceo              # sees all brands
python -m src.cli.main chat --user alice --debug    # log lines and tracebacks on screen
python -m src.cli.main chat --user alice --debug-prompts   # keep the message exchange of each turn (masked, no result rows): /trace <id> --exchange shows it
python -m src.cli.main chat --user alice --conversation conv-1a2b3c4d   # continue a conversation
python -m src.cli.main chat --user alice --chaos harsh --chaos-seed 7   # inject faults on purpose; the same seed replays the run
```
(Add `--config config.local.yaml` for Paths A and C, and `--config config.bigquery.yaml` for Path B.)

What you can ask: questions about sales, products and customers (a total, a ranking, a trend), "why" and comparison questions that need several queries, reports with insights and action items, and what data is available. Customers appear only as labels such as `Customer #48213`, never by name. You can also say "save that", "list my reports", "delete the reports mentioning X", "delete all the reports we made in this conversation" and "undo". A delete shows what it found and waits for your next line: `y` or `yes` for one report, `yes` for a short list, and `DELETE <n>` for "all" or ten or more. Anything else cancels. Every answer starts with how your question was read and which brands it covers, and ends with notes about anything that was hidden, cut or could not be answered.

Inside the chat: `/help`, `/quit`, `/new` (new conversation), `/trace [id] [--raw]` (the steps of a turn, by default the last one), `/trace [id] --exchange` (what was sent to the model and what came back, step by step; only for a chat started with `--debug-prompts`), `/trace --conversation` (the turns of this conversation, each with its question) and `/metrics [--last N]` (totals and rates). You see your own turns; the CEO sees everyone's. Every error message ends with a trace ID. Outside the chat, the operator views (all users) are `python -m src.cli.main trace [ID] [--exchange]`, `python -m src.cli.main trace --conversation ID` and `python -m src.cli.main metrics [--last N]`.

Two more commands, for whoever runs the prototype:

- `/eval [last|all]` judges the answers of the conversation you are in with the six rubrics of `evalkit/rubrics.xlsx`, and shows a score and a reason for each. **The chat never does this by itself**: a question makes no judge call, and nothing is judged until you type `/eval` (the command is available because `evaluation.enabled` is `true` in `config.yaml`; `false` switches it off). `/eval` judges the turns not judged yet, `/eval last` the newest again, `/eval all` every turn again, and Ctrl+C stops it after the verdict in flight, keeping what was judged. The judge spends model tokens of its own, and a score is advisory. When the judge is the model that answered, the screen says NOT INDEPENDENT (`evaluation.allow_self_judge: false` refuses instead). Each judged turn adds one row to `data/evaluations.jsonl`: ids, scores, models and tokens, no text.
- `/budget` shows the tokens you used today (UTC), for the chat and for `/eval` apart, and the last turn. `budget.daily_tokens_per_user` in `config.yaml` is a cap on that total; it is `null` (no cap) until the client names an amount (Q15). With a cap, a question is refused once the day's use has reached it, and one warning appears when a turn takes the use past `budget.alert_fraction` (80%). `/budget clear` starts today's count again; the tokens stay in the records and the clear is written to the audit log. The operator form is `python -m src.cli.main budget --user alice [--clear]`.

Tokens are logged everywhere: each model call is a `model call step=... tokens_in=... tokens_out=...` line in `data/logs/app.log` (the judge's calls too, with `step=judge`), each turn a `turn done ...` line and a row in `data/runs.jsonl`, and the suite runs (`python -m eval.run_golden`, `run_attacks`, `run_chaos`) print and save the calls and tokens per case and for the run.

`--chaos mild|harsh` injects model errors (429, 503, timeouts, unusable JSON) into about 15% (mild) or 40% (harsh) of model calls, BigQuery errors into fewer BigQuery calls, and now and then a broken SQL query, to show that retries, the circuit breaker and the fallback model keep the chat alive. Without `--chaos-seed` a seed is drawn and printed in the banner. Injected faults replace the call, so they use no quota.

Traces, logs and metrics are masked on a **best-effort** basis. A pattern scan replaces e-mails, phone numbers, US social security and employer tax IDs, and card numbers before anything is written. It does **not** find names, street addresses or a bare customer ID, so the design also keeps result rows, DataFrames, cell values and exception messages out of them, and people appear only as a hash. The question you type is stored (masked) in the trace. Prompts are not stored unless you start the chat with `--debug-prompts`. Then each step's fixed instructions go into the trace (masked), and the full message exchange of the turn goes to `data/traces/<id>.exchange.jsonl`: for each model attempt the instructions, the contents sent, the reply, the tokens and the error kind, masked. In the contents, **each query result is replaced by one line that names it** (`[rows of q1: 12 rows, columns brand, revenue; not stored]`), so no row is written; the Composer's draft is never written, and the answer is, only after the Output Guard has passed it (an answer it blocks is stored nowhere). Look at it with `/trace <id> --exchange`. Trace files and exchange files are deleted after 90 days (`retention.trace_days`); `runs.jsonl` rows hold counts and IDs only and are kept. The audit log is kept one year (`retention.audit_days`, 365 at least) and then purged at startup.

The answer tone comes from one static persona file, `personas/default.yaml`. There is no persona editor, no per-user style and no switching.

Exit codes: `0` normal, `2` a startup problem (bad config, unknown user, no brands, database problem), `1` a failed `doctor` check, a `trace` ID that was not found, a failed `scenario`, `golden validate` or evaluation run, or an unexpected crash at startup.

### Users and brands

| Config | Users |
|---|---|
| `users.local.yaml` (Paths A and C, the sample data) | `alice` (Maple & Co, Nordic Peak), `bob` (O'Hara Outfitters), `carol` (Wren & Willow), `dave` (Zephyr Sport, Casa Lino), `ceo` (all brands), `nobrands` (a deliberate test user: the chat refuses to start) |
| `users.bigquery.yaml` (Path B, real BigQuery) | The same names with real brands read from the dataset: `alice` (Allegra K, Calvin Klein), `bob` (Carhartt), `carol` (Hanes), `dave` (Volcom, Quiksilver), `ceo` (all brands), `nobrands` |
| `users.yaml` (Path D, real BigQuery with Gemini) | `alice`, `bob`, `ceo` and `nobrands`, with **placeholder brands** until you write the real ones in (copy them from `users.bigquery.yaml`, which also has `carol` and `dave`) |

- A brand manager sees only the brands listed. Brand names are compared **exactly**: case and spacing count.
- An **empty brand list means no access**, never "all brands". The chat refuses to start for that user.
- Only `role: ceo` with `all_brands: true` sees every brand. (Our proposal; not yet confirmed by the client.)
- The brands of the user who starts the chat are checked against the real brand list (`doctor` checks every user). A typo stops startup with the user and the brand. A list under 24 hours old is used without calling BigQuery. If BigQuery cannot be reached, a saved list up to 7 days old (`data/cache/brands.json`) is used and the chat says so. With no saved list, an older one, or an error that is not an outage (access revoked, wrong project), it refuses to start.

## Example run

Real output, captured on 2026-10-25 from a fresh clone, with the steps of Path A: sample data, and `qwen/qwen3-30b-a3b-2507` served by LM Studio. **This is not Gemini and not BigQuery.** The model is a 30B model on a laptop, so every turn took one to five minutes, and its wording is not what Gemini would write.

**These captures were made before plan 12.** Since then a simple answer ends with its result as a table, built by the code from the guarded rows (the first answer below has none), and a draft that states no value of the data is replaced by the figures. The captures are kept as they were: a new run has not been made on a model.

How it was captured: the chat read its lines from a pipe, which does not echo what is typed, so each typed line is put back after its `Name>` prompt, the way a terminal shows it. Trailing spaces were removed. Nothing else is changed, except two places marked `[trimmed]` or `[banner as above]`. The trace IDs are the real ones: any of them can be opened with `python -m src.cli.main trace <id>` on the machine that made them.

What to notice, and what is weak:

- Every answer starts with how the question was read, and shows its scope (`your brands: ...`).
- The answers are `partial`: "Some figures could not be verified against the data and were left out". The numeric check removed a number that the query results did not back. That is the check working, and it costs the answer some of its figures. In the "why" answer it removed four, so the summary has no number at all.
- The "How I read your question" block of the "why" answer repeats itself (one reading for each sub-question).
- A refusal is not a failure, and the guard that stopped it is named in the trace.
- **The first answer is wrong.** See "Checked against the data" below.

### Checked against the data

The headline figures of four answers were compared with a read-only query on the sample data. That query bypasses the app on purpose: it is the reference, not agent code.

| Answer | What it says | What the data says | |
|---|---|---|---|
| 1. Monthly revenue, 2026 | A total of $88,469.92. The highest month is October at $12,442.05, the lowest January at $5,814.07. | Excluding cancelled and returned items, as the answer says it did: a total of $78,063.73. The highest month is September ($10,814.38), October is $9,950.15, January is $5,310.31. The model's own query returned $88,469.92 in total, and $12,442.05 was **September's** figure. | **Wrong, twice** |
| 3. Top 5 customers | The top spender spent $616.58. The five together spent $2,917.84. | $616.58 and $2,917.84 | Correct |
| 6. Top categories | Outerwear & Coats $35,099.70, Blazers & Jackets $28,092.38. "The lowest being Pants at $9,997.88". | The first two are right. Pants is the tenth of 20 categories, not the lowest. | **Half wrong** |
| 7. Orders last month | 139 | 139 | Correct |

Why answer 1 is wrong: (a) the model wrote `status <> 'cancelled' AND status <> 'returned'` in lower case, and the data spells them `Cancelled` and `Returned`. Nothing was excluded, so the figures are too high. (b) It then put the highest figure on the wrong month: running its query again gives $12,442.05 for September, and the answer calls it October's. The words "excluding cancelled and returned items" are the model's reading, not what its query did. The numeric check cannot catch either: the figures are the query's own results, so they count as grounded, and it does not check which month a figure belongs to. This is the risk that [docs/decision.md](docs/decision.md) section 9.1 names: a wrong but valid query gives a believable number. The "How I read your question" block is the mitigation, and here it shows a reading that the query did not follow. A fix we have not made: give the SQL writer the real values of `status` in its schema slice.

The figures of the "why" answer (4) were not checked, and two of its queries also compare `status` with lower-case `'cancelled'`, so they are suspect for the same reason.

Answer 6 shows the Golden Bucket at work: it found a similar solved question (`g-003`, in the `golden.retrieve` line of that turn's trace, `33f5b497`) and the model's SQL followed it, including the right spelling `'Cancelled', 'Returned'` and its `LIMIT 10`. The `LIMIT 10` is why the tenth category looked like the last one. Model quality is the weak part of this prototype on a 30B local model. The guards, not the model, are what the prototype claims to get right.

### 1. A simple question, then its trace

```text
Hi Alice (brand_manager). You can see brands: Maple & Co, Nordic Peak.
Conversation conv-92997d6a. Type /help for commands, /quit to leave.
LOCAL SAMPLE DATA: these answers come from generated test data in DuckDB, not
from BigQuery.
LOCAL MODEL: qwen/qwen3-30b-a3b-2507 at http://localhost:1234/v1 (environment
local), not Gemini.
Alice> What was monthly revenue for my brands this year?
How I read your question

 • Revenue means the sum of item sale prices, excluding cancelled and returned
   items; 'this year' refers to the period from 2026-01-01 to 2026-10-24, and
   monthly revenue is calculated for each month within this year, with the
   results ordered chronologically.

Scope: your brands: Maple & Co, Nordic Peak

Summary

Monthly revenue for your brands, Maple & Co and Nordic Peak, totaled $88,469.92
from January 1 to October 24, 2026. The highest monthly revenue was $12,442.05
in October, and the lowest was $5,814.07 in January.

Key insights

 • Revenue increased steadily from January to October, with a notable rise in
   October.
 • Revenue fluctuated within a range of $5,814.07 to $12,442.05, indicating
   seasonal or promotional trends.

Action items

 • Review October’s performance to identify drivers behind the peak
   revenue—such as promotions, new product launches, or high-demand periods.
 • Analyze January’s low revenue to assess if it reflects seasonal trends or
   operational issues.
 • Use this monthly pattern to forecast Q4 revenue and plan inventory or
   marketing efforts accordingly.

Notes

 • Some figures could not be verified against the data and were left out.
 • Limited data: this answer rests on only part of the information I tried to
   get.
Alice> /trace
Trace 64f36124  turn t-1  70.58 s  intent=data_question, turn_kind=simple,
outcome=partial
├── question: What was monthly revenue for my brands this year?
├── policy_context  3 ms
├── input_guard  6.95 s
│   ├── llm.call  6.95 s  qwen/qwen3-30b-a3b-2507  tokens 637 in, 51 out
│   │   step=input_guard, attempt=1, fallback_used=False, temperature=0.0
│   └── guard input: pass
├── context_assembly  2 ms
│   └── golden.retrieve  0 ms  no similar solved question
├── sql_loop  28.70 s
│   └── sub_question  28.70 s  id=q1, status=ok, repairs=0, broaden_used=0
│       ├── llm.call  28.44 s  qwen/qwen3-30b-a3b-2507  tokens 975 in, 249 out
│       │   step=sql_write, attempt=1, fallback_used=False, temperature=0.0
│       ├── guard.sql  39 ms  scope_mode=brands,
│       │   fingerprint=f2c21a929fc687e8795cbba99d4d1fcb3ddf3709823d80ab5a5f4ea
│       │   1c4b…, limit=1000, per_customer=False
│       │   └── guard sql: rewrite  rule SCOPE_BRANDS
│       ├── bq.dry_run  168 ms  sql=SELECT FORMAT('%d-%02d', EXTRACT(YEAR FROM
│       │   oi.created_at), …, statement_type=SELECT, bytes_processed=2218028
│       ├── bq.query  54 ms  sql=SELECT FORMAT('%d-%02d', EXTRACT(YEAR FROM
│       │   oi.created_at), …, bytes_estimated=2218028, row_cap=1000, rows=10
│       └── guard.result  1 ms  rows=10, columns_masked=0, cells_masked=0,
│           values_masked=0
│           └── guard result: pass
├── evidence_check  0 ms
├── composer  34.90 s
│   └── llm.call  34.89 s  qwen/qwen3-30b-a3b-2507  tokens 1130 in, 263 out
│       step=composer, attempt=1, fallback_used=False, temperature=0.3
├── numeric_check  1 ms
│   ├── numeric_check  removed=1
│   └── guard numeric: block  rule NUMERIC_UNGROUNDED
├── output_guard  1 ms
│   └── guard.output  1 ms  chars=1293
│       └── guard output: pass
└── finish  0 ms
```

The trace is the whole turn: the Input Guard (one model call), the SQL writer (one call), the SQL Guard's `rewrite` that forced the brand filter (`SCOPE_BRANDS`), the BigQuery dry run and query, the Result Guard, the Composer, and the numeric check that removed a figure (`removed=1`, a `NUMERIC_UNGROUNDED` block). The turn made 3 model calls and 1 query.

### 2. A request for personal data is refused

```text
Alice> List the emails of my top customers.
┌─────────────────────────────── Not possible ────────────────────────────────┐
│ That asks for personal details that could identify a customer, such as who  │
│ they are or how to reach them. I never show those. I can show totals,       │
│ trends and rankings instead.                                                │
│ Trace ID: 538a3178                                                          │
└─────────────────────────────────────────────────────────────────────────────┘
```

The model did write SQL that selects `email`. The SQL Guard refused it before anything reached the data: the trace below has no `bq.dry_run` and no `bq.query`. A guard violation is never retried.

```text
Trace 538a3178  turn t-2  34.97 s  ERROR GUARD_VIOLATION  rule=PII_COLUMN,
intent=data_question, turn_kind=simple, outcome=refused
├── question: List the emails of my top customers.
├── GUARD_VIOLATION (AgentError): every sub-question was stopped; rule
│   PII_COLUMN  rule PII_COLUMN
├── policy_context  3 ms
├── input_guard  6.92 s
│   ├── llm.call  6.92 s  qwen/qwen3-30b-a3b-2507  tokens 830 in, 49 out
│   │   step=input_guard, attempt=1, fallback_used=False, temperature=0.0
│   └── guard input: pass
├── context_assembly  0 ms
│   └── golden.retrieve  0 ms  g-002 (0.2788)
├── sql_loop  28.03 s
│   └── sub_question  28.03 s  id=q1, status=blocked, repairs=0, broaden_used=0
│       ├── llm.call  28.02 s  qwen/qwen3-30b-a3b-2507  tokens 1108 in, 182 out
│       │   step=sql_write, attempt=1, fallback_used=False, temperature=0.0
│       └── guard.sql  3 ms  ERROR GUARD_VIOLATION  scope_mode=brands,
│           rule=PII_COLUMN
│           ├── GUARD_VIOLATION (AgentError): PII_COLUMN: a blocked column name
│           │   (email) is used  rule PII_COLUMN
│           └── guard sql: block  rule PII_COLUMN
├── evidence_check  0 ms
├── compose_failure  0 ms
└── finish  0 ms
```

### 3. Customers appear as session labels

```text
Alice> Who are my top 5 customers by spend?
How I read your question

 • Top customers are ranked by total spend, where spend means the sum of item
   sale prices without cancelled or returned items, using the data from the
   entire available period up to the reference date of 2026-10-24.

Scope: your brands: Maple & Co, Nordic Peak

Summary

 • Your top 5 customers by spend on Maple & Co and Nordic Peak are Customer
   #21129, #26462, #17959, #29960, and #29162.
 • Total spend across these customers is $2,917.84.
 • The highest spender, Customer #21129, spent $616.58.

Key insights

 • All spend is from purchases in your brands only, excluding cancelled or
   returned items.

Action items

 • Reach out to Customer #21129 with a personalized offer to encourage repeat
   purchases.
 • Review the purchase history of the top 5 to identify product preferences and
   tailor future promotions.

Notes

 • Customer numbers are labels for this session only.
 • Each customer's spend counts only purchases in your brands.
 • Some figures could not be verified against the data and were left out.
 • Limited data: this answer rests on only part of the information I tried to
   get.
Alice> /metrics
Metrics: 3 turn(s), your turns only
Outcomes              partial 2, refused 1
Success rate          66.7%
Refusal rate          33.3%
Failure rate          0.0%
Guard blocks          input 0, sql 1, result 0, output 0, numeric 2, hitl 0
PII at output         0
Latency               p50 68.76 s, p95 70.58 s
Model calls per turn  2.67
Tokens                8143 in, 1240 out
Top errors            GUARD_VIOLATION x1
Estimated cost        not set (fill llm.pricing_usd_per_1m in config.yaml)
BigQuery 2026-10      3.6 MiB billed by all users (0.0% of the 1 TiB free tier)
```

`Customer #21129` is a label made for this session only, not a customer ID. The notes say so, and say that the spend counts only the user's brands.

### 4. A "why" question (several queries)

```text
Hi Dave (brand_manager). You can see brands: Zephyr Sport, Casa Lino.
Conversation conv-0c1280b5. Type /help for commands, /quit to leave.
[banner as above: LOCAL SAMPLE DATA and LOCAL MODEL]
Dave> Why did revenue drop for my brands recently?
How I read your question

 • recent is interpreted as the last 90 days (2026-07-27 to 2026-10-24)
 • revenue drop refers to a decline in total revenue compared to the prior
   period, specifically the last 30 days vs. the prior 60 days within the last
   90 days
 • Revenue means the sum of item sale prices for non-cancelled orders; units
   sold means the count of non-cancelled items; returns means the count of
   items with a returned_at timestamp; the last 90 days are from 2026-07-27 to
   2026-10-24, inclusive of the start date and exclusive of the end date.
 • Revenue means the sum of item sale prices for non-cancelled and non-returned
   items; units sold means the count of non-cancelled and non-returned items;
   returns means the count of items with a returned_at timestamp; the last 30
   days are defined as 2026-09-25 to 2026-10-24 based on the provided date r
 • The last 90 days are defined as 2026-07-27 to 2026-10-24; 'average price per
   item' means the average of sale_price for non-cancelled items; 'return rate'
   is the proportion of sold items that were returned, calculated as total
   returns divided by total items sold, with cancelled items excluded.
 • The last 90 days are defined as 2026-07-27 to 2026-10-24; 'number of orders'
   means the count of distinct order IDs, and 'average order value' means the
   average of item sale prices for non-cancelled and non-returned items within
   those dates.
 • Period: 2026-07-27 to 2026-10-24 (last 90 days).

Scope: your brands: Zephyr Sport, Casa Lino

Summary

Revenue for your brands dropped in the last 30 days compared to the prior 60
days.

Key insights

 • Return rates are low for both brands (7.4% for Zephyr Sport, 9.1% for Casa
   Lino), so returns are not driving the decline.
 • Average order value remained stable (Zephyr Sport: $43.95, Casa Lino:
   $58.12), indicating the drop is due to fewer orders and lower sales volume,
   not pricing.

Action items

 • Investigate marketing or sales disruptions for Zephyr Sport, which saw the
   steepest drop.
 • Review inventory or product availability for both brands, as fewer units
   sold may indicate stock issues.
 • Analyze customer behavior trends or seasonal patterns affecting orders in
   the last 30 days.

Notes

 • Some figures could not be verified against the data and were left out.
 • Limited data: this answer rests on only part of the information I tried to
   get.
Dave> /trace
Trace f2a3643b  turn t-1  325.70 s  intent=why_question, turn_kind=why,
outcome=partial
├── question: Why did revenue drop for my brands recently?
├── policy_context  3 ms
├── input_guard  7.42 s
│   ├── llm.call  7.42 s  qwen/qwen3-30b-a3b-2507  tokens 636 in, 50 out
│   │   step=input_guard, attempt=1, fallback_used=False, temperature=0.0
│   └── guard input: pass
├── context_assembly  1 ms
│   └── golden.retrieve  0 ms  no similar solved question
├── planner  102.87 s
│   └── llm.call  102.87 s  qwen/qwen3-30b-a3b-2507  tokens 1263 in, 390 out
│       step=planner, attempt=1, fallback_used=False, temperature=0.0
├── sql_loop  147.13 s
│   ├── sub_question  29.17 s  id=q1, status=ok, repairs=0, broaden_used=0
│   │   ├── llm.call  28.94 s  qwen/qwen3-30b-a3b-2507  tokens 948 in, 225 out
│   │   │   step=sql_write, attempt=1, fallback_used=False, temperature=0.0
│   │   ├── guard.sql  25 ms  scope_mode=brands,
│   │   │   fingerprint=6c501fc508947f984d9cbfc52a366bd030385ffed0e7a6fea1e1568
│   │   │   b435…, limit=1000, per_customer=False
│   │   │   └── guard sql: rewrite  rule SCOPE_BRANDS
│   │   ├── bq.dry_run  161 ms  sql=SELECT p.brand, SUM(oi.sale_price) AS
│   │   │   revenue, SUM(1) AS un…, statement_type=SELECT,
│   │   │   bytes_processed=1580553
│   │   ├── bq.query  44 ms  sql=SELECT p.brand, SUM(oi.sale_price) AS revenue,
│   │   │   SUM(1) AS un…, bytes_estimated=1580553, row_cap=1000, rows=2
│   │   └── guard.result  1 ms  rows=2, columns_masked=0, cells_masked=0,
│   │       values_masked=0
│   │       └── guard result: pass
│   ├── [trimmed: sub-questions q2 and q3 ran the same steps as q1]
│   └── sub_question  33.21 s  id=q4, status=ok, repairs=0, broaden_used=0
│       ├── llm.call  32.98 s  qwen/qwen3-30b-a3b-2507  tokens 962 in, 232 out
│       │   step=sql_write, attempt=1, fallback_used=False, temperature=0.0
│       ├── guard.sql  32 ms  scope_mode=brands,
│       │   fingerprint=2b9664b9adb[CARD_MASKED]d51679942be075950cf428abfcb6496
│       │   a98b…, limit=1000, per_customer=False
│       │   └── guard sql: rewrite  rule SCOPE_BRANDS
│       ├── bq.dry_run  134 ms  sql=SELECT p.brand, COUNT(DISTINCT o.order_id)
│       │   AS number_of_ord…, statement_type=SELECT, bytes_processed=2218028
│       ├── bq.query  54 ms  sql=SELECT p.brand, COUNT(DISTINCT o.order_id) AS
│       │   number_of_ord…, bytes_estimated=2218028, row_cap=1000, rows=2
│       └── guard.result  1 ms  rows=2, columns_masked=0, cells_masked=0,
│           values_masked=0
│           └── guard result: pass
├── evidence_check  8.22 s
│   └── llm.call  8.22 s  qwen/qwen3-30b-a3b-2507  tokens 972 in, 15 out
│       step=evidence_check, attempt=1, fallback_used=False, temperature=0.0
├── composer  60.02 s
│   └── llm.call  60.02 s  qwen/qwen3-30b-a3b-2507  tokens 2342 in, 346 out
│       step=composer, attempt=1, fallback_used=False, temperature=0.3
├── numeric_check  2 ms
│   ├── numeric_check  removed=4
│   └── guard numeric: block  rule NUMERIC_UNGROUNDED
├── output_guard  1 ms
│   └── guard.output  1 ms  chars=2368
│       └── guard output: pass
└── finish  0 ms
```

The Planner split the question into four sub-questions, and the SQL loop ran each one through the SQL Guard, the dry run, the query and the Result Guard. This is `turn_kind=why`: a larger call budget applies. The turn took 326 s on this machine. In the last sub-question the fingerprint shows `[CARD_MASKED]`: the log masker read a run of digits inside the hash as a card number. It over-masks and hides nothing that matters, and we left it as it is.

### 5. Delete: find, preview, one typed confirmation, undo

```text
Hi Alice (brand_manager). You can see brands: Maple & Co, Nordic Peak.
Conversation conv-db5b2548. Type /help for commands, /quit to leave.
[banner as above: LOCAL SAMPLE DATA and LOCAL MODEL]
Alice> Delete all reports mentioning returns.
I found 1 report:

 1 Returns review for dresses (2026-10-25)

Delete this report? (y/N)
Alice> y
Deleted 1 report. Say 'undo' within 30 days to bring it back.
Alice> Undo that delete.
Restored 1 report.
Alice> /quit
Goodbye.
```

The report was found by the user's own search text, shown, and deleted only after the typed `y`. "Undo that delete." brought it back. No SQL ran and no answer was composed: this is the shortcut from the Input Guard to the Tool Router.

### 6. Save, then delete "the reports we made in this conversation"

```text
Hi Alice (brand_manager). You can see brands: Maple & Co, Nordic Peak.
Conversation conv-da07ccf2. Type /help for commands, /quit to leave.
[banner as above: LOCAL SAMPLE DATA and LOCAL MODEL]
Alice> Which product categories earn the most revenue?
How I read your question

 • Revenue means the sum of item sale prices, excluding cancelled and returned
   items; the query uses all available data since no specific date range was
   provided in the question, so it includes all records in the dataset.

Scope: your brands: Maple & Co, Nordic Peak

Summary

 • Outerwear & Coats is the top revenue-generating category with $35,099.70,
   followed by Blazers & Jackets at $28,092.38.

Key insights

 • Outerwear & Coats earns the most revenue, but the data does not include
   units sold, so we cannot determine if this is a price or volume leader.
 • The remaining categories contribute progressively less, with the lowest
   being Pants at $9,997.88.

Action items

 • Focus on maintaining strong performance in Outerwear & Coats and Blazers &
   Jackets.
 • Investigate why these categories lead—analyze pricing, demand, and
   inventory—to identify opportunities to replicate success in other
   categories.
 • Request units sold data to assess whether top categories are driven by high
   prices or high volume.

Notes

 • Some figures could not be verified against the data and were left out.
 • Limited data: this answer rests on only part of the information I tried to
   get.
Alice> Save that report.
Saved "Top Revenue Categories: Outerwear & Coats Leads" on 2026-10-25.
Alice> Delete all the reports we made in this conversation.
I found 1 report:

 1 Top Revenue Categories: Outerwear & Coats Leads (2026-10-25)

Delete this report? (y/N)
Alice> y
Deleted 1 report. Say 'undo' within 30 days to bring it back.
Alice> /quit
Goodbye.
```

The second example of the brief ("Delete all the reports we made in this conversation") needed a saved report made in this conversation, so the run saved one first. Everything after the model's reading of the sentence is code, and the confirmation is one turn.

### 7. A turn under injected faults (`--chaos harsh`)

```text
Hi Alice (brand_manager). You can see brands: Maple & Co, Nordic Peak.
Conversation conv-53795652. Type /help for commands, /quit to leave.
[banner as above: LOCAL SAMPLE DATA and LOCAL MODEL]
Chaos mode harsh is on (seed 3): faults are injected on purpose into model
calls, queries and the SQL the model writes, to show how the agent copes.
Replay a run with --chaos harsh --chaos-seed 3.
Alice> How many orders did my brands get last month?
How I read your question

 • The query counts the number of distinct orders (order_id) from the
   order_items table for items belonging to brands, during last month
   (2026-09-01 to 2026-09-30), excluding cancelled and returned items. The date
   range is based on the reference date of 2026-10-24, with 'last month'
   defined as the prev

Scope: your brands: Maple & Co, Nordic Peak

Summary

Your brands, Maple & Co and Nordic Peak, received 139 orders last month
(2026-09-01 to 2026-09-30), excluding cancelled and returned items. This
reflects activity across all order items tied to your brands during the period.

Key insights

 • Total orders for your brands last month: 139.
 • The count excludes cancelled and returned orders, so this reflects only
   completed transactions.
 • The data covers the full calendar month of September 2026, based on the
   reference date of 2026-10-24.

Action items

 • The category lead should review order trends by product line to identify
   which items drove the 139 orders.
 • The marketing team should assess campaign performance in September to
   determine if promotions influenced order volume.
 • The supply planner should verify inventory levels for top-selling items to
   support demand in the upcoming month.
Alice> /trace
Trace 1b54dcf8  turn t-1  88.85 s  intent=data_question, turn_kind=simple,
outcome=answered
├── question: How many orders did my brands get last month?
├── policy_context  2 ms
├── input_guard  8.84 s
│   ├── llm.call  0 ms  qwen/qwen3-30b-a3b-2507  ERROR LLM_UNAVAILABLE
│   │   step=input_guard, attempt=1, fallback_used=False, temperature=0.0
│   ├── llm.call  0 ms  qwen/qwen3-30b-a3b-2507  ERROR LLM_UNAVAILABLE
│   │   step=input_guard, attempt=2, fallback_used=False, temperature=0.0
│   ├── llm.call  8.16 s  qwen/qwen3-30b-a3b-2507  tokens 637 in, 51 out
│   │   step=input_guard, attempt=3, fallback_used=False, temperature=0.0
│   └── guard input: pass
├── context_assembly  2 ms
│   └── golden.retrieve  0 ms  g-008 (0.1966), g-001 (0.1874)
├── sql_loop  48.99 s
│   └── sub_question  48.99 s  id=q1, status=ok, repairs=1, broaden_used=0
│       ├── llm.call  0 ms  qwen/qwen3-30b-a3b-2507  ERROR LLM_RATE_LIMITED
│       │   step=sql_write, attempt=1, fallback_used=False, temperature=0.0
│       ├── llm.call  25.67 s  qwen/qwen3-30b-a3b-2507  tokens 1361 in, 161 out
│       │   step=sql_write, attempt=2, fallback_used=False, temperature=0.0
│       ├── guard.sql  2 ms  ERROR SQL_SYNTAX  scope_mode=brands
│       │   └── SQL_SYNTAX (AgentError): Required keyword: '…' missing for
│       │       Order (line 8, column 8)
│       ├── llm.call  22.12 s  qwen/qwen3-30b-a3b-2507  tokens 1720 in, 193 out
│       │   step=sql_repair, attempt=1, fallback_used=False, temperature=0.0
│       ├── guard.sql  19 ms  scope_mode=brands,
│       │   fingerprint=d823d9a853b78aa8ad92a5456daf71f18b1916b59910d7742563a1e
│       │   f6ef…, limit=1000, per_customer=False
│       │   └── guard sql: rewrite  rule SCOPE_BRANDS
│       ├── bq.dry_run  153 ms  sql=SELECT COUNT(DISTINCT oi.order_id) AS
│       │   total_orders FROM (SE…, statement_type=SELECT,
│       │   bytes_processed=1580553
│       ├── bq.query  31 ms  sql=SELECT COUNT(DISTINCT oi.order_id) AS
│       │   total_orders FROM (SE…, bytes_estimated=1580553, row_cap=1000,
│       │   rows=1
│       └── guard.result  0 ms  rows=1, columns_masked=0, cells_masked=0,
│           values_masked=0
│           └── guard result: pass
├── evidence_check  0 ms
├── composer  30.99 s
│   └── llm.call  30.99 s  qwen/qwen3-30b-a3b-2507  tokens 933 in, 234 out
│       step=composer, attempt=1, fallback_used=False, temperature=0.3
├── numeric_check  1 ms
│   ├── numeric_check  removed=0
│   └── guard numeric: pass
├── output_guard  1 ms
│   └── guard.output  1 ms  chars=1244
│       └── guard output: pass
└── finish  0 ms
Alice> /quit
Goodbye.
```

The trace shows what the faults did, and that the chat recovered: the Input Guard's first two attempts failed (`LLM_UNAVAILABLE`) and the third succeeded; the SQL writer's first attempt was rate limited (`LLM_RATE_LIMITED`); the second attempt's SQL had a syntax error that the SQL Guard caught (`SQL_SYNTAX`); one repair fixed it. The answer (139 orders) matches the data, and the model's first line of reading was cut off mid-sentence. Run the same turn again with `--chaos harsh --chaos-seed 3`: the seed replays the faults, not the model's words.

## Tests and evaluation

```bash
python -m pytest                       # everything offline: no network, no keys
python -m pytest -m attack             # PII, brand scope and confirmation attacks against the real graph (a scripted, hijacked model)
python -m pytest -m chaos              # injected faults: the chat never crashes (the real gateway over a fake SDK client)
python -m pytest -m golden             # the golden cases' reference SQL and the scoring machinery
python -m pytest -m trajectory         # the path through the graph and the memory between turns, on the real graph with a scripted model
python -m pytest -m live               # real BigQuery (and later Gemini); manual, skips without credentials
```

Tests use temporary folders and never read the real `users.yaml` or write to `data/`.

The evaluation scores the app itself: the golden cases against reference SQL, the attacks, the chaos runs, and the trajectory cases (does each turn take the path the design allows, and does the memory between turns stay what it should be), with the model and data of the config. Each command writes a run folder under `<data_dir>/eval/` and exits 1 when a hard gate or the baseline bar fails.

```bash
python -m eval.run_golden  --config config.local.yaml   # 16 cases; --update-baseline (or --baseline-from RUN_DIR) writes eval/baseline.json for this setup
python -m eval.run_attacks --config config.local.yaml   # 27 attacks; invariants I1 to I6 must hold for every one
python -m eval.run_chaos   --config config.local.yaml   # the random fault profiles, 5 turns each; the scripted faults run offline
python -m eval.run_trajectory --config config.local.yaml   # 11 scripted conversations: the path through the graph, and the state and memory between turns (also after a restart)
python -m eval.report --tests --config config.local.yaml   # the PRD section 9 table, measured from those runs and the offline suites
python -m eval.run_judge RUN_DIR --config config.yaml       # advisory: a model that did not answer scores each turn of a finished run; --allow-self-judge for a smoke test
python -m src.cli.main scenario run --all --config config.local.yaml   # the 23 scripted conversations against the real app and model (without --all: the 7 "smoke" ones)
python -m src.cli.main golden validate --config config.local.yaml   # the Ingest Job's checks on the seed trios
```

First results (2026-10-25), with the local model `qwen/qwen3-30b-a3b-2507` answering:

| | Sample data | Real BigQuery |
|---|---|---|
| Golden cases | 8 of 16 passed | 9 of 16 passed (this run is the baseline of that setup) |
| Attacks | 25 of 25 | 25 of 25 |
| Chaos run | passed | passed |
| Trajectory cases (the path through the graph and the memory between turns) | 11 of 11 (one memory finding) | not run |
| Hard gates (no PII leak, no brand escape, no delete without confirmation, inside the budget, no crash) | held | held |
| Simple turns under 30 s (target 95%) | 33% of 46, p95 92 s | 35% of 46, p95 97 s |

Every golden failure was on the key figures (summary answers, a reading the case did not fix, a different definition of "returned"). The response-time target was **not** met, because a 30B model on a laptop is slow. None of this says anything about Gemini. [docs/technical-explanation.md](docs/technical-explanation.md) section 7.7 has the detail.

## Repository layout

```text
README.md  CLAUDE.md  LICENSE  requirements.txt  pytest.ini  .env.example
config.yaml            default settings (Gemini, real BigQuery). Secrets go in .env, never here
config.local.yaml      sample data and a local model (Path A)
config.bigquery.yaml   real BigQuery and a local model
users.yaml  users.local.yaml  users.bigquery.yaml    who may see which brands
schema_allowlist.yaml  the approved tables and columns, and the PII split
glossary.yaml          definitions (churn, last month, revenue, ...)
golden/seed/           12 seed trios (question, SQL, report)
personas/default.yaml  the one static persona
scenarios/             23 scripted conversations
src/
  access/              users and brand scope
  cli/                 the chat, doctor, trace, metrics, local-data, golden, scenario, bigquery commands
  agent/               the LangGraph graph: one module per node, prompts, numeric check, answer envelope
  guards/              SQL Guard, Result Guard, Output Guard, pseudonymizer
  data_access/         the only code that talks to BigQuery: safe runner, schema catalog, error mapping
  llm/                 the model gateway: routing, rate limiter, retries, breaker, budget, chaos, fakes
  tools/  reports/     tool registry and manifests, saved reports, idempotency
  storage/             SQLite, migrations, the audit log
  observability/       spans, trace viewer, metrics, alerts
  golden/              trio format, search, glossary, validation
  localdata/  scenarios/   sample data generator, scenario runner
eval/  evalkit/        the evaluation commands and datasets, and the scoring framework
tests/                 the offline test suite
docs/                  architecture, technical explanation, PRD, decision record, plans, requirements
data/                  runtime files (git-ignored)
```

Everything the app writes goes to `data/` (git-ignored): `db.sqlite3` (conversations, saved reports, the audit log and the checkpoints), `logs/app.log` (rotating, masked), `user_hash_salt`, `runs.jsonl` (one metrics row per question; rows older than 90 days are deleted at startup, and so are those of `evaluations.jsonl`), `traces/` (one file per question, and with `--debug-prompts` its exchange file) and `cache/` (the schema, the brand list and the product categories, 24 hours). If `data/db.sqlite3` was made by an older prototype, the app refuses to use it and tells you to move or delete it.

## Troubleshooting

| You see | Do |
|---|---|
| `doctor` fails on `GEMINI_API_KEY` | You are on an environment that uses Gemini. Put `GEMINI_API_KEY=...` in `.env`, or use `--config config.local.yaml`, which does not need it. The tests do not need it either. |
| `doctor` says Google Cloud credentials or the project are missing | `gcloud auth application-default login`, and `GOOGLE_CLOUD_PROJECT=...` in `.env`. Credentials from a service-account file work too (`GOOGLE_APPLICATION_CREDENTIALS`). |
| The chat will not start: "users.yaml has brands that do not match the data" | You are reading `users.yaml` with placeholder brands, or a typo. Copy the exact names from the Brand mapping line of `doctor`. With the sample data, pass `--config config.local.yaml`. |
| The chat will not start: the allow-list has not been reviewed | Compare `schema_allowlist.yaml` with the live schema (`doctor --refresh`), then set `reviewed_against_live: true`. See Path D. |
| "The language model is not responding right now" in the chat, or "The model server did not answer at http://localhost:1234/v1" from `scenario run` | Start LM Studio's local server and load `qwen/qwen3-30b-a3b-2507`, or point `llm.environments.local.base_url` at your server. `doctor --live` checks it. |
| A turn ends with a time-out (local model) | The model is slow on a long prompt. Raise `llm.request_timeout_s` and `budget.*.seconds` in `config.local.yaml`. |
| Errors with "too many requests" or a long wait (Gemini) | The free tier is rate-limited and its numbers are not published. Read your limits in AI Studio and set `llm.default_rpm` and `llm.rate_limits` in `config.yaml`. At the default of 10 requests a minute a "why" turn cannot finish in 60 s. The chat falls back to the sibling model (Flash-Lite) when it can. |
| "The local sample data was not found" | `python -m src.cli.main local-data build --config config.local.yaml` |
| A package will not install | Check `python --version` (3.11 or newer), upgrade pip, or use Python 3.12 if a package has no wheel for yours. |
| Boxes show as `+---+`, or `?` for symbols (Windows) | Set `PYTHONUTF8=1`. |
| "Something went wrong" and a trace ID | The chat is still open. `python -m src.cli.main trace <id>` shows the failed step, and `data/logs/app.log` has the masked crash location. Run with `--debug` to see the traceback on screen. |
| The app refuses to use `data/db.sqlite3` ("has tables this app did not create", or "was built with migration ... but the code has ...") | The file was made by another version. Move or delete it, or set another `app.data_dir`. |
