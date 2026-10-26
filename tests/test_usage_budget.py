"""The token ledger and the daily budget (NFR8, Q15): counting by user, source and UTC day, the cap, the alert line, and `clear`."""
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest

from src.config import BudgetConfig, Settings
from src.errors import REFUSAL_KINDS, AgentError, ErrorKind
from src.storage.audit import AuditLog
from src.storage.db import Database, open_connection
from src.storage.usage import DailyBudget, UsageLedger

ALICE, BOB = "hash-alice", "hash-bob"


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta) -> None:
        self.now += timedelta(**delta)


@pytest.fixture
def clock() -> Clock:
    return Clock(datetime(2026, 10, 25, 9, 0, 0, tzinfo=timezone.utc))


@pytest.fixture
def ledger(tmp_path, clock) -> UsageLedger:
    with Database(tmp_path / "db.sqlite3") as db:
        db.migrate()
    return UsageLedger(tmp_path / "db.sqlite3", clock)


def rows(ledger: UsageLedger) -> list[sqlite3.Row]:
    with closing(open_connection(ledger.path)) as conn:
        return conn.execute("SELECT * FROM token_usage ORDER BY seq").fetchall()


# counting ---------------------------------------------------------------------------------------------------------------------------------------

def test_a_turn_adds_to_todays_totals_apart_for_the_chat_and_for_the_evaluation(ledger):
    ledger.record(ALICE, "chat", tokens_in=1000, tokens_out=200, calls=5, trace_id="aaaaaaaa")
    ledger.record(ALICE, "chat", tokens_in=500, tokens_out=50, calls=3, trace_id="bbbbbbbb")
    ledger.record(ALICE, "eval", tokens_in=4000, tokens_out=300, calls=5, trace_id="aaaaaaaa")
    usage = ledger.today(ALICE)
    assert (usage.chat.entries, usage.chat.calls, usage.chat.tokens_in, usage.chat.tokens_out) == (2, 8, 1500, 250)
    assert (usage.eval.entries, usage.eval.calls, usage.eval.tokens) == (1, 5, 4300)
    assert usage.tokens == 1750 + 4300 and usage.day == "2026-10-25" and usage.cleared_at is None


def test_a_turn_that_spent_nothing_writes_no_row(ledger):
    ledger.record(ALICE, "chat", tokens_in=0, tokens_out=0, calls=0)  # a refusal before any model call
    assert rows(ledger) == [] and ledger.today(ALICE).tokens == 0


def test_a_user_is_counted_apart_from_the_others(ledger):
    ledger.record(ALICE, "chat", tokens_in=100, tokens_out=10, calls=1)
    ledger.record(BOB, "chat", tokens_in=900, tokens_out=90, calls=1)
    assert ledger.today(ALICE).tokens == 110 and ledger.today(BOB).tokens == 990


def test_yesterday_is_not_counted_and_a_new_utc_day_starts_at_midnight(ledger, clock):
    ledger.record(ALICE, "chat", tokens_in=700, tokens_out=0, calls=1)
    clock.now = datetime(2026, 10, 25, 23, 59, 59, tzinfo=timezone.utc)
    assert ledger.today(ALICE).tokens == 700
    clock.advance(seconds=1)  # midnight UTC
    assert ledger.today(ALICE).tokens == 0 and ledger.today(ALICE).day == "2026-10-26"
    ledger.record(ALICE, "chat", tokens_in=5, tokens_out=0, calls=1)
    assert ledger.today(ALICE).tokens == 5 and len(rows(ledger)) == 2  # the old row is still on record


def test_the_day_is_a_utc_day_whatever_zone_the_clock_carries(tmp_path):
    with Database(tmp_path / "db.sqlite3") as db:
        db.migrate()
    plus_three = timezone(timedelta(hours=3))
    clock = Clock(datetime(2026, 10, 26, 1, 0, tzinfo=plus_three))  # 22:00 on the 25th in UTC
    ledger = UsageLedger(tmp_path / "db.sqlite3", clock)
    ledger.record(ALICE, "chat", tokens_in=10, tokens_out=0, calls=1)
    assert ledger.today(ALICE).day == "2026-10-25"


def test_an_unknown_source_is_refused_by_the_code_and_by_the_table(ledger):
    with pytest.raises(ValueError):
        ledger.record(ALICE, "judge", tokens_in=1, tokens_out=1)  # type: ignore[arg-type]
    with closing(open_connection(ledger.path)) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO token_usage (ts, user_hash, source) VALUES ('2026-10-25T09:00:00+00:00', 'h', 'other')")


# clear ------------------------------------------------------------------------------------------------------------------------------------------

def test_clear_starts_the_count_again_and_keeps_every_row(ledger, clock):
    ledger.record(ALICE, "chat", tokens_in=1000, tokens_out=0, calls=2)
    ledger.record(ALICE, "eval", tokens_in=300, tokens_out=0, calls=2)
    clock.advance(minutes=5)
    cleared = ledger.clear(ALICE, by="user")
    assert cleared.tokens == 1300 and cleared.chat.tokens == 1000 and cleared.eval.tokens == 300
    after = ledger.today(ALICE)
    assert after.tokens == 0 and after.cleared_at == "2026-10-25T09:05:00+00:00"
    assert len(rows(ledger)) == 2  # what was spent stays on record
    ledger.record(ALICE, "chat", tokens_in=40, tokens_out=0, calls=1)
    assert ledger.today(ALICE).tokens == 40


def test_a_row_written_in_the_same_second_after_a_clear_is_counted_and_one_before_it_is_not(ledger):
    ledger.record(ALICE, "chat", tokens_in=100, tokens_out=0, calls=1)
    ledger.clear(ALICE, by="user")  # the clock has not moved
    ledger.record(ALICE, "chat", tokens_in=7, tokens_out=0, calls=1)
    assert ledger.today(ALICE).tokens == 7


def test_clearing_one_user_leaves_the_others(ledger):
    ledger.record(ALICE, "chat", tokens_in=100, tokens_out=0, calls=1)
    ledger.record(BOB, "chat", tokens_in=200, tokens_out=0, calls=1)
    ledger.clear(ALICE, by="operator")
    assert ledger.today(ALICE).tokens == 0 and ledger.today(BOB).tokens == 200


def test_a_clear_is_audited_with_the_user_hash_and_the_count_and_no_text(ledger):
    ledger.record(ALICE, "chat", tokens_in=900, tokens_out=100, calls=2)
    ledger.clear(ALICE, by="operator", trace_id="cccccccc")
    entries = AuditLog(ledger.path).entries(action="budget_cleared")
    assert len(entries) == 1
    entry = entries[0]
    assert (entry["user_hash"], entry["count"], entry["outcome"], entry["trace_id"]) == (ALICE, 1000, "ok", "cccccccc")
    assert entry["detail"] == "cleared by operator"


def test_a_clear_by_somebody_else_than_a_user_or_the_operator_is_refused_by_the_table(ledger):
    with closing(open_connection(ledger.path)) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO budget_clears (ts, user_hash, cleared_by, usage_seq) VALUES ('t', 'h', 'model', 0)")


# the cap ----------------------------------------------------------------------------------------------------------------------------------------

def test_without_a_cap_nothing_is_ever_refused_and_the_use_is_still_counted(ledger):
    budget = DailyBudget(ledger, None)
    ledger.record(ALICE, "chat", tokens_in=10**9, tokens_out=0, calls=1)
    status = budget.check(ALICE)
    assert status.cap is None and status.fraction is None and status.exhausted is False and status.used == 10**9


def test_a_turn_is_refused_once_the_cap_is_used_up_and_not_before(ledger):
    budget = DailyBudget(ledger, 1000)
    ledger.record(ALICE, "chat", tokens_in=999, tokens_out=0, calls=1)
    assert budget.check(ALICE).used == 999  # one token under: the turn runs (and may go over: the cap is checked before a turn)
    ledger.record(ALICE, "chat", tokens_in=1, tokens_out=0, calls=1)
    with pytest.raises(AgentError) as caught:
        budget.check(ALICE)
    assert caught.value.kind is ErrorKind.DAILY_BUDGET_USED and caught.value.kind in REFUSAL_KINDS
    assert "1000 of 1000" in caught.value.detail


def test_the_evaluation_counts_against_the_same_cap(ledger):
    budget = DailyBudget(ledger, 1000)
    ledger.record(ALICE, "eval", tokens_in=1200, tokens_out=0, calls=5)
    with pytest.raises(AgentError):
        budget.check(ALICE)
    assert budget.check(BOB).used == 0  # another user is not affected


def test_clear_lets_the_user_work_again(ledger):
    budget = DailyBudget(ledger, 1000)
    ledger.record(ALICE, "chat", tokens_in=5000, tokens_out=0, calls=1)
    with pytest.raises(AgentError):
        budget.check(ALICE)
    ledger.clear(ALICE, by="user")
    assert budget.check(ALICE).used == 0


def test_the_alert_fires_once_when_the_use_crosses_the_line_and_not_on_every_later_turn(ledger):
    budget = DailyBudget(ledger, 1000, alert_fraction=0.8)
    before = budget.status(ALICE)
    ledger.record(ALICE, "chat", tokens_in=850, tokens_out=0, calls=1)
    after = budget.status(ALICE)
    assert budget.crossed_alert(before, after) is True
    ledger.record(ALICE, "chat", tokens_in=50, tokens_out=0, calls=1)
    assert budget.crossed_alert(after, budget.status(ALICE)) is False  # already over the line
    assert DailyBudget(ledger, None).crossed_alert(before, after) is False  # no cap, no alert


def test_the_budget_reads_its_cap_from_the_settings(tmp_path):
    with Database(tmp_path / "data" / "db.sqlite3") as db:
        db.migrate()
    settings = Settings(budget=BudgetConfig(daily_tokens_per_user=250_000, alert_fraction=0.5), base_dir=tmp_path)
    budget = DailyBudget.from_settings(settings)
    assert budget.cap == 250_000 and budget.alert_fraction == 0.5
    assert DailyBudget.from_settings(Settings(base_dir=tmp_path)).cap is None  # the default: no cap until the client names an amount (Q15)


@pytest.mark.parametrize("value", [0, -5])
def test_a_cap_of_zero_or_less_is_a_config_mistake(value):
    with pytest.raises(ValueError):
        BudgetConfig(daily_tokens_per_user=value)
