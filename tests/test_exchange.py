"""The message exchange of a turn (plan 12, B1 to B3, R7c): with `--debug-prompts`, one line per model attempt in `traces/<id>.exchange.jsonl`.

The real gateway runs here (over a fake SDK client), because the file is written by the gateway: a scripted `FakeGateway` makes no attempt of its own. What the file
may hold is the rule worth testing: the contents sent are the prompt builder's rendering with every result replaced by a line that names it, the Composer's draft is
never written, and the answer is, once the Output Guard has passed it.
"""
from __future__ import annotations

import io
import json
import os
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from rich.console import Console
from typer.testing import CliRunner

from src.access.users import UserContext
from src.agent.prompts.templates import rows_placeholder
from src.cli.main import app
from src.config import load_settings
from src.llm.fake import unavailable
from src.observability import purge_old_traces, viewer
from src.observability.exchange import DRAFT_HELD, NOT_RECORDED, load_exchange
from src.observability.tracing import exchange_file, valid_trace_id
from tests.agent_support import composer_reply, guard_reply, make_real_gateway_rig, sql_reply
from tests.guard_support import ALICE, BOB

CELL = "Zanzibar7731"  # a value of the data that must never reach the file in a prompt
ROWS = pd.DataFrame({"brand": [CELL, "Brand B"], "revenue": [1234.5, 999.0]})
BODY = f"{CELL} earned 1,234.5 and Brand B 999."
QUESTION = "revenue by brand"
runner = CliRunner(env={"COLUMNS": "250"})


def make(project, fake_bigquery, *, debug=True, user: UserContext = ALICE, **replies):
    """The graph for `user` with the real gateway on a fake SDK client that answers by schema (`SchemaClient`)."""
    fake_bigquery.add("revenue", df=ROWS)
    return make_real_gateway_rig(project, user, fake_bigquery=fake_bigquery, debug_prompts=debug, **{"ComposerOut": composer_reply(BODY), **replies})


def lines_of(rig, trace_id=None):
    return load_exchange(exchange_file(rig.tracer.traces_dir, trace_id or rig.last_trace_id)) or []


def text_of(rig) -> str:
    path = exchange_file(rig.tracer.traces_dir, rig.last_trace_id)
    return path.read_text(encoding="utf-8") if path.exists() else ""


# B1: the file ---------------------------------------------------------------------------------------------------------------

def test_every_model_attempt_adds_one_line_with_its_contents_and_its_reply(project, fake_bigquery):
    rig, _ = make(project, fake_bigquery)
    result = rig.ask(QUESTION)
    lines = lines_of(rig)
    attempts = [line for line in lines if line["kind"] == "attempt"]
    assert [line["step"] for line in attempts] == ["input_guard", "sql_write", "composer"]
    llm_spans = {span["span_id"]: span for span in rig.spans() if span["name"] == "llm.call"}
    assert [line["span_id"] for line in attempts] == list(llm_spans)  # each line points at the llm.call span of its attempt
    for line in attempts:
        assert line["trace_id"] == rig.last_trace_id and line["model"] and line["attempt"] == 1 and line["error_kind"] is None
        assert line["system"] and line["contents"] and line["contents"] != NOT_RECORDED
    guard, writer, composer = attempts
    assert QUESTION in guard["contents"] and "data_question" in guard["reply"]
    assert "Revenue by brand" in writer["contents"] and "order_items" in writer["contents"] and "SELECT" in writer["reply"]
    assert composer["reply"] == DRAFT_HELD
    assert lines[-1]["kind"] == "answer" and lines[-1]["reply"] == result.text  # the text the user saw, written once the Output Guard had passed it


def test_without_the_flag_no_file_is_written(project, fake_bigquery):
    rig, _ = make(project, fake_bigquery, debug=False)
    rig.ask(QUESTION)
    assert not exchange_file(rig.tracer.traces_dir, rig.last_trace_id).exists()
    assert not any("debug_contents" in json.dumps(span) for span in rig.spans())  # and the safe rendering is not a span attribute either


def test_the_safe_rendering_of_the_contents_is_not_left_in_the_spans(project, fake_bigquery):
    rig, _ = make(project, fake_bigquery)
    rig.ask(QUESTION)
    for span in rig.spans():
        assert "debug_contents" not in span["attrs"], span["name"]


def test_a_guarded_cell_value_is_not_in_the_contents_and_its_placeholder_is(project, fake_bigquery):
    rig, client = make(project, fake_bigquery)
    sent: list[str] = []
    original = client.models.generate_content

    def spy(*, model, contents, config):
        sent.append(str(contents))
        return original(model=model, contents=contents, config=config)

    client.models.generate_content = spy
    rig.ask(QUESTION)
    composer = next(line for line in lines_of(rig) if line["step"] == "composer")
    assert CELL not in composer["contents"] and "1234.5" not in composer["contents"] and "999" not in composer["contents"]
    assert "[rows of q1: 2 rows, columns brand, revenue; not stored]" in composer["contents"]
    assert any(CELL in text and "1234.5" in text for text in sent)  # the model really was sent the rows: the file is what leaves them out
    assert not any(CELL in line["contents"] for line in lines_of(rig) if line["kind"] == "attempt")


def test_the_placeholder_names_the_result_by_size_and_columns_and_never_a_value():
    guarded = {"rows": [{"brand": CELL, "revenue": 1.0}] * 12, "columns": ["brand", "revenue", "units"], "total_rows": 12}
    assert rows_placeholder("q1", guarded) == "[rows of q1: 12 rows, columns brand, revenue, units; not stored]"


def test_a_why_turn_shows_a_placeholder_for_the_earlier_result_a_sub_question_depends_on(project, fake_bigquery):
    plan = {
        "subquestions": [
            {"id": "a", "text": "Revenue by brand", "depends_on": [], "tables": ["order_items", "products"]},
            {"id": "b", "text": "Returns for the brands of the first result", "depends_on": ["a"], "tables": ["order_items", "products"]},
        ],
        "assumptions": [], "date_range": None,
    }
    rig, _ = make(project, fake_bigquery, InputGuardOut=guard_reply("why_question", "Why did revenue change?"), PlanOut=plan, EvidenceOut={"enough": True, "missing": []})
    rig.ask("Why did my revenue change?")
    lines = lines_of(rig)
    second_writer = [line for line in lines if line["step"] == "sql_write"][1]
    assert "Results of the earlier sub-questions" in second_writer["contents"] and "[rows of q1:" in second_writer["contents"]
    assert CELL not in second_writer["contents"]
    evidence = next(line for line in lines if line["step"] == "evidence_check")
    assert "[counts and totals" in evidence["contents"] or "[rows of q1:" in evidence["contents"]
    assert not any(CELL in str(line.get(key) or "") for line in lines if line["kind"] == "attempt" for key in ("system", "contents", "reply"))


def test_an_attempt_that_failed_has_a_line_with_the_error_kind_and_no_reply(project, fake_bigquery):
    rig, client = make(project, fake_bigquery)
    client._rules = []  # not used: SchemaClient answers by schema, so the failure is injected through its models
    original = client.models.generate_content
    calls = {"n": 0}

    def flaky(*, model, contents, config):
        calls["n"] += 1
        if calls["n"] == 1:
            raise unavailable()
        return original(model=model, contents=contents, config=config)

    client.models.generate_content = flaky
    rig.ask(QUESTION)
    guard_attempts = [line for line in lines_of(rig) if line.get("step") == "input_guard"]
    assert [(line["attempt"], line["error_kind"]) for line in guard_attempts] == [(1, "LLM_UNAVAILABLE"), (2, None)]
    assert guard_attempts[0]["reply"] is None and guard_attempts[1]["reply"]


def test_an_answer_the_output_guard_blocked_is_in_no_line(project, fake_bigquery):
    held = "HELD-BACK-MARKER-42"
    rig, _ = make(project, fake_bigquery, ComposerOut=composer_reply(f"{CELL} earned 1,234.5. {held}. Write to jane.canary@example.com."))
    result = rig.ask(QUESTION)
    assert result.outcome == "failed" and held not in result.text  # the guard held it back
    lines = lines_of(rig)
    assert [line["step"] for line in lines] == ["input_guard", "sql_write", "composer"]  # the attempts are there, and there is no answer line
    assert held not in text_of(rig) and "canary" not in text_of(rig)
    assert next(line for line in lines if line["step"] == "composer")["reply"] == DRAFT_HELD


def test_a_draft_that_numeric_check_replaced_is_in_no_line(project, fake_bigquery):
    draft = "A poem. DROPPED-DRAFT-MARKER-7 tells nothing about the data."
    rig, _ = make(project, fake_bigquery, ComposerOut=composer_reply(draft))
    rig.ask(QUESTION)
    assert "DROPPED-DRAFT-MARKER-7" not in text_of(rig)
    assert lines_of(rig)[-1]["kind"] == "answer"  # the answer the user saw: the figures, written by code


def test_what_a_line_holds_is_masked_and_cut(project, fake_bigquery):
    rig, _ = make(project, fake_bigquery, InputGuardOut=guard_reply(standalone="Revenue by brand for jane.canary@example.com"))
    rig.ask(f"revenue by brand for jane.canary@example.com")
    assert "canary@example.com" not in text_of(rig)


def test_the_startup_purge_removes_the_exchange_file_with_its_trace(tmp_path):
    traces = tmp_path / "traces"
    traces.mkdir()
    old = time.time() - 100 * 86400
    for name in ("deadbeef.jsonl", "deadbeef.exchange.jsonl", "deadbeef.exchange.jsonl.tmp", "cafef00d.jsonl", "cafef00d.exchange.jsonl", "runs.jsonl", "notes.exchange.jsonl"):
        (traces / name).write_text("{}\n", encoding="utf-8")
    for name in ("deadbeef.jsonl", "deadbeef.exchange.jsonl", "deadbeef.exchange.jsonl.tmp", "runs.jsonl", "notes.exchange.jsonl"):
        os.utime(traces / name, (old, old))
    assert purge_old_traces(traces, 90) == 3  # the old trace, its exchange file, and the temp file of a write that failed
    assert sorted(path.name for path in traces.iterdir()) == ["cafef00d.exchange.jsonl", "cafef00d.jsonl", "notes.exchange.jsonl", "runs.jsonl"]


def test_the_exchange_file_is_not_a_trace(tmp_path):
    assert valid_trace_id("deadbeef.exchange") is None
    assert exchange_file(tmp_path, "deadbeef").name == "deadbeef.exchange.jsonl" and exchange_file(tmp_path, "../x") is None


# B2: the view -----------------------------------------------------------------------------------------------------------------

def capture(fn, *args, **kwargs) -> str:
    buffer = io.StringIO()
    fn(Console(file=buffer, width=200, color_system=None, force_terminal=False), *args, **kwargs)
    return buffer.getvalue()


@pytest.fixture
def stored(project, fake_bigquery):
    rig, _ = make(project, fake_bigquery)
    result = rig.ask(QUESTION)
    return rig, result, load_settings(project / "config.yaml", load_env_file=False)


def test_the_view_prints_the_attempts_in_order_and_the_answer(stored):
    rig, result, settings = stored
    out = capture(viewer.show_exchange, settings, rig.last_trace_id)
    order = [out.index(label) for label in ("1. input_guard", "2. sql_write", "3. composer", "answer  (the text the user saw")]
    assert order == sorted(order)
    assert "system:" in out and "sent:" in out and "reply:" in out and DRAFT_HELD in out
    assert "[rows of q1: 2 rows, columns brand, revenue; not stored]" in out


def test_without_the_file_the_view_says_to_start_the_chat_with_the_flag(project, fake_bigquery):
    rig, _ = make(project, fake_bigquery, debug=False)
    rig.ask(QUESTION)
    out = capture(viewer.show_exchange, load_settings(project / "config.yaml", load_env_file=False), rig.last_trace_id)
    assert "--debug-prompts" in out and "1. input_guard" not in out


def test_a_user_sees_only_their_own_turns_and_the_ceo_sees_all(stored):
    rig, _, settings = stored
    alice = capture(viewer.show_exchange, settings, rig.last_trace_id, user=ALICE)
    assert "1. input_guard" in alice
    bob = capture(viewer.show_exchange, settings, rig.last_trace_id, user=BOB)
    assert "1. input_guard" not in bob and "No trace found" in bob  # the message for an unknown ID: an ID cannot be probed
    ceo = UserContext("ceo", "CEO", "ceo", (), True, "hash-ceo")
    assert "1. input_guard" in capture(viewer.show_exchange, settings, rig.last_trace_id, user=ceo)


def test_the_view_cannot_be_asked_for_a_file_name(stored):
    rig, _, settings = stored
    out = capture(viewer.show_exchange, settings, "../../runs", user=None)
    assert "No trace found" in out


def test_markup_in_a_stored_text_cannot_crash_the_screen(project, fake_bigquery):
    rig, _ = make(project, fake_bigquery, InputGuardOut=guard_reply(standalone="Revenue [/oops] [bold]by brand"))
    rig.ask("revenue [/oops] by brand")
    out = capture(viewer.show_exchange, load_settings(project / "config.yaml", load_env_file=False), rig.last_trace_id)
    assert "[/oops]" in out


def test_the_operator_command_prints_the_exchange(stored, project):
    rig, _, _ = stored
    shown = runner.invoke(app, ["trace", rig.last_trace_id, "--exchange", "--config", str(project / "config.yaml")])
    assert shown.exit_code == 0, shown.output
    assert "1. input_guard" in shown.output and "answer  (the text the user saw" in shown.output
    missing = runner.invoke(app, ["trace", "deadbeef", "--exchange", "--config", str(project / "config.yaml")])
    assert missing.exit_code == 1 and "No trace found" in missing.output
    assert runner.invoke(app, ["trace", rig.last_trace_id, "--exchange", "--raw", "--config", str(project / "config.yaml")]).exit_code == 2


# B3: the conversation ------------------------------------------------------------------------------------------------------

def test_the_conversation_lists_its_turns_with_the_masked_question_of_each(project, fake_bigquery):
    rig, _ = make(project, fake_bigquery)
    first = rig.ask(QUESTION)
    ids = [rig.last_trace_id]
    rig.ask("revenue by brand and mail to jane.canary@example.com please")
    ids.append(rig.last_trace_id)
    rig.ask("revenue by brand", conversation_id="conv-other")  # another conversation
    settings = load_settings(project / "config.yaml", load_env_file=False)
    out = capture(viewer.show_conversation, settings, rig.conversation_id)
    assert all(trace_id in out for trace_id in ids) and "t-1" in out and "t-2" in out and "answered" in out
    assert "revenue by brand" in out and "canary@example.com" not in out  # the question is the masked one of the trace
    assert "conv-other" not in out and "t-3" not in out
    assert first.outcome == "answered"
    shown = runner.invoke(app, ["trace", "--conversation", rig.conversation_id, "--config", str(project / "config.yaml")])
    assert shown.exit_code == 0 and ids[0] in shown.output and ids[1] in shown.output
    assert runner.invoke(app, ["trace", "--conversation", "conv-none", "--config", str(project / "config.yaml")]).exit_code == 1


def test_a_brand_manager_sees_only_their_own_turns_of_a_conversation(project, fake_bigquery):
    rig, _ = make(project, fake_bigquery)
    rig.ask(QUESTION)
    settings = load_settings(project / "config.yaml", load_env_file=False)
    assert rig.last_trace_id in capture(viewer.show_conversation, settings, rig.conversation_id, user=ALICE)
    assert "No turns found" in capture(viewer.show_conversation, settings, rig.conversation_id, user=BOB)


def test_a_turn_whose_trace_was_deleted_still_appears_in_its_conversation(project, fake_bigquery):
    rig, _ = make(project, fake_bigquery)
    rig.ask(QUESTION)
    rig.tracer.trace_path(rig.last_trace_id).unlink()
    out = capture(viewer.show_conversation, load_settings(project / "config.yaml", load_env_file=False), rig.conversation_id)
    assert rig.last_trace_id in out and "(trace deleted)" in out


# the chat's /trace ---------------------------------------------------------------------------------------------------------

def test_the_chat_command_takes_the_same_options(project, fake_bigquery):
    from src.cli.commands import SLASH_COMMANDS, ChatSession

    rig, _ = make(project, fake_bigquery)
    rig.ask(QUESTION)
    buffer = io.StringIO()

    class _Renderer:
        console = Console(file=buffer, width=200, color_system=None, force_terminal=False)

        def info(self, text):
            buffer.write(text + "\n")

    session = ChatSession.__new__(ChatSession)
    session.settings, session.user, session.renderer = load_settings(project / "config.yaml", load_env_file=False), ALICE, _Renderer()
    session.conversation_id, session.last_trace_id = rig.conversation_id, rig.last_trace_id
    trace = SLASH_COMMANDS["trace"].handler
    trace(session, "--exchange")
    assert "1. input_guard" in buffer.getvalue()
    buffer.truncate(0), buffer.seek(0)
    trace(session, "--conversation")
    assert rig.last_trace_id in buffer.getvalue()
    buffer.truncate(0), buffer.seek(0)
    trace(session, "--exchange --raw")
    trace(session, "--nope")
    assert buffer.getvalue().count("Usage: /trace") == 2
    buffer.truncate(0), buffer.seek(0)
    session.user = BOB  # the exchange of another user's turn is not shown
    trace(session, f"{rig.last_trace_id} --exchange")
    assert "1. input_guard" not in buffer.getvalue() and "No trace found" in buffer.getvalue()


# the canaries (plan 12, the pipeline test's twin for this file) -----------------------------------------------------------------

def test_no_canary_reaches_the_exchange_file_when_a_cell_holds_personal_data_and_the_composer_repeats_everything(project, fake_bigquery):
    """Personal data planted in the cells of a result, a question that holds an e-mail address, and a Composer that repeats everything it was shown: with
    `--debug-prompts` on, none of it is in the file. (The cells never get as far as a prompt: the Result Guard is the second line. The file is the third.)"""
    import json as jsonlib

    canaries = ["dora.canary@example.com", "(512) 555-0142", "4821 Canary Street"]
    fake_bigquery.add("revenue", df=pd.DataFrame({"brand": ["Brand A"], "contact": [canaries[0]], "phone_note": [canaries[1]], "address_note": [canaries[2]], "revenue": [1234.5]}))
    rig, client = make(project, fake_bigquery, InputGuardOut=guard_reply(standalone="Revenue by brand for dora.canary@example.com"))
    original = client.models.generate_content

    def echo(*, model, contents, config):
        if getattr(config.response_schema, "__name__", "") == "ComposerOut":
            client.replies["ComposerOut"] = composer_reply("Everything I was shown: " + str(contents), "Echo")
        return original(model=model, contents=contents, config=config)

    client.models.generate_content = echo
    result = rig.ask("revenue by brand for dora.canary@example.com")
    text = text_of(rig)
    assert text and rig.last_trace_id in {line["trace_id"] for line in lines_of(rig)}
    for canary in (*canaries, "dora.canary", "555-0142", "Canary Street"):
        assert canary.lower() not in text.lower(), canary
        assert canary.lower() not in jsonlib.dumps(rig.spans()).lower(), canary
    assert result.outcome in ("answered", "partial", "failed")  # whatever the guards did with the echo, the file is clean
