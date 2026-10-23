# Data Analysis Chat Assistant

A chat agent that answers business questions about the `bigquery-public-data.thelook_ecommerce` dataset.
Brand managers see only their own brands; the CEO sees all of them.

> **Status:** plans 01 (foundation) and 02 (observability) are done: project skeleton, config, users, storage, the CLI
> shell, and tracing, masked logs and metrics. The chat currently **echoes** your question. The agent behind it
> arrives with plans 03 to 07.
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
```

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
python -m pytest
```

Tests use temporary folders and never read the real `users.yaml` or write to `data/`.

## Runtime files

Everything the app writes goes to `data/` (git-ignored): `db.sqlite3`, `logs/app.log` (rotating, masked),
`user_hash_salt`, `runs.jsonl` (one metrics row per question) and `traces/` (one file per question). If `data/db.sqlite3` was made by an older prototype, the app refuses to use it and tells
you to move or delete it.
