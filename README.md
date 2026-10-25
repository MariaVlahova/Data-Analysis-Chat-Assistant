# Data Analysis Chat Assistant

A chat agent that answers business questions about the `bigquery-public-data.thelook_ecommerce` dataset.
Brand managers see only their own brands; the CEO sees all of them.

> **Status:** plans 01 to 08 and 10 are built. The chat runs the agent graph (plan 07): it checks the message, plans, writes
> and guards the SQL, runs it, checks the numbers and the answer, and handles saving and deleting reports behind a
> confirmation. Plans 03 to 05, 07 and 08 were built and tested offline, with a scripted model and a fake BigQuery client;
> their live steps need a Google sign-in and a Gemini key (see each plan's "Still to do"). Saved reports are kept in
> the SQLite database with an append-only audit log, a delete is soft with a 30-day undo, and a delete that is waiting
> for your confirmation is still waiting when you start the chat again.
> This README holds the setup section only. Plan 11 completes it (architecture, example run, limits).

## Prerequisites

- **Python 3.11 or newer.** All requirements were installed into a fresh virtual environment and imported on
  Python 3.14.6, so no fallback version is needed.
- A **Google Cloud project** (BigQuery jobs run, and are billed, in it) and the `gcloud` CLI.
- A **Gemini API key** from [Google AI Studio](https://aistudio.google.com/).

## Setup

```bash
# 1. Create and activate a virtual environment
python -m venv venv
source venv/bin/activate            # Windows PowerShell: venv\Scripts\Activate.ps1

# 2. Install everything (one file, tests included)
pip install -r requirements.txt

# 3. Sign in to Google Cloud for BigQuery (application default credentials)
gcloud auth application-default login

# 4. Secrets: copy the template and fill it in. Real environment variables win over .env.
cp .env.example .env                # Windows: copy .env.example .env
#    GEMINI_API_KEY=...
#    GOOGLE_CLOUD_PROJECT=your-project-id

# 5. Check the setup. It prints one line per check and the fix for each failure.
python -m src.cli.main doctor
#    Add --live to also call Gemini: it checks the model IDs in config.yaml against your key and makes one
#    small call (a little of the free-tier quota).
python -m src.cli.main doctor --live

# 6. Chat
python -m src.cli.main chat --user alice
```

Secrets never go into `config.yaml`. A mistyped key in `config.yaml` stops startup on purpose and names the file,
line and key.

## Using the chat

```bash
python -m src.cli.main chat --user alice            # a brand manager
python -m src.cli.main chat --user ceo              # sees all brands
python -m src.cli.main chat --user alice --debug    # log lines and tracebacks on screen
python -m src.cli.main chat --user alice --conversation conv-1a2b3c4d   # continue a conversation
python -m src.cli.main chat --user alice --chaos harsh --chaos-seed 7   # inject faults on purpose; the same seed replays the run
```

`--chaos mild|harsh` injects model errors (429, 503, timeouts, unusable JSON) into about 15% (mild) or 40% (harsh) of
model calls, BigQuery errors into fewer BigQuery calls, and now and then a broken SQL query, to show that retries, the
circuit breaker and the fallback model keep the chat alive. Without `--chaos-seed` a seed is drawn and printed in the banner. Injected faults replace the call, so
they use no quota.

What you can ask: questions about sales, products and customers (a total, a ranking, a trend), "why" and comparison
questions that need several queries, reports with insights and action items, and what data is available. Customers
appear only as labels such as `Customer #48213`, never by name. You can also say "save that", "list my reports",
"delete the reports mentioning X" and "undo". A delete shows what it found and waits for your next line: `y` or `yes`
for one report, `yes` for a short list, and `DELETE <n>` for "all" or ten or more. Anything else cancels. Every answer
starts with how your question was read and which brands it covers, and ends with notes about anything that was hidden,
cut or could not be answered.

Inside the chat: `/help`, `/quit`, `/new` (new conversation), `/trace [id] [--raw]` (the steps of a turn, by default
the last one) and `/metrics [--last N]` (totals and rates). You see your own turns; the CEO sees everyone's.
Every error message ends with a trace ID. Outside the chat, the operator views (all users) are
`python -m src.cli.main trace [ID]` and `python -m src.cli.main metrics [--last N]`.

Traces, logs and metrics are masked on a **best-effort** basis. A pattern scan replaces emails, phone numbers,
US social security and employer tax IDs, and card numbers before anything is written. It does **not** find names,
street addresses or a bare customer ID, so the design also keeps result rows, DataFrames, cell values and exception
messages out of them, and people appear only as a hash. The question you type is stored (masked) in the trace. Prompts
are not stored unless you start the chat with `--debug-prompts` (still masked). Trace files are deleted after 90 days
(`retention.trace_days`); `runs.jsonl` rows hold counts and IDs only and are kept.

The answer tone comes from one static persona file, `personas/default.yaml`. Set `persona.enabled: true` in
`config.yaml` to use it. There is no persona editor, no per-user style and no switching.

Exit codes: `0` normal, `2` a startup problem (bad config, unknown user, no brands, database problem),
`1` a failed `doctor` check or a `trace` ID that was not found.

## Running without Google Cloud (generated sample data)

`config.local.yaml` runs the whole app on this machine: generated test data in a DuckDB file instead of BigQuery, and
`qwen/qwen3-30b-a3b-2507` served by [LM Studio](https://lmstudio.ai/) (load the model and start its local server,
`http://localhost:1234/v1`) instead of Gemini. No Google sign-in, no key, no cost. Every guard, the graph, saved reports and the traces
run as in a live run. The banner says `LOCAL SAMPLE DATA` and `LOCAL MODEL`. The environments `default`, `develop`, `staging` and `prod`
keep Gemini; `app.environment` or `OPSFLEET_ENV` chooses (`OPSFLEET_ENV=default` runs the sample data with Gemini).

```bash
python -m src.cli.main local-data build        --config config.local.yaml   # about a second; the same seed gives the same rows
python -m src.cli.main local-data seed-reports --config config.local.yaml   # a few saved reports to try delete and undo
python -m src.cli.main chat --user alice       --config config.local.yaml   # users: alice, bob, carol, dave, ceo, nobrands
python -m src.cli.main scenario run            --config config.local.yaml   # scripted test scenarios against the app and the local model
```

`docs/local-testing.md` lists the scenarios, what the data holds, what to ask each user, how to check an answer, what a local run cannot prove,
and how to test the BigQuery part on real BigQuery: `config.bigquery.yaml` runs the same scenarios against the real dataset with the local model
(`bigquery init-users` writes the users from the real brand list; section 7 has the steps in order).

## Users and brands (`users.yaml`)

- A brand manager sees only the brands listed. Brand names are compared **exactly**: case and spacing count.
- An **empty brand list means no access**, never "all brands". The chat refuses to start for that user.
- Only `role: ceo` with `all_brands: true` sees every brand. (Our proposal; not yet confirmed by the client.)
- The brands of the user who starts the chat are checked against the real brand list (`doctor` checks every user). A
  typo stops startup with the user and the brand (`doctor` also lists up to three close matches; the chat does not,
  because they could be other managers' brands). A list under 24 hours old is used
  without calling BigQuery. If BigQuery cannot be reached, a saved list up to 7 days old
  (`data/cache/brands.json`) is used and the chat says so. With no saved list, an older one, or an error that is not
  an outage (access revoked, wrong project), it refuses to start.
- The brand names in the shipped file are **placeholders** (`<verified brand N>`), so **the chat will not start for
  alice and bob until you replace them** (the CEO can start). After signing in to Google Cloud, run `python -m src.cli.main doctor`: the Brand mapping
  check lists the top brands by item count. Copy the exact names into `users.yaml`. `nobrands` is a deliberate test
  user for the no-access rule.

## Tests

```bash
python -m pytest                       # everything offline: no network, no keys
python -m pytest -m attack             # PII, brand scope and confirmation attacks against the real graph (a scripted, hijacked model)
python -m pytest -m chaos              # injected faults: the chat never crashes (the real gateway over a fake SDK client)
python -m pytest -m golden             # the golden cases' reference SQL and the scoring machinery
python -m pytest -m live               # real BigQuery (and later Gemini); manual, skips without credentials
```

Tests use temporary folders and never read the real `users.yaml` or write to `data/`.

The evaluation (plan 09) scores the app itself: the golden cases against reference SQL, the attacks, and the chaos runs, with the model and data of the
config. Each command writes a run folder under `<data_dir>/eval/` and exits 1 when a hard gate or the baseline bar fails.

```bash
python -m eval.run_golden  --config config.local.yaml   # 16 cases; --update-baseline (or --baseline-from RUN_DIR) writes eval/baseline.json for this setup
python -m eval.run_attacks --config config.local.yaml   # 25 attacks; invariants I1 to I6 must hold for every one
python -m eval.run_chaos   --config config.local.yaml   # the random fault profiles, 5 turns each; the scripted faults run offline
python -m eval.report --tests --config config.local.yaml   # the PRD section 9 table, measured from those runs and the offline suites
python -m eval.run_judge RUN_DIR --config config.yaml       # advisory: a model that did not answer scores each turn of a finished run (evalkit/rubrics.xlsx); --allow-self-judge for a smoke test
```

## Runtime files

Everything the app writes goes to `data/` (git-ignored): `db.sqlite3`, `logs/app.log` (rotating, masked),
`user_hash_salt`, `runs.jsonl` (one metrics row per question) and `traces/` (one file per question). If `data/db.sqlite3` was made by an older prototype, the app refuses to use it and tells
you to move or delete it.
