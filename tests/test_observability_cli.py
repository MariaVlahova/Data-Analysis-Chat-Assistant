"""The chat and the commands with tracing wired in (plan 02 acceptance)."""
import json
import os
import re
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from src.cli import main as cli_main
from src.cli.main import app
from src.errors import AgentError, ErrorKind
from src.observability import event, guard_decision, span, turn_metrics
from tests.agent_support import echo_turn

runner = CliRunner(env={"COLUMNS": "200"})
CANARY = "canary@example.test"


def chat(config_path: Path, user: str, text: str = "/quit\n", *extra: str):
    return runner.invoke(app, ["chat", "--user", user, "--config", str(config_path), *extra], input=text)


def runs(project: Path) -> list[dict]:
    path = project / "data" / "runs.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def trace_ids(output: str) -> list[str]:
    return re.findall(r"Trace ID: ([0-9a-f]{8})", output)


def failing_chaos_turn(session, text, *, trace_id, progress):
    """A turn that fails the way a chaos run does: a BigQuery outage inside the SQL loop."""
    with span("input_guard", "step"):
        guard_decision("input", "pass", "IN-001")
    with span("sql_loop", "step"):
        with span("bq.query", "query", sql=f"SELECT * FROM t WHERE email = '{CANARY}'"):
            raise AgentError(ErrorKind.BQ_UNAVAILABLE, detail=f"chaos: injected outage for {CANARY}")


# acceptance: a failed chaos turn --------------------------------------------------------------------

def test_after_a_failed_chaos_turn_the_trace_shows_which_step_failed_and_why(project, config_path, monkeypatch):
    monkeypatch.setattr(cli_main, "run_turn", failing_chaos_turn)
    result = chat(config_path, "alice", "sales by brand\n/trace\n/quit\n", "--chaos", "mild")
    assert result.exit_code == 0, result.output

    (shown,) = set(trace_ids(result.output))  # the user got one trace ID, and /trace shows the same turn
    assert f"Trace {shown}" in result.output
    assert "bq.query" in result.output and "ERROR BQ_UNAVAILABLE" in result.output
    assert "BQ_UNAVAILABLE (AgentError): chaos: injected outage for [EMAIL_MASKED]" in result.output
    assert "guard input: pass" in result.output and "question: sales by brand" in result.output
    assert CANARY not in result.output

    (row,) = runs(project)  # runs.jsonl has the matching row
    assert row["trace_id"] == shown and row["outcome"] == "failed" and row["success"] is False
    assert row["error_kinds"] == ["BQ_UNAVAILABLE"] and row["chaos"] is True
    assert row["guard_blocks"]["input"] == 0 and row["conversation_id"].startswith("conv-") and row["turn_id"] == "t-1"
    assert (project / "data" / "traces" / f"{shown}.jsonl").is_file()


def test_the_operator_trace_command_shows_the_same_turn_later(project, config_path, monkeypatch):
    monkeypatch.setattr(cli_main, "run_turn", failing_chaos_turn)
    shown = trace_ids(chat(config_path, "alice", "boom\n/quit\n", "--chaos", "mild").output)[0]
    result = runner.invoke(app, ["trace", shown, "--config", str(config_path)])
    assert result.exit_code == 0 and "bq.query" in result.output and "ERROR BQ_UNAVAILABLE" in result.output
    raw = runner.invoke(app, ["trace", shown, "--raw", "--config", str(config_path)])
    assert {json.loads(line)["name"] for line in raw.output.splitlines()} == {"turn", "input_guard", "sql_loop", "bq.query"}
    last = runner.invoke(app, ["trace", "--config", str(config_path)])  # no ID: the last turn
    assert f"Trace {shown}" in last.output


def test_nothing_the_canary_touched_is_on_disk_after_the_failed_turn(project, config_path, monkeypatch):
    monkeypatch.setattr(cli_main, "run_turn", failing_chaos_turn)
    chat(config_path, "alice", f"does {CANARY} buy much?\n/quit\n")
    for path in (project / "data").rglob("*"):
        if path.is_file() and path.name not in ("db.sqlite3", "db.sqlite3-wal", "db.sqlite3-shm"):
            assert CANARY not in path.read_text(encoding="utf-8", errors="replace"), path.name


# the normal turn ------------------------------------------------------------------------------------

def test_every_question_leaves_a_trace_and_one_row_with_the_user_hash_only(project, config_path, monkeypatch):
    monkeypatch.setattr(cli_main, "run_turn", echo_turn)
    result = chat(config_path, "alice", "hello\nagain\n/quit\n")
    assert result.exit_code == 0
    rows = runs(project)
    assert [r["turn_id"] for r in rows] == ["t-1", "t-2"] and all(r["outcome"] == "answered" for r in rows)
    assert all(re.fullmatch(r"[0-9a-f]{12}", r["user_hash"]) for r in rows)
    for row in rows:
        text = (project / "data" / "traces" / f"{row['trace_id']}.jsonl").read_text(encoding="utf-8")
        assert "alice" not in text.lower() and row["user_hash"] in text
        assert "echo" in text  # the stub turn's own step


def test_slash_commands_and_blank_lines_do_not_count_as_turns(project, config_path):
    chat(config_path, "alice", "/help\n\n/metrics\n/trace\n/quit\n")
    assert runs(project) == []


def test_a_bare_slash_trace_shows_the_turn_just_asked_about(config_path, monkeypatch):
    monkeypatch.setattr(cli_main, "run_turn", echo_turn)
    result = chat(config_path, "alice", "what sold best\n/trace\n/quit\n")
    assert "question: what sold best" in result.output and "echo" in result.output


def test_slash_trace_with_an_unknown_id_says_so_and_the_chat_goes_on(config_path):
    result = chat(config_path, "alice", "/trace deadbeef\n/trace ../x\n/trace a b\n/trace --nope\n/quit\n")
    assert result.exit_code == 0 and "Traceback" not in result.output
    assert result.output.count("No trace found") == 2
    assert result.output.count("Usage: /trace [id] [--raw | --exchange]") == 2
    assert "Goodbye" in result.output


def test_slash_metrics_shows_totals_and_checks_its_arguments(config_path):
    result = chat(config_path, "alice", "one\ntwo\nthree\n/metrics\n/metrics --last 2\n/metrics --last x\n/metrics 5\n/quit\n")
    assert result.exit_code == 0
    assert "Metrics: 3 turn(s)" in result.output and "last 2 of 3 turns" in result.output
    assert result.output.count("Usage: /metrics [--last N]") == 2


def test_a_user_cannot_open_another_users_trace_but_the_ceo_can(project, config_path):
    alice_id = trace_ids_from_rows(chat(config_path, "alice", "private question\n/quit\n"), project)[0]
    bob = chat(config_path, "bob", f"/trace {alice_id}\n/quit\n")
    assert "No trace found" in bob.output and "private question" not in bob.output
    ceo = chat(config_path, "ceo", f"/trace {alice_id}\n/quit\n")
    assert "private question" in ceo.output


def trace_ids_from_rows(result, project) -> list[str]:
    assert result.exit_code == 0
    return [row["trace_id"] for row in runs(project)]


def test_a_failure_to_number_the_turn_still_leaves_a_stored_trace_for_the_id_the_user_sees(project, config_path, monkeypatch):
    # next_turn used to run before the trace existed: the user got a trace ID that was never stored.
    import sqlite3

    from src.storage.db import Database

    def locked(self, conv_id):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(Database, "next_turn", locked)
    result = chat(config_path, "alice", "hello\n/trace\n/quit\n")
    assert result.exit_code == 0
    (shown,) = set(trace_ids(result.output))
    assert f"Trace {shown}" in result.output and "No trace found" not in result.output
    assert "turn t-?" in result.output and "ERROR INTERNAL" in result.output
    (row,) = runs(project)
    assert row["trace_id"] == shown and row["outcome"] == "failed" and row["turn_id"] == "t-?"
    assert row["error_kinds"] == ["INTERNAL"]


def test_the_turn_id_in_the_trace_is_the_one_the_database_allocated(project, config_path):
    chat(config_path, "alice", "one\ntwo\n/quit\n")
    assert [row["turn_id"] for row in runs(project)] == ["t-1", "t-2"]
    for row in runs(project):
        text = (project / "data" / "traces" / f"{row['trace_id']}.jsonl").read_text(encoding="utf-8")
        assert f'"turn_id":"{row["turn_id"]}"' in text


def test_in_the_chat_metrics_cover_your_own_turns_and_the_ceo_sees_everyone(project, config_path, monkeypatch):
    real_run_turn = cli_main.run_turn

    def bob_leaks(session, text, *, trace_id, progress):
        if session.user.user_id == "bob":
            turn_metrics().add_pii_at_output(1)  # bob's turn has PII at output
            raise AgentError(ErrorKind.OUTPUT_BLOCKED)
        return real_run_turn(session, text, trace_id=trace_id, progress=progress)

    monkeypatch.setattr(cli_main, "run_turn", bob_leaks)
    chat(config_path, "bob", "bob asks\n/quit\n")
    chat(config_path, "alice", "alice asks\n/quit\n")
    bob_trace = next(row["trace_id"] for row in runs(project) if row["pii_at_output"])

    alice = chat(config_path, "alice", "/metrics\n/quit\n")
    assert "Metrics: 1 turn(s), your turns only" in alice.output
    assert "ALERT" not in alice.output and bob_trace not in alice.output
    ceo = chat(config_path, "ceo", "/metrics\n/quit\n")  # the CEO has no turns of their own and still sees all
    assert "Metrics: 2 turn(s), all users" in ceo.output and bob_trace in ceo.output


# the metrics command ----------------------------------------------------------------------------------

def test_the_metrics_command_reads_runs_jsonl(project, config_path):
    empty = runner.invoke(app, ["metrics", "--config", str(config_path)])
    assert empty.exit_code == 0 and "No turns recorded yet" in empty.output
    chat(config_path, "alice", "one\ntwo\n/quit\n")
    result = runner.invoke(app, ["metrics", "--config", str(config_path)])
    assert result.exit_code == 0 and "Metrics: 2 turn(s), all users" in result.output
    assert "of the 1 TiB free tier" in result.output
    assert "last 1 of 2 turns" in runner.invoke(app, ["metrics", "--last", "1", "--config", str(config_path)]).output
    assert runner.invoke(app, ["metrics", "--last", "0", "--config", str(config_path)]).exit_code == 2


def test_the_trace_command_exits_1_for_an_unknown_id(config_path):
    result = runner.invoke(app, ["trace", "deadbeef", "--config", str(config_path)])
    assert result.exit_code == 1 and "No trace found" in result.output and "Traceback" not in result.output


def test_trace_and_metrics_need_a_working_config(tmp_path):
    for command in (["trace"], ["metrics"]):
        result = runner.invoke(app, [*command, "--config", str(tmp_path / "nope.yaml")])
        assert result.exit_code == 2 and "not found" in result.output


def test_help_lists_the_new_commands_and_flag():
    assert "trace" in runner.invoke(app, ["--help"]).output and "metrics" in runner.invoke(app, ["--help"]).output
    assert "--debug-prompts" in runner.invoke(app, ["chat", "--help"]).output


# the PII alert -------------------------------------------------------------------------------------------

def test_pii_at_output_prints_one_warning_line_logs_critical_and_lands_in_the_metrics(project, config_path, monkeypatch):
    def blocked(session, text, *, trace_id, progress):
        turn_metrics().add_pii_at_output(1)  # what plan 06's Output Guard will do
        guard_decision("output", "block", "OUT-PII-001")
        raise AgentError(ErrorKind.OUTPUT_BLOCKED, detail="email pattern in the draft")

    monkeypatch.setattr(cli_main, "run_turn", blocked)
    result = chat(config_path, "alice", "show customer contacts\n/metrics\n/quit\n")
    assert result.exit_code == 0
    # once right after the turn, once at the top of /metrics (that line follows the prompt, so search, not startswith)
    assert result.output.count("ALERT: personal data was found in a draft answer") == 1
    assert result.output.count("ALERT: 1 PII finding(s) at output in 1 turn(s)") == 1
    (row,) = runs(project)
    assert row["pii_at_output"] == 1 and row["guard_blocks"]["output"] == 1 and row["outcome"] == "failed"
    log = (project / "data" / "logs" / "app.log").read_text(encoding="utf-8")
    assert "CRITICAL" in log and "pii_at_output=1" in log and row["trace_id"] in log


def test_a_clean_turn_prints_no_alert(config_path):
    assert "ALERT" not in chat(config_path, "alice", "hello\n/quit\n").output


# startup: retention and flags ------------------------------------------------------------------------------

def test_old_traces_are_purged_at_startup_and_recent_ones_stay(project, config_path):
    folder = project / "data" / "traces"
    folder.mkdir(parents=True)
    old, recent = folder / "01d00001.jsonl", folder / "0ecce001.jsonl"
    for path, age_days in ((old, 120), (recent, 5)):
        path.write_text("{}\n", encoding="utf-8")
        stamp = time.time() - age_days * 86400
        os.utime(path, (stamp, stamp))
    assert chat(config_path, "alice").exit_code == 0
    assert not old.exists() and recent.exists()


def test_the_retention_days_come_from_the_config(project, config_path):
    config_path.write_text("retention:\n  trace_days: 2\n", encoding="utf-8")
    folder = project / "data" / "traces"
    folder.mkdir(parents=True)
    path = folder / "01d00002.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    stamp = time.time() - 3 * 86400
    os.utime(path, (stamp, stamp))
    chat(config_path, "alice")
    assert not path.exists()


def test_debug_prompts_reaches_the_tracer_and_is_off_by_default(config_path, monkeypatch):
    seen = []

    def spy(session, text, *, trace_id, progress):
        seen.append(session.tracer.debug_prompts)
        return "ok"

    monkeypatch.setattr(cli_main, "run_turn", spy)
    chat(config_path, "alice", "a\n/quit\n")
    chat(config_path, "alice", "a\n/quit\n", "--debug-prompts")
    assert seen == [False, True]


def test_the_turn_gets_the_stored_trace_id(project, config_path, monkeypatch):
    seen = []

    def spy(session, text, *, trace_id, progress):
        seen.append(trace_id)
        event("seen", ok=True)
        return "ok"

    monkeypatch.setattr(cli_main, "run_turn", spy)
    chat(config_path, "alice", "a\n/quit\n")
    assert [row["trace_id"] for row in runs(project)] == seen


def test_a_persona_version_is_recorded_when_the_persona_is_on(project):
    (project / "config.yaml").write_text("persona:\n  enabled: true\n  default: default\n", encoding="utf-8")
    folder = project / "personas"
    folder.mkdir(exist_ok=True)  # the project fixture ships the default persona
    (folder / "default.yaml").write_text(
        "id: default\nversion: 1\ntone: Plain.\nsections: [Summary]\nmax_words: 100\n", encoding="utf-8"
    )
    chat(project / "config.yaml", "alice", "hello\n/quit\n")
    (row,) = runs(project)
    assert re.fullmatch(r"default@v1\+[0-9a-f]{8}", row["persona_version"])
