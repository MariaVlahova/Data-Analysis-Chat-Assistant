"""The confirmation pause survives a restart (plan 08, F28, NFR21): `SqliteSaver` on the app database, the chat that finds a
conversation where it stopped, `/new` that drops the wait, and the retention of checkpoints.

Most tests build the graph again from the same SQLite file (`restart()`): the old agent, store and saver are gone and nothing is
left in memory, which is what a new process is. The chat tests go through the Typer app, twice, with the same data folder.
"""
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest

from src.agent.checkpointer import open_checkpointer, purge_old_checkpoints
from src.llm.routing import Step
from tests.agent_support import guard_reply
from tests.reports_support import text_match
from tests.test_cli_agent import chat, runs, script_answer


# the graph (F28) -------------------------------------------------------------------------------------------------------------------------

def test_a_conversation_stopped_at_the_gate_is_still_stopped_after_a_restart(durable):
    durable.seed("Revenue Q1")
    asked = durable.delete(text_match("revenue"))
    assert asked.awaiting_confirmation
    durable.restart()
    snapshot = durable.state()
    assert snapshot.next == ("hitl_gate",) and snapshot.values["pending_action"]["count"] == 1  # get_state shows the pending interrupt
    assert durable.rig.agent.awaiting_confirmation(durable.rig.conversation_id)
    assert durable.rig.agent.pending_confirmation(durable.rig.conversation_id) == asked.text  # the same preview and prompt
    done = durable.reply("yes")
    assert done.outcome == "action_done" and done.text.startswith("Deleted 1 report.") and durable.titles() == []
    assert len(durable.rows("SELECT * FROM delete_batches")) == 1  # the delete happens once


def test_after_a_restart_a_no_cancels_and_nothing_is_deleted(durable):
    durable.seed("Revenue Q1")
    durable.delete(text_match("revenue"))
    durable.restart()
    assert durable.reply("no").outcome == "action_cancelled" and durable.titles() == ["Revenue Q1"]
    assert not durable.rig.agent.awaiting_confirmation(durable.rig.conversation_id)


def test_after_a_restart_a_report_added_in_between_still_makes_the_confirmation_stale(durable):
    """The hash key comes from the installation's salt, so it is the same in the new process: the comparison is a real one."""
    durable.seed("Revenue Q1")
    durable.delete(text_match("revenue"))
    durable.restart()
    durable.seed("Revenue Q2")
    stale = durable.reply("yes")
    assert stale.awaiting_confirmation and "I found 2 reports:" in stale.text and len(durable.titles()) == 2
    assert durable.reply("yes").outcome == "action_done"


def test_after_a_restart_the_last_answer_can_still_be_saved(durable):
    import pandas as pd

    from tests.agent_support import composer_reply, sql_reply

    durable.fake_bigquery.add("revenue", df=pd.DataFrame({"brand": ["Brand A"], "revenue": [1234.5]}))
    durable.rig.gateway.queue(Step.INPUT_GUARD, guard_reply(standalone="Revenue by brand"))
    durable.rig.gateway.queue(Step.SQL_WRITE, sql_reply())
    durable.rig.gateway.queue(Step.COMPOSER, composer_reply("Brand A earned 1,234.5.", "Revenue by brand"))
    assert durable.rig.ask("revenue by brand").outcome == "answered"
    durable.restart()  # the history and the last answer are in the checkpoint
    assert durable.say("save_report").text.startswith('Saved "Revenue by brand" on ')
    assert "Brand A earned 1,234.5." in durable.rows("SELECT body FROM saved_reports")[0]["body"]


def test_a_conversation_that_is_not_waiting_has_nothing_pending(durable):
    agent = durable.rig.agent
    assert agent.pending_confirmation(durable.rig.conversation_id) is None and not agent.awaiting_confirmation("conv-never-seen")
    durable.say("list_reports")
    durable.restart()
    assert durable.rig.agent.pending_confirmation(durable.rig.conversation_id) is None


def test_the_wait_of_one_conversation_is_not_the_wait_of_another(durable):
    durable.seed("Revenue Q1")
    durable.delete(text_match("revenue"))
    durable.restart()
    assert durable.rig.agent.pending_confirmation("conv-another") is None
    durable.rig.gateway.queue(Step.INPUT_GUARD, guard_reply("list_reports"))
    other = durable.rig.ask("list my reports", conversation_id="conv-another")
    assert other.outcome == "action_done" and durable.rig.agent.awaiting_confirmation(durable.rig.conversation_id)


def sql(path, statement, *args):
    """One statement on a connection that is closed again (an open one keeps the file locked on Windows and is a ResourceWarning).
    Writes are committed. Returns the rows as tuples."""
    with closing(sqlite3.connect(path)) as conn:
        rows = conn.execute(statement, args).fetchall()
        conn.commit()
        return rows


def test_the_checkpoint_holds_no_report_id_and_no_raw_rows(durable):
    durable.seed("Revenue Q1")
    ids = [row["report_id"] for row in durable.rows("SELECT report_id FROM saved_reports")]
    durable.delete(text_match("revenue"))
    blobs = [bytes(row[0] or b"") for table, column in (("checkpoints", "checkpoint"), ("writes", "value"))
             for row in sql(durable.settings.db_path, f"SELECT {column} FROM {table}")]
    assert blobs and not any(re.search(rb"r-[0-9a-f]{8}", blob) for blob in blobs)
    assert not any(report_id.encode() in blob for report_id in ids for blob in blobs)
    assert any(b"Revenue Q1" in blob for blob in blobs)  # the preview is there: the user sees those titles anyway


# the wait is dropped, not carried out ------------------------------------------------------------------------------------------------------

def test_abandoning_a_wait_deletes_nothing_and_frees_the_conversation(durable):
    durable.seed("Revenue Q1")
    durable.delete(text_match("revenue"))
    agent = durable.rig.agent
    assert agent.abandon_pending(durable.rig.conversation_id) is True
    assert not agent.awaiting_confirmation(durable.rig.conversation_id) and agent.pending_confirmation(durable.rig.conversation_id) is None
    assert durable.titles() == ["Revenue Q1"] and durable.rows("SELECT * FROM delete_batches") == []
    [row] = durable.audit("delete_cancelled")
    assert row["outcome"] == "abandoned" and row["count"] == 1 and len(row["target_hash"]) == 64
    assert agent.abandon_pending(durable.rig.conversation_id) is False  # nothing left to drop
    assert "Revenue Q1" in durable.say("list_reports").text  # the next line is a new question, not a reply


def test_an_abandoned_wait_cannot_be_confirmed_after_a_restart(durable):
    durable.seed("Revenue Q1")
    durable.delete(text_match("revenue"))
    durable.rig.agent.abandon_pending(durable.rig.conversation_id)
    durable.restart()
    assert not durable.rig.agent.awaiting_confirmation(durable.rig.conversation_id)
    durable.rig.gateway.queue(Step.INPUT_GUARD, guard_reply("list_reports"))
    assert durable.rig.ask("yes").outcome == "action_done" and durable.titles() == ["Revenue Q1"]  # "yes" is just a message now


# retention of checkpoints (NFR21) ------------------------------------------------------------------------------------------------------------

def test_checkpoints_of_a_conversation_idle_for_more_than_90_days_are_deleted(durable):
    durable.rig.gateway.queue(Step.INPUT_GUARD, guard_reply("list_reports"))
    durable.rig.ask("list my reports", conversation_id="conv-old")
    durable.say("list_reports")  # the current conversation
    long_ago = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat(timespec="seconds")
    durable.execute("UPDATE conversations SET last_active_at = ? WHERE id = 'conv-old'", long_ago)
    days = durable.settings.retention.checkpoint_days
    assert purge_old_checkpoints(durable.saver, days) == 1
    assert purge_old_checkpoints(durable.saver, days) == 0  # nothing left to purge
    assert durable.state("conv-old").values == {} and durable.state().values  # the old one is gone, the current one is not
    threads = {row[0] for row in sql(durable.settings.db_path, "SELECT DISTINCT thread_id FROM checkpoints")}
    assert threads == {durable.rig.conversation_id}


def test_the_retention_is_the_configured_number_of_days(project, fake_bigquery):
    from tests.reports_support import ReportsEnv

    env = ReportsEnv(project, fake_bigquery, checkpoints=True, config="retention:\n  checkpoint_days: 5\n")
    try:
        env.say("list_reports")
        env.execute("UPDATE conversations SET last_active_at = ?", (datetime.now(timezone.utc) - timedelta(days=6)).isoformat(timespec="seconds"))
        assert purge_old_checkpoints(env.saver, env.settings.retention.checkpoint_days) == 1
    finally:
        env.close()


def test_the_saved_reports_of_a_purged_conversation_stay(durable):
    durable.seed("Revenue Q1")
    durable.say("list_reports")
    durable.execute("UPDATE conversations SET last_active_at = '2020-01-01T00:00:00+00:00'")
    assert purge_old_checkpoints(durable.saver, 90) == 1
    assert durable.titles() == ["Revenue Q1"]


def test_open_checkpointer_needs_no_setup_and_survives_being_opened_twice(durable):
    second = open_checkpointer(durable.settings)
    try:
        assert second.get_tuple({"configurable": {"thread_id": "conv-nothing"}}) is None
    finally:
        second.conn.close()


# the chat (F28) ---------------------------------------------------------------------------------------------------------------------------------

def stop_at_the_gate(config_path, scripted_gateway):
    """A first chat that saves a report, asks to delete everything and ends (end of input) while the preview is on the screen."""
    script_answer(scripted_gateway)
    scripted_gateway.queue(Step.INPUT_GUARD, guard_reply("save_report"))
    scripted_gateway.queue(Step.INPUT_GUARD, guard_reply("delete_reports", selector={"kind": "all", "text": None}))
    first = chat(config_path, "alice", "revenue by brand\nsave that\ndelete all my reports\n")
    assert first.exit_code == 0 and "Type DELETE 1 to confirm." in first.output, first.output
    return re.search(r"Conversation (conv-[0-9a-f]{8})", first.output).group(1)


def audit_rows(project):
    with closing(sqlite3.connect(project / "data" / "db.sqlite3")) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute("SELECT * FROM audit_log ORDER BY seq")]


def reports_left(project):
    return sql(project / "data" / "db.sqlite3", "SELECT COUNT(*) FROM saved_reports WHERE deleted_at IS NULL")[0][0]


def test_the_next_chat_shows_the_waiting_preview_and_the_reply_confirms_it(project, config_path, scripted_gateway):
    conversation = stop_at_the_gate(config_path, scripted_gateway)
    calls_before = len(scripted_gateway.calls)
    second = chat(config_path, "alice", "DELETE 1\n/quit\n")  # a new process: nothing is left in memory
    out = second.output
    assert second.exit_code == 0, out
    assert f"Conversation {conversation}." in out  # the same conversation, not a new one
    assert "waiting for your confirmation" in out and "I found 1 report:" in out and "Type DELETE 1 to confirm." in out
    assert "Deleted 1 report. Say 'undo' within 30 days to bring it back." in out
    assert len(scripted_gateway.calls) == calls_before  # the reply made no model call
    assert reports_left(project) == 0
    assert [row["action"] for row in audit_rows(project)] == ["save", "delete_requested", "delete_confirmed", "delete_executed"]


def test_the_turn_numbers_carry_on_after_the_restart(project, config_path, scripted_gateway):
    stop_at_the_gate(config_path, scripted_gateway)
    chat(config_path, "alice", "DELETE 1\n/quit\n")
    rows = runs(project)
    assert [row["turn_id"] for row in rows] == ["t-1", "t-2", "t-3", "t-4"] and rows[-1]["outcome"] == "action_done"


def test_a_wrong_reply_in_the_next_chat_cancels_and_deletes_nothing(project, config_path, scripted_gateway):
    stop_at_the_gate(config_path, scripted_gateway)
    second = chat(config_path, "alice", "yes\n/quit\n")  # level 3 needs "DELETE 1"
    assert "Cancelled. Nothing was deleted." in second.output and reports_left(project) == 1
    third = chat(config_path, "alice", "/quit\n")
    assert "waiting for your confirmation" not in third.output  # it was answered, so it is not shown a third time


def test_a_conversation_that_is_not_waiting_is_not_resumed(project, config_path, scripted_gateway):
    scripted_gateway.queue(Step.INPUT_GUARD, guard_reply("list_reports"))
    first = chat(config_path, "alice", "list my reports\n/quit\n")
    second = chat(config_path, "alice", "/quit\n")
    one = re.search(r"Conversation (conv-[0-9a-f]{8})", first.output).group(1)
    two = re.search(r"Conversation (conv-[0-9a-f]{8})", second.output).group(1)
    assert one != two and "waiting for your confirmation" not in second.output


def test_new_drops_the_wait_without_doing_the_action(project, config_path, scripted_gateway):
    conversation = stop_at_the_gate(config_path, scripted_gateway)
    scripted_gateway.queue(Step.INPUT_GUARD, guard_reply("list_reports"))
    second = chat(config_path, "alice", "/new\nlist my reports\n/quit\n")
    out = second.output
    assert "waiting for your confirmation" in out and "was cancelled. Nothing was changed." in out
    assert "You have 1 saved report:" in out and reports_left(project) == 1
    assert [(row["action"], row["outcome"]) for row in audit_rows(project)][-1] == ("delete_cancelled", "abandoned")
    third = chat(config_path, "alice", "/quit\n")
    assert "waiting for your confirmation" not in third.output and f"Conversation {conversation}." not in third.output


def test_an_old_conversation_asked_for_by_id_resumes_too(project, config_path, scripted_gateway):
    conversation = stop_at_the_gate(config_path, scripted_gateway)
    second = chat(config_path, "alice", "no\n/quit\n", "--conversation", conversation)
    assert "waiting for your confirmation" in second.output and "Cancelled. Nothing was deleted." in second.output


def test_another_user_cannot_resume_the_conversation(project, config_path, scripted_gateway):
    conversation = stop_at_the_gate(config_path, scripted_gateway)
    result = chat(config_path, "bob", "DELETE 1\n/quit\n", "--conversation", conversation)
    assert result.exit_code == 2 and reports_left(project) == 1


def test_a_chat_removes_the_checkpoints_of_conversations_idle_past_the_retention(project, config_path, scripted_gateway):
    scripted_gateway.queue(Step.INPUT_GUARD, guard_reply("list_reports"))
    first = chat(config_path, "alice", "list my reports\n/quit\n")
    old = re.search(r"Conversation (conv-[0-9a-f]{8})", first.output).group(1)

    def checkpoint_threads():
        return {row[0] for row in sql(project / "data" / "db.sqlite3", "SELECT DISTINCT thread_id FROM checkpoints")}

    assert old in checkpoint_threads()
    sql(project / "data" / "db.sqlite3", "UPDATE conversations SET last_active_at = ? WHERE id = ?",
        (datetime.now(timezone.utc) - timedelta(days=91)).isoformat(timespec="seconds"), old)
    chat(config_path, "alice", "/quit\n")
    assert old not in checkpoint_threads()


def test_a_chat_removes_reports_deleted_past_their_undo_window(project, config_path, scripted_gateway):
    stop_at_the_gate(config_path, scripted_gateway)
    chat(config_path, "alice", "DELETE 1\n/quit\n")
    database = project / "data" / "db.sqlite3"
    sql(database, "UPDATE saved_reports SET purge_after = '2020-01-01T00:00:00+00:00'")
    chat(config_path, "alice", "/quit\n")
    assert sql(database, "SELECT COUNT(*) FROM saved_reports")[0][0] == 0
    assert sql(database, "SELECT action, count FROM audit_log ORDER BY seq DESC LIMIT 1")[0] == ("purge", 1)


# a run that was cut off before the gate asked anything ------------------------------------------------------------------------------------

def cut_off_before_the_prompt(env, monkeypatch):
    """The process dies after `prepare_action` and before the gate has asked: Ctrl+C at that moment."""

    def killed(value):
        raise KeyboardInterrupt

    env.seed("Revenue Q1")
    with monkeypatch.context() as patched:
        patched.setattr("src.agent.nodes.tools.interrupt", killed)
        env.rig.gateway.queue(Step.INPUT_GUARD, guard_reply("delete_reports", selector=text_match("revenue")))
        with pytest.raises(KeyboardInterrupt):
            env.rig.ask("delete the revenue report")
    env.restart()


def test_a_checkpoint_that_stopped_before_the_gate_asked_is_not_waiting_for_a_reply(durable, monkeypatch):
    cut_off_before_the_prompt(durable, monkeypatch)
    snapshot = durable.state()
    assert snapshot.next == ("hitl_gate",) and not any(task.interrupts for task in snapshot.tasks)  # the gate is next, nothing was asked
    agent, conversation = durable.rig.agent, durable.rig.conversation_id
    assert agent.pending_confirmation(conversation) is None and agent.awaiting_confirmation(conversation) is False  # the two agree
    assert agent.abandon_pending(conversation) is False


def test_the_first_line_after_a_cut_off_run_is_a_new_question_not_a_blind_reply(durable, monkeypatch):
    cut_off_before_the_prompt(durable, monkeypatch)
    durable.rig.gateway.queue(Step.INPUT_GUARD, guard_reply("list_reports"))
    result = durable.rig.ask("DELETE 1")  # typed without ever seeing a prompt: it is not the reply to one
    assert result.outcome == "action_done" and "Revenue Q1" in result.text and not result.awaiting_confirmation
    assert durable.titles() == ["Revenue Q1"] and durable.rows("SELECT * FROM delete_batches") == []
    assert durable.state().next == ()  # the cut-off run is closed: its gate does not run later with another turn's state
    assert [row["outcome"] for row in durable.audit("delete_cancelled")] == ["abandoned"]  # the request had been audited, so its end is too


def test_a_chat_does_not_resume_a_conversation_whose_gate_never_asked(project, config_path, scripted_gateway, monkeypatch):
    """The chat shows nothing for it and starts a new conversation, as it does for any conversation that is not waiting."""
    script_answer(scripted_gateway)
    scripted_gateway.queue(Step.INPUT_GUARD, guard_reply("save_report"))
    scripted_gateway.queue(Step.INPUT_GUARD, guard_reply("delete_reports", selector={"kind": "all", "text": None}))

    def killed(value):
        raise KeyboardInterrupt

    with monkeypatch.context() as patched:
        patched.setattr("src.agent.nodes.tools.interrupt", killed)
        first = chat(config_path, "alice", "revenue by brand\nsave that\ndelete all my reports\n/quit\n")
    old = re.search(r"Conversation (conv-[0-9a-f]{8})", first.output).group(1)
    second = chat(config_path, "alice", "/quit\n")
    assert "waiting for your confirmation" not in second.output and f"Conversation {old}." not in second.output
    assert reports_left(project) == 1


# what the user typed is masked before it is stored (review finding: checkpoints are kept on disk for 90 days) --------------------------------------

def stored_bytes(env):
    """Every checkpoint and every pending write in the app database, as bytes."""
    return [bytes(row[0] or b"") for statement in ("SELECT checkpoint FROM checkpoints", "SELECT value FROM writes")
            for row in sql(env.settings.db_path, statement)]


def test_what_the_user_typed_is_masked_before_it_reaches_the_checkpoint_the_model_and_the_history(durable):
    import pandas as pd

    from tests.agent_support import composer_reply, sql_reply

    durable.fake_bigquery.add("revenue", df=pd.DataFrame({"brand": ["Brand A"], "revenue": [1234.5]}))
    durable.rig.gateway.queue(Step.INPUT_GUARD, guard_reply(standalone="Revenue by brand, to send to jane.roe@example.test"))
    durable.rig.gateway.queue(Step.SQL_WRITE, sql_reply())
    durable.rig.gateway.queue(Step.COMPOSER, composer_reply("Brand A earned 1,234.5.", "Revenue by brand"))
    assert durable.rig.ask("revenue by brand, e-mail it to jane.roe@example.test please").outcome == "answered"
    values = durable.state().values
    assert "[EMAIL_MASKED]" in values["question"] and "[EMAIL_MASKED]" in values["standalone_question"]
    assert "[EMAIL_MASKED]" in values["history"][0]["user"]
    blobs = stored_bytes(durable)
    assert blobs and not any(b"jane.roe" in blob for blob in blobs)  # nowhere in the checkpoints, nor in the writes between steps
    for call in durable.rig.gateway.calls:
        assert "jane.roe" not in str(call.contents) + call.system  # and the model never saw it either
    assert values["last_answer"]["title"] == "Revenue by brand"


def test_a_question_that_was_masked_never_becomes_a_saved_title(durable):
    import pandas as pd

    from tests.agent_support import composer_reply, sql_reply

    durable.fake_bigquery.add("revenue", df=pd.DataFrame({"brand": ["Brand A"], "revenue": [1234.5]}))
    durable.rig.gateway.queue(Step.INPUT_GUARD, guard_reply(standalone="Revenue by brand for jane.roe@example.test"))
    durable.rig.gateway.queue(Step.SQL_WRITE, sql_reply())
    durable.rig.gateway.queue(Step.COMPOSER, composer_reply("Brand A earned 1,234.5."))  # no title from the Composer
    durable.rig.ask("revenue by brand for jane.roe@example.test")
    assert durable.state().values["last_answer"]["title"] == "Saved answer"  # not "Revenue by brand for [EMAIL_MASKED]"


def test_the_reply_to_a_confirmation_is_masked_too_and_still_compares_exactly(durable):
    durable.seed("Revenue Q1")
    durable.delete(text_match("revenue"))
    assert durable.reply("yes, jane.roe@example.test said so").outcome == "action_cancelled"  # more than "yes": cancels
    assert durable.titles() == ["Revenue Q1"]
    assert not any(b"jane.roe" in blob for blob in stored_bytes(durable))
    durable.delete(text_match("revenue"))
    assert durable.reply(" yes ").outcome == "action_done"  # masking leaves an ordinary reply as it was typed


# labels shown by an earlier process are still not IDs (review finding) ------------------------------------------------------------------------------------

LABEL_SQL = "SELECT SUM(oi.sale_price) AS revenue FROM order_items oi WHERE oi.user_id = {number}"


def ask_about_customer(env, number):
    env.rig.gateway.queue(Step.INPUT_GUARD, guard_reply(standalone=f"What did customer {number} buy?"))
    env.rig.gateway.queue(Step.SQL_WRITE, {"sql": LABEL_SQL.format(number=number), "interpretation": "Revenue of one customer."})
    return env.rig.ask("what did that customer buy?")


def show_a_label(env, number, *, filler=0):
    """An answered turn whose answer shows `Customer #<number>`, after `filler` words (a label deep in a long answer is not in the
    600 characters the history keeps)."""
    import pandas as pd

    from tests.agent_support import composer_reply, sql_reply

    env.fake_bigquery.add("revenue", df=pd.DataFrame({"customer": [f"Customer #{number}"], "revenue": [500.0]}))
    env.rig.gateway.queue(Step.INPUT_GUARD, guard_reply(standalone="Top customer by revenue"))
    env.rig.gateway.queue(Step.SQL_WRITE, sql_reply())
    env.rig.gateway.queue(Step.COMPOSER, composer_reply("Some background first. " * filler + f"Customer #{number} spent 500.", "Top customer"))
    answered = env.rig.ask("who is my top customer?")
    assert answered.outcome == "answered" and f"Customer #{number}" in answered.text
    return answered


def test_a_label_the_user_was_shown_is_kept_in_the_checkpoint_as_a_number(durable):
    show_a_label(durable, 48213)
    assert durable.state().values["shown_labels"] == ["48213"]  # only the number: a label is a random number and stands for nothing real
    show_a_label(durable, 48213)
    assert durable.state().values["shown_labels"] == ["48213"]  # not twice


def test_a_label_shown_before_a_restart_is_refused_as_an_id_after_it(durable):
    show_a_label(durable, 48213)
    durable.restart()  # the new process has a new pseudonymizer that never issued 48213
    result = ask_about_customer(durable, 48213)
    assert result.outcome == "refused" and result.error.rule_id == "SESSION_LABEL"  # the rule that keeps a label's digits out of a query
    assert durable.rig.queries == []  # and the query never reached BigQuery


def test_a_label_deep_in_a_long_answer_or_in_an_older_one_is_refused_too(durable):
    """The history keeps 600 characters of the last 6 answers; what the user was shown is more than that."""
    show_a_label(durable, 48213, filler=60)  # far beyond the first 600 characters
    assert "48213" not in durable.state().values["history"][-1]["assistant"]
    for number in (51234, 52345, 53456, 54567, 55678, 56789, 57890):  # and more answers than the history holds
        show_a_label(durable, number)
    durable.restart()
    for number in (48213, 51234):
        result = ask_about_customer(durable, number)
        assert result.outcome == "refused" and result.error.rule_id == "SESSION_LABEL", number
    assert durable.rig.queries == []


def test_a_number_that_was_never_shown_as_a_label_is_not_refused(durable):
    """The control: the same question about another number runs, so the refusal above is the label rule and nothing else."""
    import pandas as pd

    show_a_label(durable, 48213)
    durable.restart()
    durable.fake_bigquery.add("user_id", df=pd.DataFrame({"revenue": [10.0]}))
    ask_about_customer(durable, 77001)
    assert len(durable.rig.queries) == 1


def test_a_label_shown_before_a_restart_is_never_issued_again_to_another_customer(durable):
    show_a_label(durable, 48213)
    durable.restart()
    durable.rig.gateway.queue(Step.INPUT_GUARD, guard_reply("list_reports"))
    durable.rig.ask("list my reports")  # the first turn of the new process reads the numbers from the checkpoint
    guard = durable.rig.agent.deps.result_guard
    assert guard.references_session_label("WHERE u.id = 48213") and not guard.references_session_label("WHERE u.id = 48214")


# /new: the wait is closed first, then the audit row says so (review finding) -------------------------------------------------------------------------------

def test_if_the_wait_cannot_be_closed_no_row_says_it_was(durable, monkeypatch):
    durable.seed("Revenue Q1")
    durable.delete(text_match("revenue"))
    agent, conversation = durable.rig.agent, durable.rig.conversation_id

    def refused(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(agent.graph, "update_state", refused)
    with pytest.raises(RuntimeError):
        agent.abandon_pending(conversation)
    assert durable.audit("delete_cancelled") == []  # nothing claims "abandoned" about a request that is still waiting
    assert agent.awaiting_confirmation(conversation) and durable.reply("yes").outcome == "action_done"  # it is still there to answer


def test_if_the_row_cannot_be_written_the_wait_is_still_closed_and_the_failure_is_logged(durable, caplog):
    durable.seed("Revenue Q1")
    durable.delete(text_match("revenue"))
    durable.execute("CREATE TRIGGER no_cancel_audit BEFORE INSERT ON audit_log WHEN NEW.action = 'delete_cancelled' "
                    "BEGIN SELECT RAISE(ABORT, 'forced audit failure'); END")
    with caplog.at_level("ERROR", logger="src"):
        assert durable.rig.agent.abandon_pending(durable.rig.conversation_id) is True
    assert not durable.rig.agent.awaiting_confirmation(durable.rig.conversation_id) and durable.titles() == ["Revenue Q1"]
    assert "could not record the abandoned" in caplog.text and "forced audit failure" not in caplog.text  # where, never the exception text


# a pending confirmation is found among the recent conversations (review finding) ----------------------------------------------------------------------------

def test_a_waiting_conversation_is_found_even_when_another_one_was_used_after_it(project, config_path, scripted_gateway):
    older = chat_conversation(chat(config_path, "alice", "/quit\n"))  # a conversation that is not waiting for anything
    waiting = stop_at_the_gate(config_path, scripted_gateway)  # a second one, which ends at the gate
    # The user then used the first one again (`--conversation`), so it is the most recently active of the two.
    sql(project / "data" / "db.sqlite3", "UPDATE conversations SET last_active_at = ? WHERE id = ?",
        (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds"), older)
    third = chat(config_path, "alice", "no\n/quit\n")
    assert f"Conversation {waiting}." in third.output and "waiting for your confirmation" in third.output
    assert "Cancelled. Nothing was deleted." in third.output and reports_left(project) == 1


def chat_conversation(result):
    return re.search(r"Conversation (conv-[0-9a-f]{8})", result.output).group(1)


# startup cleanup is optional (review finding) -------------------------------------------------------------------------------------------------------------

def test_a_chat_starts_when_the_purge_of_deleted_reports_fails(project, config_path, scripted_gateway, monkeypatch):
    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("src.reports.store.purge_expired", broken)
    result = chat(config_path, "alice", "/quit\n")
    assert result.exit_code == 0 and "Conversation conv-" in result.output


def test_a_chat_starts_when_the_purge_of_old_checkpoints_fails(project, config_path, scripted_gateway, monkeypatch):
    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("src.agent.checkpointer.purge_old_checkpoints", broken)
    result = chat(config_path, "alice", "/quit\n")
    assert result.exit_code == 0 and "Conversation conv-" in result.output


def test_the_purge_of_checkpoints_looks_only_at_idle_conversations_that_still_have_some(durable):
    """Conversation rows are never deleted, so most old ones have no checkpoints left: they cost nothing."""
    for number in range(30):
        durable.have_conversation(f"conv-gone{number:04d}")
    durable.execute("UPDATE conversations SET last_active_at = '2020-01-01T00:00:00+00:00' WHERE id LIKE 'conv-gone%'")
    durable.say("list_reports")
    calls = []
    real = durable.saver.delete_thread
    durable.saver.delete_thread = lambda thread_id: (calls.append(thread_id), real(thread_id))[1]
    assert purge_old_checkpoints(durable.saver, 90) == 0 and calls == []


# hidden characters cannot hide personal data from the mask (review finding) ------------------------------------------------------------------------------

def test_typed_text_is_cleaned_before_it_is_masked():
    from src import pii
    from src.agent.nodes.input_guard import typed_text

    assert typed_text("send it to jane.roe\u200b@example.test") == "send it to [EMAIL_MASKED]"  # a zero-width space inside the address
    assert typed_text("jane\u00ad.roe@exa\u2060mple.test") == "[EMAIL_MASKED]"  # a soft hyphen and a word joiner
    assert typed_text("call +1 555\u00ad1234567") == pii.mask("call +1 5551234567")  # the same text as without the hidden character
    assert typed_text("Revenue by brand") == "Revenue by brand" and typed_text("") == ""


def test_hidden_characters_in_a_question_do_not_put_personal_data_in_the_checkpoint_or_a_prompt(durable):
    import pandas as pd

    from tests.agent_support import composer_reply, sql_reply

    durable.fake_bigquery.add("revenue", df=pd.DataFrame({"brand": ["Brand A"], "revenue": [1234.5]}))
    durable.rig.gateway.queue(Step.INPUT_GUARD, guard_reply(standalone="Revenue by brand for jane.roe\u200b@example.test"))
    durable.rig.gateway.queue(Step.SQL_WRITE, sql_reply())
    durable.rig.gateway.queue(Step.COMPOSER, composer_reply("Brand A earned 1,234.5.", "Revenue by brand"))
    assert durable.rig.ask("revenue by brand, send it to jane.roe\u200b@example.test").outcome == "answered"
    values = durable.state().values
    assert "[EMAIL_MASKED]" in values["question"] and "[EMAIL_MASKED]" in values["standalone_question"]
    assert not any(b"jane.roe" in blob or b"example.test" in blob for blob in stored_bytes(durable))
    for call in durable.rig.gateway.calls:
        assert "jane.roe" not in str(call.contents) + call.system


def test_a_confirmation_typed_with_a_hidden_character_is_the_same_reply(durable):
    durable.seed("Revenue Q1")
    durable.delete(text_match("revenue"))
    assert durable.reply("ye\u200bs").outcome == "action_done"  # what looks like "yes" to the user is "yes"


# an abandoned row is about a request that was shown and never confirmed (review finding) -----------------------------------------------------------------

def cut_off_in(env, monkeypatch, tool_name, *, after_the_effect):
    """The process dies inside `execute_action`: before the tool runs, or just after it has committed."""
    import src.agent.nodes.tools as nodes

    real = nodes._call

    def dying(tool, args, ctx):
        if tool.manifest.name != tool_name:
            return real(tool, args, ctx)
        if after_the_effect:
            real(tool, args, ctx)
        raise KeyboardInterrupt

    monkeypatch.setattr(nodes, "_call", dying)


def test_a_run_cut_off_inside_a_tool_that_is_not_gated_is_closed_without_an_abandoned_row(durable, monkeypatch):
    cut_off_in(durable, monkeypatch, "saved_reports.list", after_the_effect=False)
    durable.rig.gateway.queue(Step.INPUT_GUARD, guard_reply("list_reports"))
    with pytest.raises(KeyboardInterrupt):
        durable.rig.ask("list my reports")
    assert durable.state().next == ("execute_action",)
    monkeypatch.undo()
    durable.restart()
    durable.rig.gateway.queue(Step.INPUT_GUARD, guard_reply("list_reports"))
    assert durable.rig.ask("list my reports").outcome == "action_done"
    assert durable.audit("delete_cancelled") == [] and durable.state().next == ()  # never a request for a confirmation, so never "abandoned"


def test_a_run_cut_off_after_the_effect_committed_does_not_add_a_cancelled_row_to_an_executed_one(durable, monkeypatch):
    durable.seed("Revenue Q1")
    durable.delete(text_match("revenue"))
    cut_off_in(durable, monkeypatch, "saved_reports.delete", after_the_effect=True)
    with pytest.raises(KeyboardInterrupt):
        durable.reply("yes")  # the delete committed, then the process died before the checkpoint moved on
    monkeypatch.undo()
    assert [row["outcome"] for row in durable.audit("delete_executed")] == ["ok"] and durable.titles() == []
    durable.restart()
    durable.rig.gateway.queue(Step.INPUT_GUARD, guard_reply("list_reports"))
    assert durable.rig.ask("list my reports").outcome == "action_done"
    assert durable.audit("delete_cancelled") == []  # the trail does not say that a delete was both executed and cancelled
    assert [row["action"] for row in durable.audit()] == ["save", "delete_requested", "delete_confirmed", "delete_executed"]


# the checkpoint of an active conversation does not grow with every turn (review finding) ------------------------------------------------------------------

def checkpoint_rows(env, thread=None):
    thread = thread or env.rig.conversation_id
    return (
        sql(env.settings.db_path, "SELECT COUNT(*) FROM checkpoints WHERE thread_id = ?", thread)[0][0],
        sql(env.settings.db_path, "SELECT COUNT(DISTINCT checkpoint_id) FROM writes WHERE thread_id = ?", thread)[0][0],
    )


def test_only_the_newest_checkpoint_of_a_conversation_is_kept_after_each_turn(durable):
    for _ in range(4):
        durable.say("list_reports")
        checkpoints, writes = checkpoint_rows(durable)
        assert checkpoints == 1 and writes <= 1
    assert durable.state().values["history"]  # and it is the whole state: the history is in it


def test_a_pause_at_the_gate_survives_the_pruning_and_a_restart(durable):
    durable.seed("Revenue Q1")
    for _ in range(3):
        durable.say("list_reports")
    asked = durable.delete(text_match("revenue"))
    assert checkpoint_rows(durable)[0] == 1
    durable.restart()
    assert durable.rig.agent.pending_confirmation(durable.rig.conversation_id) == asked.text
    assert durable.reply("yes").outcome == "action_done" and checkpoint_rows(durable)[0] == 1


def test_the_conversations_are_pruned_one_at_a_time(durable):
    durable.say("list_reports")
    durable.rig.gateway.queue(Step.INPUT_GUARD, guard_reply("list_reports"))
    durable.rig.ask("list my reports", conversation_id="conv-other")
    durable.rig.gateway.queue(Step.INPUT_GUARD, guard_reply("list_reports"))
    durable.rig.ask("list my reports", conversation_id="conv-other")
    assert checkpoint_rows(durable)[0] == 1 and checkpoint_rows(durable, "conv-other")[0] == 1


def test_a_failure_to_prune_is_logged_and_does_not_fail_the_turn(durable, monkeypatch, caplog):
    def broken(saver, thread_id):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("src.agent.runtime.prune_thread", broken)
    with caplog.at_level("ERROR", logger="src"):
        assert durable.say("list_reports").outcome == "action_done"
    assert "could not prune" in caplog.text and "database is locked" not in caplog.text


def test_the_in_memory_checkpointer_of_the_tests_keeps_every_step(reports):
    """Pruning is for the saver on disk. The tests that search every checkpoint for a leak keep their full history."""
    reports.say("list_reports")
    thread = {"configurable": {"thread_id": reports.rig.conversation_id}}
    assert len(list(reports.rig.agent.graph.get_state_history(thread))) > 3


# the search for a waiting conversation is one query and is optional (review finding) ------------------------------------------------------------------------

def test_the_chat_asks_only_the_conversations_that_have_a_pause_on_file(project, config_path, scripted_gateway, monkeypatch):
    from src.agent.runtime import AgentRuntime

    waiting = stop_at_the_gate(config_path, scripted_gateway)
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(timespec="seconds")
    for number in range(25):  # newer than the waiting one and with no checkpoint: the common case, each chat start makes one
        sql(project / "data" / "db.sqlite3", "INSERT INTO conversations (id, user_id, created_at, last_active_at) VALUES (?, 'alice', ?, ?)",
            f"conv-empty{number:03d}", future, future)
    asked = []
    real = AgentRuntime.pending_confirmation
    monkeypatch.setattr(AgentRuntime, "pending_confirmation", lambda self, conversation_id: (asked.append(conversation_id), real(self, conversation_id))[1])
    third = chat(config_path, "alice", "no\n/quit\n")
    assert f"Conversation {waiting}." in third.output and "waiting for your confirmation" in third.output
    assert set(asked) == {waiting}  # not one of the 25 empty conversations was loaded


def test_waiting_conversations_are_the_users_own_newest_first(durable):
    from src.agent.checkpointer import waiting_conversations

    durable.seed("Revenue Q1")
    durable.delete(text_match("revenue"))
    assert waiting_conversations(durable.saver, "alice", 20) == [durable.rig.conversation_id]
    assert waiting_conversations(durable.saver, "bob", 20) == [] and waiting_conversations(durable.saver, "alice", 0) == []
    durable.reply("no")  # answered: the pause is no longer the newest checkpoint's, and pruning removed the old one
    assert waiting_conversations(durable.saver, "alice", 20) == []


def test_the_pause_channel_is_the_one_langgraph_uses(durable):
    """`waiting_conversations` names the channel of a pause. If a LangGraph release renames it, this fails instead of the chat
    quietly never finding a waiting conversation."""
    durable.seed("Revenue Q1")
    durable.delete(text_match("revenue"))
    channels = {row[0] for row in sql(durable.settings.db_path, "SELECT DISTINCT channel FROM writes")}
    from src.agent.checkpointer import INTERRUPT_CHANNEL

    assert INTERRUPT_CHANNEL in channels


def test_a_checkpoint_that_cannot_be_read_does_not_stop_the_chat(project, config_path, scripted_gateway, monkeypatch):
    from src.agent.runtime import AgentRuntime

    stop_at_the_gate(config_path, scripted_gateway)

    def unreadable(self, conversation_id):
        raise sqlite3.DatabaseError("malformed")

    monkeypatch.setattr(AgentRuntime, "pending_confirmation", unreadable)
    result = chat(config_path, "alice", "/quit\n")
    assert result.exit_code == 0 and "Conversation conv-" in result.output  # a new conversation, the old one is still there for --conversation


def test_a_failing_search_for_waiting_conversations_does_not_stop_the_chat(project, config_path, scripted_gateway, monkeypatch):
    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("src.agent.checkpointer.waiting_conversations", broken)
    result = chat(config_path, "alice", "/quit\n")
    assert result.exit_code == 0 and "Conversation conv-" in result.output


# what the session opened is closed whatever fails (review finding) --------------------------------------------------------------------------------------------

def test_the_session_closes_the_database_even_if_the_checkpoint_connection_would_not(project, config_path, scripted_gateway, monkeypatch):
    from src.agent import checkpointer
    from src.cli.main import open_session
    from src.cli.render import Renderer

    session = open_session(Renderer(), "alice", config_path, debug=False, chaos=None, chaos_seed=None, conversation=None)
    real = checkpointer.close_checkpointer

    def boom(saver):
        raise RuntimeError("cannot close")

    monkeypatch.setattr(checkpointer, "close_checkpointer", boom)
    with pytest.raises(RuntimeError):
        session.close()
    assert session.db._conn is None  # the database is closed all the same
    real(session.checkpointer)


def test_what_open_session_opened_is_closed_when_it_fails_late(project, config_path, scripted_gateway, monkeypatch):
    from src.agent import checkpointer
    from src.observability import Tracer

    closed = []
    real = checkpointer.close_checkpointer
    monkeypatch.setattr(checkpointer, "close_checkpointer", lambda saver: (closed.append(saver), real(saver)))

    def broken(*args, **kwargs):
        raise RuntimeError("tracer")

    monkeypatch.setattr(Tracer, "from_settings", staticmethod(broken))
    assert chat(config_path, "alice", "/quit\n").exit_code == 1
    assert len(closed) == 1  # the checkpoint connection was not left open behind the failure
