"""A few saved reports for the local app database, so the delete and undo flow (US7 to US9) can be tried at once.

They go in through `ReportStore.save`, the same door as a report the user saves in the chat, so each one has its audit row. The
bodies are fixtures: they hold no figures and no personal data, and say so. A second run adds nothing (each report has a fixed
idempotency key).

The sets are shaped for the confirmation levels: Alice has four reports (two are about returns), Bob has three, and the CEO has
fourteen, twelve of them weekly digests, so "delete the weekly digests" or "delete all my reports" meets the large-batch level.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.access.users import load_users, resolve_salt
from src.config import Settings
from src.reports.store import Actor, ReportStore
from src.storage.db import Database, open_connection, utc_now
from src.tools.confirmation import id_hash_key, id_set_hash

FIXTURE_BODY = "Fixture report for local testing. It holds no figures and no personal data."

# user -> (title, words a search should find)
SEED_REPORTS: dict[str, tuple[tuple[str, str], ...]] = {
    "alice": (
        ("Monthly revenue by brand", "revenue monthly brands trend"),
        ("Returns review for dresses", "returns dresses return rate"),
        ("Q1 performance summary", "quarter summary insights action items"),
        ("Top customers by spend", "customers ranked spend"),
    ),
    "bob": (
        ("Weekly sales snapshot", "sales weekly snapshot"),
        ("Returns by category", "returns categories"),
        ("Customer segments by region", "customers segments regions"),
    ),
    "ceo": (
        ("All-brand revenue overview", "revenue brands overview"),
        ("Brand returns comparison", "returns brands comparison"),
        *((f"Weekly digest, week {week}", "weekly digest") for week in range(1, 13)),
    ),
}


@dataclass(frozen=True)
class SeedResult:
    saved: dict[str, int]  # per user: reports that were added just now
    skipped_users: tuple[str, ...]  # users in the seed that are not in users.yaml


def seed_saved_reports(settings: Settings) -> SeedResult:
    """Add the fixture reports for the users that `users.yaml` has. Migrates the app database first, as the chat does."""
    with Database.from_settings(settings) as database:
        database.migrate()
    directory = load_users(settings)
    key = id_hash_key(resolve_salt(settings))
    store = ReportStore.from_settings(settings, hash_ids=lambda ids: id_set_hash(ids, key))
    saved: dict[str, int] = {}
    skipped: list[str] = []
    for user_id, reports in SEED_REPORTS.items():
        if user_id not in directory.names:
            skipped.append(user_id)
            continue
        user = directory.get(user_id)
        conversation_id = f"conv-seed-{user_id}"
        conn = open_connection(settings.db_path)
        try:
            conn.execute(
                "INSERT OR IGNORE INTO conversations (id, user_id, created_at, last_active_at) VALUES (?, ?, ?, ?)",
                (conversation_id, user_id, utc_now(), utc_now()),
            )
        finally:
            conn.close()
        added = 0
        for number, (title, search_text) in enumerate(reports, 1):
            who = Actor(user_id, user.user_hash, conversation_id, f"t-{number}", f"seed{number:04d}")
            outcome = store.save(who, title=title, body=FIXTURE_BODY, search_text=f"{title} {search_text}", key=f"seed-{user_id}-{number}")
            added += int(outcome.created)
        saved[user_id] = added
    return SeedResult(saved, tuple(skipped))
