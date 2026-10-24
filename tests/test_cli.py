import re
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from src.cli import main as cli_main
from src.cli.main import app
from src.errors import AgentError, ErrorKind
from src.llm.routing import Step
from tests.agent_support import composer_reply, echo_turn, guard_reply, sql_reply

runner = CliRunner(env={"COLUMNS": "200"})  # wide terminal, so Rich never wraps a line inside an assertion


def chat(config_path: Path, user: str, text: str = "/quit\n", *extra: str):
    return runner.invoke(app, ["chat", "--user", user, "--config", str(config_path), *extra], input=text)


def conversations(project: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(project / "data" / "db.sqlite3")
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM conversations ORDER BY created_at").fetchall()
    finally:
        conn.close()


def test_help_lists_the_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "chat" in result.output and "doctor" in result.output
    chat_help = runner.invoke(app, ["chat", "--help"])
    assert chat_help.exit_code == 0
    for flag in ("--user", "--chaos", "--chaos-seed", "--conversation", "--config", "--debug"):
        assert flag in chat_help.output


def test_missing_user_option_is_a_usage_error():
    assert runner.invoke(app, ["chat"]).exit_code == 2


def test_bad_chaos_value_is_a_usage_error(config_path):
    assert chat(config_path, "alice", "/quit\n", "--chaos", "apocalyptic").exit_code == 2


def test_quit_exits_zero_and_creates_a_conversation(project, config_path):
    result = chat(config_path, "alice")
    assert result.exit_code == 0, result.output
    assert "Hi Alice" in result.output and "Brand A, Brand B" in result.output
    assert "Goodbye" in result.output
    rows = conversations(project)
    assert len(rows) == 1 and rows[0]["user_id"] == "alice" and rows[0]["id"].startswith("conv-")


def test_end_of_input_quits_cleanly(config_path):
    result = chat(config_path, "alice", "")
    assert result.exit_code == 0
    assert "Goodbye" in result.output


def test_ceo_banner_says_all_brands(config_path):
    result = chat(config_path, "ceo")
    assert result.exit_code == 0
    assert "all brands" in result.output


def test_unknown_user_exits_2_with_one_clear_sentence_and_the_known_names(config_path):
    result = chat(config_path, "zed")
    assert result.exit_code == 2
    assert "Unknown user 'zed'" in result.output
    assert "alice, bob, ceo, nobrands" in result.output
    assert "Traceback" not in result.output


def test_user_without_brands_exits_2_and_never_starts_a_chat(project, config_path):
    result = chat(config_path, "nobrands", "what are my sales?\n/quit\n")
    assert result.exit_code == 2
    assert "No brands are assigned to this user" in result.output
    assert "How I read your question" not in result.output and "Something went wrong" not in result.output  # no turn ran
    assert not (project / "data" / "db.sqlite3").exists()  # nothing was created for them


def test_missing_users_file_blocks_the_chat(project, config_path):
    (project / "users.yaml").unlink()
    result = chat(config_path, "alice")
    assert result.exit_code == 2
    assert "user list could not be read" in result.output


def test_bad_config_exits_2_naming_file_and_key(project, config_path):
    config_path.write_text("loop:\n  max_replans: 3\n", encoding="utf-8")
    result = chat(config_path, "alice")
    assert result.exit_code == 2
    assert "config.yaml line 2" in result.output and "loop.max_replans" in result.output


def test_missing_config_exits_2(tmp_path):
    result = chat(tmp_path / "nope.yaml", "alice")
    assert result.exit_code == 2
    assert "not found" in result.output


def script_one_answer(gateway, body: str, standalone: str = "revenue by brand") -> None:
    """The model replies of one simple question. The default fake dataset answers any `GROUP BY p.brand` query with
    Nike 1,200 and Levi's 900 (tests/fake_bigquery.py)."""
    gateway.queue(Step.INPUT_GUARD, guard_reply(standalone=standalone))
    gateway.queue(Step.SQL_WRITE, sql_reply())
    gateway.queue(Step.COMPOSER, composer_reply(body))


def test_a_turn_runs_the_agent_and_prints_the_answer_once(config_path, scripted_gateway):
    script_one_answer(scripted_gateway, "Nike sold 1,200 items and Levi's 900.")
    result = chat(config_path, "alice", "hello there\n/quit\n")
    assert result.exit_code == 0, result.output
    assert result.output.count("Nike sold 1,200 items") == 1
    assert "How I read your question" in result.output and "Scope: your brands: Brand A, Brand B" in result.output


def test_text_that_looks_like_markup_cannot_crash_the_screen(config_path, scripted_gateway):
    script_one_answer(scripted_gateway, "You asked about [/oops] [bold]x and Nike sold 1,200 items.", standalone="[/oops] [bold]x")
    result = chat(config_path, "alice", "[/oops] [bold]x\n/quit\n")
    assert result.exit_code == 0
    assert "[/oops]" in result.output and "Something went wrong" not in result.output


def test_empty_lines_are_ignored(config_path):
    result = chat(config_path, "alice", "\n   \n/quit\n")
    assert result.exit_code == 0
    assert "How I read your question" not in result.output and "Something went wrong" not in result.output


def test_an_exception_inside_a_turn_shows_a_friendly_message_and_the_loop_continues(project, config_path, monkeypatch):
    def flaky(session, text, *, trace_id, progress):
        if text == "boom":
            raise RuntimeError("secret detail: password=hunter2")
        return f"fine: {text}"

    monkeypatch.setattr(cli_main, "run_turn", flaky)
    result = chat(config_path, "alice", "boom\nsecond question\n/quit\n")

    assert result.exit_code == 0, result.output
    assert "Something went wrong on my side" in result.output
    assert re.search(r"Trace ID: [0-9a-f]{8}", result.output)
    assert "Traceback" not in result.output
    assert "hunter2" not in result.output and "RuntimeError" not in result.output
    assert "fine: second question" in result.output  # the loop carried on
    assert "Goodbye" in result.output
    # the details are in the log file, not on the screen
    log = (project / "data" / "logs" / "app.log").read_text(encoding="utf-8")
    assert "RuntimeError" in log and "turn crashed" in log
    assert "hunter2" not in log and "secret detail" not in log  # the exception text can carry data: never logged


def test_debug_shows_the_traceback_on_screen(config_path, monkeypatch):
    def broken(session, text, *, trace_id, progress):
        raise RuntimeError("visible only with debug")

    monkeypatch.setattr(cli_main, "run_turn", broken)
    result = chat(config_path, "alice", "boom\n/quit\n", "--debug")
    assert result.exit_code == 0
    assert "Traceback" in result.output and "visible only with debug" in result.output


def test_an_agent_error_uses_its_own_message_and_a_refusal_is_not_shown_as_a_failure(config_path, monkeypatch):
    def refuse(session, text, *, trace_id, progress):
        if text == "poem":
            raise AgentError(ErrorKind.OUT_OF_SCOPE)
        raise AgentError(ErrorKind.BQ_UNAVAILABLE)

    monkeypatch.setattr(cli_main, "run_turn", refuse)
    result = chat(config_path, "alice", "poem\nsales?\n/quit\n")
    assert result.exit_code == 0
    assert "Not possible" in result.output and "outside what I can help with" in result.output
    assert "Something went wrong" in result.output and "data warehouse is not responding" in result.output
    assert result.output.count("Trace ID:") == 2


def test_ctrl_c_during_a_turn_quits_with_a_goodbye(config_path, monkeypatch):
    def interrupted(session, text, *, trace_id, progress):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_main, "run_turn", interrupted)
    result = chat(config_path, "alice", "go\nnever reached\n")
    assert result.exit_code == 0
    assert "Goodbye" in result.output
    assert "never reached" not in result.output


def test_slash_help_and_unknown_slash_command_print_the_help(config_path):
    result = chat(config_path, "alice", "/help\n/nonsense\n/quit\n")
    assert result.exit_code == 0
    assert result.output.count("Commands:") == 2
    for command in ("/help", "/quit", "/new", "/trace", "/metrics"):
        assert command in result.output


def test_slash_new_starts_a_new_conversation_and_resets_the_turn_counter(project, config_path, monkeypatch):
    monkeypatch.setattr(cli_main, "run_turn", echo_turn)
    result = chat(config_path, "alice", "one\n/new\ntwo\n/quit\n")
    assert result.exit_code == 0
    assert "Started a new conversation" in result.output
    assert result.output.count("t-1") == 2 and "t-2" not in result.output
    assert len(conversations(project)) == 2


def test_resuming_a_conversation_by_id(project, config_path):
    first = chat(config_path, "alice")
    conv_id = conversations(project)[0]["id"]
    resumed = chat(config_path, "alice", "/quit\n", "--conversation", conv_id)
    assert first.exit_code == 0 and resumed.exit_code == 0
    assert conv_id in resumed.output
    assert len(conversations(project)) == 1  # no new row


def test_a_conversation_cannot_be_resumed_by_another_user(project, config_path):
    chat(config_path, "alice")
    conv_id = conversations(project)[0]["id"]
    result = chat(config_path, "bob", "/quit\n", "--conversation", conv_id)
    assert result.exit_code == 2
    assert "was not found for user 'bob'" in result.output
    assert chat(config_path, "alice", "/quit\n", "--conversation", "conv-00000000").exit_code == 2


def test_the_console_shows_no_log_lines_without_debug(config_path):
    result = chat(config_path, "alice", "hi\n/quit\n")
    assert not re.search(r"\b(INFO|DEBUG|WARNING)\b", result.output)


def test_chaos_flags_are_accepted(config_path):
    result = chat(config_path, "alice", "/quit\n", "--chaos", "mild", "--chaos-seed", "7")
    assert result.exit_code == 0


# static persona ---------------------------------------------------------------------------------

@pytest.fixture
def persona_project(project):
    (project / "config.yaml").write_text("persona:\n  enabled: true\n  default: default\n", encoding="utf-8")
    folder = project / "personas"
    folder.mkdir()
    (folder / "default.yaml").write_text(
        "id: default\nversion: 1\ntone: Plain.\nsections: [Summary]\nmax_words: 100\n", encoding="utf-8"
    )
    return project


def test_persona_is_off_by_default_and_has_no_commands(config_path):
    result = chat(config_path, "alice", "hello\n/persona\n/style length=short\n/quit\n")
    assert result.exit_code == 0
    assert "Persona:" not in result.output
    assert "/persona" not in result.output and "/style" not in result.output  # unknown commands print the help


def test_persona_label_with_version_is_shown_and_follows_edits_without_restart(persona_project, monkeypatch):
    config = persona_project / "config.yaml"
    path = persona_project / "personas" / "default.yaml"
    labels = []

    def spy(session, text, *, trace_id, progress):
        if text == "edit":
            path.write_text("id: default\nversion: 2\ntone: Brand new tone.\nmax_words: 100\n", encoding="utf-8")
        labels.append(session.personas.get(session.persona_name).label)
        return labels[-1]

    monkeypatch.setattr(cli_main, "run_turn", spy)
    result = chat(config, "alice", "one\nedit\nthree\n/quit\n")
    assert result.exit_code == 0
    assert labels[0].startswith("default@v1+")
    assert labels[1].startswith("default@v2+") and labels[1] == labels[2]
    assert labels[0] != labels[1]


def test_a_broken_default_persona_is_a_startup_problem(persona_project):
    (persona_project / "personas" / "default.yaml").write_text("id: default\nversion: 0\n", encoding="utf-8")
    result = chat(persona_project / "config.yaml", "alice", "/quit\n")
    assert result.exit_code == 2
    assert "default.yaml" in result.output


def test_a_persona_file_broken_mid_chat_does_not_block_the_turn(persona_project, monkeypatch, scripted_gateway):
    real_run_turn = cli_main.run_turn

    def corrupt_then_answer(session, text, *, trace_id, progress):
        if text == "break":
            (persona_project / "personas" / "default.yaml").write_text("id: default\nversion: 0\n", encoding="utf-8")
        return real_run_turn(session, text, trace_id=trace_id, progress=progress)

    monkeypatch.setattr(cli_main, "run_turn", corrupt_then_answer)
    script_one_answer(scripted_gateway, "Nike sold 1,200 items.")
    script_one_answer(scripted_gateway, "Levi's sold 900 items.")
    result = chat(persona_project / "config.yaml", "alice", "break\nnext\n/quit\n")
    assert result.exit_code == 0
    assert "Nike sold 1,200 items" in result.output  # the file was broken before this turn ran: carried on without a persona
    assert "Levi's sold 900 items" in result.output
    assert "Something went wrong" not in result.output
    composer_calls = scripted_gateway.calls_for(Step.COMPOSER)
    assert len(composer_calls) == 2 and all("Company persona" not in call.system for call in composer_calls)
    log = (persona_project / "data" / "logs" / "app.log").read_text(encoding="utf-8")
    assert "the persona is not available" in log


# regressions from the code review --------------------------------------------------------------

def test_turn_ids_carry_on_when_a_conversation_is_resumed(project, config_path, monkeypatch):
    monkeypatch.setattr(cli_main, "run_turn", echo_turn)
    first = chat(config_path, "alice", "one\ntwo\n/quit\n")
    assert first.exit_code == 0 and "t-2" in first.output
    conv_id = conversations(project)[0]["id"]

    resumed = chat(config_path, "alice", "three\n/quit\n", "--conversation", conv_id)
    assert resumed.exit_code == 0
    assert "t-3" in resumed.output and "t-1" not in resumed.output
    assert conversations(project)[0]["turn_count"] == 3


def test_a_failed_turn_still_uses_up_its_number(project, config_path, monkeypatch):
    def fail_then_answer(session, text, *, trace_id, progress):
        if text == "boom":
            raise RuntimeError("x")
        return session.turn_id

    monkeypatch.setattr(cli_main, "run_turn", fail_then_answer)
    result = chat(config_path, "alice", "boom\nnext\n/quit\n")
    assert result.exit_code == 0
    assert "t-2" in result.output  # the crashed turn was t-1, so IDs in traces never repeat


def test_a_crash_before_the_chat_opens_is_friendly_and_does_not_claim_the_session_is_open(project, config_path, monkeypatch):
    def broken(settings, path=None):
        raise RuntimeError("secret: password=hunter2")

    monkeypatch.setattr(cli_main, "load_users", broken)
    result = chat(config_path, "alice")
    assert result.exit_code == 1
    assert "had to close" in result.output and re.search(r"Trace ID: [0-9a-f]{8}", result.output)
    assert "session is still open" not in result.output
    for leak in ("Traceback", "hunter2", "RuntimeError", ".py"):
        assert leak not in result.output
    log = (project / "data" / "logs" / "app.log").read_text(encoding="utf-8")
    assert "chat crashed" in log and "RuntimeError" in log and "hunter2" not in log


def test_a_crash_before_logging_exists_shows_no_traceback(config_path, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("boom before logging")

    monkeypatch.setattr(cli_main, "load_settings", broken)
    result = chat(config_path, "alice")
    assert result.exit_code == 1
    assert "Traceback" not in result.output and "boom before logging" not in result.output
    assert "Trace ID:" in result.output


def test_the_trace_id_an_error_already_carries_is_the_one_shown_and_logged(project, config_path, monkeypatch):
    def raises(session, text, *, trace_id, progress):
        raise AgentError(ErrorKind.TOOL_FAILED, trace_id="aaaa1111")

    monkeypatch.setattr(cli_main, "run_turn", raises)
    result = chat(config_path, "alice", "go\n/quit\n")
    assert "Trace ID: aaaa1111" in result.output
    assert "trace_id=aaaa1111" in (project / "data" / "logs" / "app.log").read_text(encoding="utf-8")


def test_a_chat_leaves_the_root_logger_and_the_app_logger_as_it_found_them(config_path):
    import logging

    root_level, root_handlers = logging.getLogger().level, list(logging.getLogger().handlers)
    chat(config_path, "alice", "hi\n/quit\n", "--debug")
    assert logging.getLogger().level == root_level and logging.getLogger().handlers == root_handlers
    assert logging.getLogger("src").level == logging.NOTSET
    assert not [h for h in logging.getLogger("src").handlers if getattr(h, "_opsfleet_handler", False)]


def test_a_utf16_config_is_a_startup_problem_with_a_fix(config_path):
    config_path.write_bytes("app: {}\n".encode("utf-16"))
    result = chat(config_path, "alice")
    assert result.exit_code == 2
    assert "not UTF-8" in result.output and "Fix:" in result.output
    assert "Traceback" not in result.output


def test_a_utf16_users_file_blocks_the_chat_as_an_unreadable_user_list(project, config_path):
    (project / "users.yaml").write_bytes("users: {}\n".encode("utf-16"))
    result = chat(config_path, "alice")
    assert result.exit_code == 2
    assert "user list could not be read" in result.output and "Traceback" not in result.output
