"""Token use in the chat (NFR8): every turn's tokens are logged and kept in the daily ledger, `/budget` shows and clears them, and a cap refuses a turn."""
import json
import re
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from src.cli import main as cli_main
from src.cli.main import app
from src.observability import turn_metrics
from src.storage.audit import AuditLog

runner = CliRunner(env={"COLUMNS": "200"})


def chat(config_path: Path, user: str, text: str, *extra: str):
    return runner.invoke(app, ["chat", "--user", user, "--config", str(config_path), *extra], input=text)


def spending(tokens_in: int, tokens_out: int, calls: list | None = None):
    """A stand-in turn that spends tokens the way the gateway would count them."""

    def turn(session, text, *, trace_id, progress):
        if calls is not None:
            calls.append(text)
        turn_metrics().add_llm_call("fake-model", tokens_in=tokens_in, tokens_out=tokens_out)
        turn_metrics().add_llm_call("fake-model", tokens_in=tokens_in, tokens_out=tokens_out)
        return f"answer to {text}"

    return turn


def with_budget(project: Path, cap: int | None, alert: float = 0.8) -> Path:
    lines = ["app:\n  language: en\nbudget:\n"]
    if cap is not None:
        lines.append(f"  daily_tokens_per_user: {cap}\n")
    lines.append(f"  alert_fraction: {alert}\n")
    (project / "config.yaml").write_text("".join(lines), encoding="utf-8")
    return project / "config.yaml"


def metrics_rows(project: Path) -> list[dict]:
    path = project / "data" / "runs.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def usage_rows(project: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(project / "data" / "db.sqlite3")
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM token_usage ORDER BY seq").fetchall()
    finally:
        conn.close()


# logging ----------------------------------------------------------------------------------------------------------------------------------------

def test_every_turn_writes_its_tokens_to_the_log_the_metrics_row_and_the_ledger(project, config_path, monkeypatch):
    monkeypatch.setattr(cli_main, "run_turn", spending(1200, 300))
    result = chat(config_path, "alice", "first question\nsecond question\n/quit\n")
    assert result.exit_code == 0, result.output

    rows = metrics_rows(project)
    assert [(row["llm_calls"], row["tokens_in"], row["tokens_out"]) for row in rows] == [(2, 2400, 600), (2, 2400, 600)]
    log = (project / "data" / "logs" / "app.log").read_text(encoding="utf-8")
    for row in rows:
        assert re.search(rf"turn done trace_id={row['trace_id']} outcome=answered llm_calls=2 tokens_in=2400 tokens_out=600 ", log)
    ledger = usage_rows(project)
    assert [(r["source"], r["trace_id"], r["calls"], r["tokens_in"], r["tokens_out"]) for r in ledger] == [
        ("chat", rows[0]["trace_id"], 2, 2400, 600), ("chat", rows[1]["trace_id"], 2, 2400, 600),
    ]


def test_a_refused_turn_that_spent_nothing_writes_no_ledger_row(project, config_path, monkeypatch):
    from src.errors import AgentError, ErrorKind

    def refuse(session, text, *, trace_id, progress):
        raise AgentError(ErrorKind.OUT_OF_SCOPE)

    monkeypatch.setattr(cli_main, "run_turn", refuse)
    result = chat(config_path, "alice", "what is the weather?\n/quit\n")
    assert "Not possible" in result.output and usage_rows(project) == []


# /budget ----------------------------------------------------------------------------------------------------------------------------------------

def test_budget_shows_the_days_use_and_the_last_turn_and_says_when_there_is_no_cap(config_path, monkeypatch):
    monkeypatch.setattr(cli_main, "run_turn", spending(1000, 100))
    result = chat(config_path, "alice", "a question\n/budget\n/quit\n")
    assert "Token use today" in result.output
    assert "1 turn(s), 2 model call(s), 2,000 tokens in, 200 tokens out" in result.output
    assert "no daily cap is set" in result.output
    assert "Last turn: 2,000 tokens in, 200 tokens out, 2 model call(s)." in result.output


def test_budget_before_any_question_shows_zeros(config_path):
    result = chat(config_path, "alice", "/budget\n/quit\n")
    assert "0 turn(s), 0 model call(s), 0 tokens in, 0 tokens out" in result.output and "Last turn" not in result.output


def test_the_use_carries_over_to_the_next_chat_of_the_same_day_and_is_not_shared_with_another_user(config_path, monkeypatch):
    monkeypatch.setattr(cli_main, "run_turn", spending(500, 0))
    chat(config_path, "alice", "one\n/quit\n")
    again = chat(config_path, "alice", "/budget\n/quit\n")
    assert "1 turn(s), 2 model call(s), 1,000 tokens in" in again.output
    bob = chat(config_path, "bob", "/budget\n/quit\n")
    assert "0 turn(s), 0 model call(s)" in bob.output


def test_budget_clear_starts_the_count_again_keeps_the_rows_and_is_audited(project, config_path, monkeypatch):
    monkeypatch.setattr(cli_main, "run_turn", spending(1000, 100))
    result = chat(config_path, "alice", "a question\n/budget clear\n/quit\n")
    assert "Today's count was cleared: 2,200 tokens (2,200 chat, 0 evaluation)" in result.output
    assert "0 turn(s), 0 model call(s), 0 tokens in" in result.output  # the screen after the clear
    assert len(usage_rows(project)) == 1  # nothing was deleted
    entries = AuditLog(project / "data" / "db.sqlite3").entries(action="budget_cleared")
    assert len(entries) == 1 and entries[0]["count"] == 2200 and entries[0]["detail"] == "cleared by user"


def test_budget_with_a_word_it_does_not_know_prints_the_usage(config_path):
    assert "Usage: /budget [clear]" in chat(config_path, "alice", "/budget everything\n/quit\n").output


# the cap ----------------------------------------------------------------------------------------------------------------------------------------

def test_a_turn_does_not_start_once_the_daily_cap_is_used_up(project, monkeypatch):
    config = with_budget(project, cap=3000)
    calls: list[str] = []
    monkeypatch.setattr(cli_main, "run_turn", spending(1000, 250, calls))  # 2,500 tokens a turn
    result = chat(config, "alice", "one\ntwo\nthree\n/quit\n")
    assert calls == ["one", "two"]  # turn 3 started with 5,000 of 3,000 used and never ran
    assert "Not possible" in result.output and "usage budget for your account is used up" in result.output
    refused = metrics_rows(project)[-1]
    assert refused["outcome"] == "refused" and "DAILY_BUDGET_USED" in refused["error_kinds"] and refused["llm_calls"] == 0
    assert refused["turn_id"] == "t-3"  # a refused turn is numbered like any other
    assert result.output.count("answer to") == 2
    assert result.exit_code == 0  # the chat itself stays open


def test_the_cap_is_a_soft_one_the_turn_that_crosses_it_still_finishes(project, monkeypatch):
    config = with_budget(project, cap=1000)
    calls: list[str] = []
    monkeypatch.setattr(cli_main, "run_turn", spending(5000, 0, calls))  # one turn spends 10,000
    result = chat(config, "alice", "one\ntwo\n/quit\n")
    assert calls == ["one"] and "answer to one" in result.output


def test_clear_lets_the_user_carry_on_after_the_cap(project, monkeypatch):
    config = with_budget(project, cap=1000)
    calls: list[str] = []
    monkeypatch.setattr(cli_main, "run_turn", spending(1000, 0, calls))
    result = chat(config, "alice", "one\ntwo\n/budget clear\nthree\n/quit\n")
    assert calls == ["one", "three"] and "answer to three" in result.output


def test_one_warning_when_a_turn_takes_the_day_past_the_alert_line(project, monkeypatch):
    config = with_budget(project, cap=5000, alert=0.8)
    monkeypatch.setattr(cli_main, "run_turn", spending(850, 0))  # 1,700 a turn
    result = chat(config, "alice", "one\ntwo\nthree\nfour\n/quit\n")
    # 1,700, 3,400, 5,100 (over the line and over the cap), then turn four is refused
    assert len(re.findall(r"You have used \d+% of today's token budget", result.output)) == 1
    assert "You have used 102% of today's token budget (5,100 of 5,000 tokens)" in result.output


def test_a_cap_in_the_config_shows_in_budget(project, monkeypatch):
    config = with_budget(project, cap=10_000)
    monkeypatch.setattr(cli_main, "run_turn", spending(1000, 0))
    result = chat(config, "alice", "one\n/budget\n/quit\n")
    assert "total       2,000 of 10,000 tokens (20%)" in result.output


# the operator command ---------------------------------------------------------------------------------------------------------------------------

def test_the_operator_can_see_and_clear_a_users_count(project, config_path, monkeypatch):
    monkeypatch.setattr(cli_main, "run_turn", spending(1000, 0))
    chat(config_path, "alice", "one\n/quit\n")
    shown = runner.invoke(app, ["budget", "--user", "alice", "--config", str(config_path)])
    assert shown.exit_code == 0 and "2,000 tokens in" in shown.output
    cleared = runner.invoke(app, ["budget", "--user", "alice", "--clear", "--config", str(config_path)])
    assert cleared.exit_code == 0 and "Cleared 2,000 tokens" in cleared.output and "0 turn(s)" in cleared.output
    entries = AuditLog(project / "data" / "db.sqlite3").entries(action="budget_cleared")
    assert [entry["detail"] for entry in entries] == ["cleared by operator"]


def test_the_operator_command_names_an_unknown_user_and_exits_2(config_path):
    result = runner.invoke(app, ["budget", "--user", "zed", "--config", str(config_path)])
    assert result.exit_code == 2 and "Unknown user 'zed'" in result.output
