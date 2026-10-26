"""The audit log is kept one year and no longer (plan 12, E1 and E2; NFR21, A11), and the settings that name the language, the currency and the time zone mean what they say
(F1, F2; A16, A17).

Migration 002 made every delete of an audit row refuse, which kept the log for ever. Migration 005 refuses it only for a row younger than 365 days; the startup purge removes
the older ones and writes one `audit_purged` row about it. `retention.audit_days` cannot be below 365, so the config cannot ask for less than the database allows.
"""
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.cli import main as cli_main
from src.config import load_settings
from src.errors import ConfigError
from src.llm.routing import Step
from src.storage.audit import AuditEntry, AuditLog, purge_old_entries, write_entry
from src.storage.db import MIGRATIONS_DIR, Database, iso, open_connection
from tests.agent_support import composer_reply, guard_reply, make_rig, sql_reply

DAY = timedelta(days=1)


def ago(days: float) -> str:
    return iso(datetime.now(timezone.utc) - timedelta(days=days))


@pytest.fixture
def db_path(tmp_path) -> Path:
    path = tmp_path / "db.sqlite3"
    with Database(path) as database:
        database.migrate()
    return path


def put(path: Path, *ages: float) -> None:
    """One audit row for each age in days, written the way the app writes one (with its `ts`)."""
    with closing(open_connection(path)) as conn:
        for age in ages:
            write_entry(conn, AuditEntry("save", f"hash-{age}", "ok", count=1), ago(age))


def rows(path: Path) -> list[dict]:
    return AuditLog(path).entries()


# the trigger (E1) ------------------------------------------------------------------------------------------------------------------

def test_a_row_younger_than_a_year_cannot_be_deleted_and_an_older_row_can(db_path):
    put(db_path, 0, 364, 366)
    with closing(open_connection(db_path)) as conn:
        for age in ("hash-0", "hash-364"):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute("DELETE FROM audit_log WHERE user_hash = ?", (age,))
        assert conn.execute("DELETE FROM audit_log WHERE user_hash = 'hash-366'").rowcount == 1  # past the year: the database lets it go
    assert sorted(row["user_hash"] for row in rows(db_path)) == ["hash-0", "hash-364"]


@pytest.mark.parametrize("statement", [
    "UPDATE audit_log SET outcome = 'tampered'",
    "UPDATE audit_log SET count = 0 WHERE user_hash = 'hash-0'",
    "UPDATE audit_log SET outcome = 'tampered' WHERE user_hash = 'hash-366'",  # an old row too: nothing is ever changed
    "DELETE FROM audit_log",
    "DELETE FROM audit_log WHERE user_hash = 'hash-364'",
])
def test_an_update_of_any_audit_row_and_a_delete_that_reaches_a_young_one_are_still_refused(db_path, statement):
    put(db_path, 0, 364, 366)
    with closing(open_connection(db_path)) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(statement)
    assert len(rows(db_path)) == 3


def test_an_existing_database_gets_the_new_trigger_at_the_next_start(tmp_path):
    old_migrations = tmp_path / "migrations"
    old_migrations.mkdir()
    for file in MIGRATIONS_DIR.glob("*.sql"):
        if not file.name.startswith("005"):
            shutil.copy(file, old_migrations / file.name)
    path = tmp_path / "db.sqlite3"
    with Database(path, old_migrations) as database:
        database.migrate()
    put(path, 400)
    with closing(open_connection(path)) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM audit_log")  # the log of the earlier version: nothing goes
    with Database(path) as database:
        assert database.migrate() == [5]
    with closing(open_connection(path)) as conn:
        assert conn.execute("DELETE FROM audit_log").rowcount == 1


# the purge (E2) --------------------------------------------------------------------------------------------------------------------

def test_a_row_366_days_old_is_deleted_at_startup_and_a_row_is_added_that_says_so(db_path):
    put(db_path, 0, 364, 366, 500)
    assert purge_old_entries(db_path, 365) == 2
    kept = rows(db_path)
    assert sorted(row["user_hash"] for row in kept if row["action"] == "save") == ["hash-0", "hash-364"]
    (purge,) = [row for row in kept if row["action"] == "audit_purged"]
    assert (purge["user_hash"], purge["count"], purge["outcome"]) == ("system", 2, "ok") and purge["detail"] == "older than 365 days"
    assert purge["seq"] > max(row["seq"] for row in kept if row["action"] == "save")  # appended, like every row


def test_a_second_purge_finds_nothing_and_writes_no_row(db_path):
    put(db_path, 0, 400)
    assert purge_old_entries(db_path, 365) == 1
    before = rows(db_path)
    assert purge_old_entries(db_path, 365) == 0
    assert rows(db_path) == before


def test_the_row_that_records_a_purge_is_itself_kept_for_a_year(db_path):
    put(db_path, 400)
    purge_old_entries(db_path, 365)
    assert purge_old_entries(db_path, 365) == 0 and [row["action"] for row in rows(db_path)] == ["audit_purged"]


def test_a_longer_retention_keeps_what_it_says(db_path):
    put(db_path, 380, 450)
    assert purge_old_entries(db_path, 400) == 1 and [row["user_hash"] for row in rows(db_path) if row["action"] == "save"] == ["hash-380"]


def test_the_database_holds_the_year_whatever_the_caller_asks_for(db_path):
    put(db_path, 100)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        purge_old_entries(db_path, 30)  # a caller that bypassed the config check: the trigger refuses, and nothing is left half done
    assert [row["action"] for row in rows(db_path)] == ["save"]


def test_the_startup_housekeeping_runs_the_purge_and_never_stops_the_chat(project, monkeypatch):
    settings = load_settings(project / "config.yaml", load_env_file=False)
    with Database(settings.db_path) as database:
        database.migrate()
    put(settings.db_path, 0, 400)
    cli_main._housekeeping("the purge of the audit log", lambda: cli_main._purge_old_audit_rows(settings))
    assert [row["action"] for row in rows(settings.db_path)] == ["save", "audit_purged"]
    monkeypatch.setattr("src.storage.audit.purge_old_entries", lambda *args, **kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")))
    cli_main._housekeeping("the purge of the audit log", lambda: cli_main._purge_old_audit_rows(settings))  # logged, not raised


def test_the_report_purge_still_never_touches_the_audit_log(reports):
    """E3. (The longer test of it is in tests/test_audit.py.)"""
    reports.seed("Revenue Q1")
    before = reports.audit()
    reports.store.purge_expired()
    assert reports.audit()[: len(before)] == before


# the config (E2, F1) -------------------------------------------------------------------------------------------------------------

def load(tmp_path, text):
    (tmp_path / "config.yaml").write_text(text, encoding="utf-8")
    return load_settings(tmp_path / "config.yaml", load_env_file=False)


@pytest.mark.parametrize("days", [200, 364, 1, 0])
def test_less_than_a_year_of_audit_log_does_not_load(tmp_path, days):
    with pytest.raises(ConfigError) as err:
        load(tmp_path, f"retention:\n  audit_days: {days}\n")
    assert "audit_days" in err.value.message and "365" in err.value.message


def test_a_year_or_more_loads(tmp_path):
    assert load(tmp_path, "retention:\n  audit_days: 365\n").retention.audit_days == 365
    assert load(tmp_path, "retention:\n  audit_days: 730\n").retention.audit_days == 730


@pytest.mark.parametrize("text,key,supported", [
    ("app:\n  currency: EUR\n", "currency", "USD"),
    ("app:\n  timezone: CET\n", "timezone", "UTC"),
    ("app:\n  language: de\n", "language", "en"),
    ("app:\n  currency: usd\n", "currency", "USD"),  # not the same value: the prompts say USD
])
def test_another_language_currency_or_time_zone_stops_the_app_at_start_and_says_what_is_supported(tmp_path, text, key, supported):
    with pytest.raises(ConfigError) as err:
        load(tmp_path, text)
    assert f"app.{key}" in err.value.message and f"'{supported}'" in err.value.message and "not supported" in err.value.message


def test_the_supported_values_load():
    settings = load_settings(Path(__file__).resolve().parent.parent / "config.yaml", load_env_file=False)
    assert (settings.app.language, settings.app.currency, settings.app.timezone) == ("en", "USD", "UTC")


# the rules reach the prompts (F2) -----------------------------------------------------------------------------------------------

def test_the_money_rule_and_the_time_rule_reach_the_prompts_that_need_them(project, fake_bigquery):
    import pandas as pd

    fake_bigquery.add("revenue", df=pd.DataFrame({"brand": ["Brand A"], "revenue": [1234.5]}))
    rig = make_rig(project, fake_bigquery=fake_bigquery)
    question = "What was my revenue by month in 2025?"
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply(standalone=question))
    rig.gateway.queue(Step.SQL_WRITE, sql_reply())
    rig.gateway.queue(Step.COMPOSER, composer_reply("Brand A earned 1,234.5."))
    rig.ask(question)
    for step in (Step.SQL_WRITE, Step.COMPOSER):
        prompt = rig.gateway.calls_for(step)[0]
        text = prompt.system + prompt.contents
        assert "US dollars (USD)" in text, step  # money: USD
        assert "dates and times: UTC" in text, step  # time: UTC
