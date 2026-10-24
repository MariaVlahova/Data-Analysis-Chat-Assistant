"""The chat with the agent behind it (plan 07, CLI wiring): the real graph, guards and tools through the Typer app, with a scripted
model and the fake BigQuery client."""
import json
import re
from pathlib import Path

from typer.testing import CliRunner

from src.cli import main as cli_main
from src.cli.main import app
from src.cli.render import Renderer
from src.llm.routing import Step
from tests.agent_support import composer_reply, guard_reply, sql_reply

runner = CliRunner(env={"COLUMNS": "200"})


def chat(config_path: Path, user: str, text: str, *extra: str):
    return runner.invoke(app, ["chat", "--user", user, "--config", str(config_path), *extra], input=text)


def runs(project: Path) -> list[dict]:
    path = project / "data" / "runs.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def script_answer(gateway, body="Nike sold 1,200 items and Levi's 900."):
    gateway.queue(Step.INPUT_GUARD, guard_reply(standalone="Revenue by brand"))
    gateway.queue(Step.SQL_WRITE, sql_reply())
    gateway.queue(Step.COMPOSER, composer_reply(body, "Revenue by brand"))


def test_a_report_is_saved_then_deleted_with_one_confirmation_turn(project, config_path, scripted_gateway):
    script_answer(scripted_gateway)
    scripted_gateway.queue(Step.INPUT_GUARD, guard_reply("save_report"))
    scripted_gateway.queue(Step.INPUT_GUARD, guard_reply("delete_reports", selector={"kind": "text_match", "text": "revenue"}))
    result = chat(config_path, "alice", "revenue by brand\nsave that\ndelete the reports about revenue\ny\n/quit\n")
    assert result.exit_code == 0, result.output
    out = result.output
    assert 'Saved "Revenue by brand" on' in out
    assert "I found 1 report:" in out and re.search(r"1\W+Revenue by brand \(", out) and "Delete this report? (y/N)" in out  # Rich draws the list
    assert "Deleted 1 report. Say 'undo' within 30 days to bring it back." in out
    assert len(scripted_gateway.calls_for(Step.INPUT_GUARD)) == 3  # the "y" was the reply, not a question: no model call
    assert not re.search(r"r-[0-9a-f]{8}", out)  # report IDs are never shown
    rows = runs(project)
    assert [row["outcome"] for row in rows] == ["answered", "action_done", "answered", "action_done"]
    assert [row["turn_kind"] for row in rows] == ["simple", "tool", "tool", "tool"] and [row["turn_id"] for row in rows] == ["t-1", "t-2", "t-3", "t-4"]
    assert all(re.fullmatch(r"[0-9a-f]{12}", row["user_hash"]) for row in rows)


def test_an_unconfirmed_delete_changes_nothing_and_the_chat_goes_on(project, config_path, scripted_gateway):
    script_answer(scripted_gateway)
    scripted_gateway.queue(Step.INPUT_GUARD, guard_reply("save_report"))
    scripted_gateway.queue(Step.INPUT_GUARD, guard_reply("delete_reports", selector={"kind": "all", "text": None}))
    scripted_gateway.queue(Step.INPUT_GUARD, guard_reply("list_reports"))
    result = chat(config_path, "alice", "revenue by brand\nsave that\ndelete all my reports\nyes\nlist my reports\n/quit\n")
    assert result.exit_code == 0, result.output
    assert "Type DELETE 1 to confirm." in result.output and "Cancelled. Nothing was deleted." in result.output
    assert "You have 1 saved report:" in result.output  # the "yes" did not confirm a level 3 delete
    assert [row["outcome"] for row in runs(project)][-3:] == ["answered", "action_cancelled", "action_done"]


def test_an_out_of_scope_message_shows_the_not_possible_panel_with_a_trace_id(config_path, scripted_gateway):
    scripted_gateway.queue(Step.INPUT_GUARD, guard_reply("out_of_scope", in_scope=False))
    result = chat(config_path, "alice", "write a poem\n/quit\n")
    assert "Not possible" in result.output and "outside what I can help with" in result.output
    assert re.search(r"Trace ID: [0-9a-f]{8}", result.output) and result.exit_code == 0


def test_a_guard_refusal_names_what_kind_of_request_it_was(config_path, scripted_gateway):
    scripted_gateway.queue(Step.INPUT_GUARD, guard_reply(standalone="customer emails"))
    scripted_gateway.queue(Step.SQL_WRITE, sql_reply("SELECT email FROM users"))
    result = chat(config_path, "alice", "show customer emails\n/quit\n")
    assert "Not possible" in result.output and "personal details that could identify a customer" in result.output
    assert "email FROM" not in result.output and "SELECT" not in result.output  # never the SQL


def test_a_crash_inside_the_graph_is_a_friendly_message_and_the_chat_continues(project, config_path, scripted_gateway):
    # no scripted reply for the first question: the fake gateway raises inside the Input Guard
    result = chat(config_path, "alice", "first\n/quit\n")
    assert result.exit_code == 0
    assert "Something went wrong on my side" in result.output and re.search(r"Trace ID: [0-9a-f]{8}", result.output)
    assert "Traceback" not in result.output and "AssertionError" not in result.output
    (row,) = runs(project)
    assert row["outcome"] == "failed" and row["error_kinds"] == ["INTERNAL"]


def test_the_chat_keeps_going_after_a_failed_turn(config_path, scripted_gateway):
    script_answer(scripted_gateway)
    result = chat(config_path, "alice", "first\n/quit\n")  # the scripted replies are used by the first question
    assert "Nike sold 1,200 items" in result.output
    again = chat(config_path, "alice", "boom\nsecond\n/quit\n")
    assert len(re.findall(r"Trace ID: [0-9a-f]{8}", again.output)) == 2 and "Goodbye" in again.output  # both fail (nothing scripted), the loop lives


def test_the_chat_shows_progress_while_it_works_but_not_the_answer(config_path, scripted_gateway):
    script_answer(scripted_gateway, "Nike sold 1,200 items.")
    result = chat(config_path, "alice", "revenue by brand\n/quit\n")
    assert result.output.count("Nike sold 1,200 items.") == 1  # printed once, after the Output Guard


def test_the_trace_command_shows_every_step_of_a_turn(project, config_path, scripted_gateway):
    script_answer(scripted_gateway)
    result = chat(config_path, "alice", "revenue by brand\n/trace\n/quit\n")
    for step in ("input_guard", "context_assembly", "sql_loop", "guard.sql", "bq.dry_run", "bq.query", "guard.result", "evidence_check",
                 "composer", "numeric_check", "output_guard", "guard.output", "finish"):
        assert step in result.output, step
    assert "guard sql: " in result.output and "guard result: " in result.output and "guard output: pass" in result.output


def test_the_ceo_sees_all_brands_in_the_scope_line(project, config_path, scripted_gateway):
    script_answer(scripted_gateway)
    assert "Scope: all brands" in chat(config_path, "ceo", "revenue by brand\n/quit\n").output


def test_chaos_reaches_the_gateway_the_query_runner_and_the_sql_writer(config_path, monkeypatch):
    from src.llm.fake import FakeGateway

    seen = {}

    def fake_factory(settings, **kwargs):
        seen.update(kwargs)
        return FakeGateway()

    monkeypatch.setattr("src.llm.gateway.LLMGateway", fake_factory)
    session = cli_main.open_session(Renderer(), "alice", config_path, debug=False, conversation=None, chaos=cli_main.ChaosLevel.harsh, chaos_seed=5)
    try:
        engine = session.chaos_engine
        assert seen["chaos"] is engine and session.agent.deps.chaos is engine and session.agent.deps.runner._chaos is engine
        assert engine.seed == 5 and engine.level == "harsh"
    finally:
        session.db.close()


def test_without_chaos_nothing_is_injected(config_path, monkeypatch):
    from src.llm.fake import FakeGateway

    seen = {}
    monkeypatch.setattr("src.llm.gateway.LLMGateway", lambda settings, **kwargs: (seen.update(kwargs), FakeGateway())[1])
    session = cli_main.open_session(Renderer(), "alice", config_path, debug=False, conversation=None, chaos=None, chaos_seed=None)
    try:
        assert seen["chaos"] is None and session.agent.deps.chaos is None
    finally:
        session.db.close()


def test_a_session_has_one_agent_with_its_own_pseudonymizer_and_no_pseudonymizer_in_its_deps_fields_that_could_be_checkpointed(config_path, scripted_gateway):
    session = cli_main.open_session(Renderer(), "alice", config_path, debug=False, conversation=None, chaos=None, chaos_seed=None)
    other = cli_main.open_session(Renderer(), "alice", config_path, debug=False, conversation=None, chaos=None, chaos_seed=None)
    try:
        assert session.agent is not other.agent
        assert session.agent.deps.result_guard._pseudonymizer is not other.agent.deps.result_guard._pseudonymizer  # fake IDs are per session
    finally:
        session.db.close()
        other.db.close()
