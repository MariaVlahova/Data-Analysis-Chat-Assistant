# Data Analysis Chat Assistant

A chat agent that answers business questions about the `bigquery-public-data.thelook_ecommerce` dataset.
Brand managers see only their own brands; the CEO sees all of them.

> **Status:** plan 01 (foundation) is done: project skeleton, config, users, storage and the CLI shell.
> The chat currently **echoes** your question. The agent behind it arrives with plans 03 to 07.
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

Inside the chat: `/help`, `/quit`, `/new` (new conversation), `/trace` and `/metrics` (arrive with plan 02).

The answer tone comes from one static persona file, `personas/default.yaml`. Set `persona.enabled: true` in
`config.yaml` to use it. There is no persona editor, no per-user style and no switching.

Exit codes: `0` normal, `2` a startup problem (bad config, unknown user, no brands, database problem),
`1` a failed `doctor` check.

## Users and brands (`users.yaml`)

- A brand manager sees only the brands listed. Brand names are compared **exactly**: case and spacing count.
- An **empty brand list means no access**, never "all brands". The chat refuses to start for that user.
- Only `role: ceo` with `all_brands: true` sees every brand. (Our proposal; not yet confirmed by the client.)
- The brand names in the shipped file are **placeholders** (`<verified brand N>`). Plan 03 replaces them with
  real brands from the data. `doctor` warns until then. `nobrands` is a deliberate test user for the no-access rule.

## Tests

```bash
python -m pytest
```

Tests use temporary folders and never read the real `users.yaml` or write to `data/`.

## Runtime files

Everything the app writes goes to `data/` (git-ignored): `db.sqlite3`, `logs/app.log`, `user_hash_salt`, and later
`runs.jsonl` and `traces/`. If `data/db.sqlite3` was made by an older prototype, the app refuses to use it and tells
you to move or delete it.
