"""A retry or a resume never repeats an effect (plan 08, F27, decision 3.10, Q23, and the loop-engineering checklist's missing test).

The key of a request holds the turn ID, so the same request has one effect however often it arrives, and a later turn that asks
for the same thing is a new request. These tests try to make an effect happen twice: the same key twice, a kill and rebuild of
the graph, a replay after an undo, and eight threads at once.
"""
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import pytest

from src.llm.routing import Step
from src.reports import idempotency, messages
from src.tools.base import LastAnswer
from tests.agent_support import composer_reply, guard_reply, sql_reply
from tests.guard_support import ALICE, BOB
from tests.reports_support import text_match
from tests.test_reports_store import ctx, delete, report_ids, tool


# (a) save twice with the same key ---------------------------------------------------------------------------------------------------

def test_save_twice_with_the_same_key_is_one_report_and_one_audit_row(reports):
    context = ctx(last=LastAnswer("Revenue Q1", "the text"), key="same-key")
    first = tool(reports, "save").run({}, context)
    second = tool(reports, "save").run({}, context)
    assert first == second and first.message.startswith('Saved "Revenue Q1"')
    assert len(reports.rows("SELECT * FROM saved_reports")) == 1 and len(reports.audit("save")) == 1


def test_a_retry_of_the_same_save_request_finds_the_first_report_even_if_the_clock_moved(reports):
    context = ctx(last=LastAnswer("Revenue Q1", "the text"), key="same-key")
    first = tool(reports, "save").run({}, context)
    reports.clock.advance(days=3)
    assert tool(reports, "save").run({}, context) == first  # the first save's date, not today's
    assert len(reports.rows("SELECT * FROM saved_reports")) == 1


# (b) delete twice with the same key --------------------------------------------------------------------------------------------------

def test_delete_twice_with_the_same_key_is_one_batch_and_one_executed_row(reports):
    reports.seed("A")
    reports.seed("B")
    ids = report_ids(reports)
    first = delete(reports, ids, key="same-key")
    second = delete(reports, ids, key="same-key")
    assert first == second and first.count == 2
    assert len(reports.rows("SELECT * FROM delete_batches")) == 1
    assert [row["outcome"] for row in reports.audit("delete_executed")] == ["ok"]


def test_a_replay_after_an_undo_does_not_delete_again(reports):
    reports.seed("A")
    ids = report_ids(reports)
    delete(reports, ids, key="same-key")
    assert tool(reports, "undo_delete").run({}, ctx(key="u1")).message == "Restored 1 report."
    again = delete(reports, ids, key="same-key")  # the same request, replayed: the first result, no new effect
    assert again.count == 1 and reports.titles() == ["A"] and len(reports.rows("SELECT * FROM delete_batches")) == 1


def test_a_different_set_is_a_different_request_with_its_own_key(reports):
    reports.seed("A")
    reports.seed("B")
    first, second = report_ids(reports)
    assert delete(reports, [first], key=idempotency.delete_key("c", "t-1", "hash-1")).count == 1
    assert delete(reports, [second], key=idempotency.delete_key("c", "t-1", "hash-2")).count == 1
    assert len(reports.rows("SELECT * FROM delete_batches")) == 2


# undo ----------------------------------------------------------------------------------------------------------------------------------

def test_a_retried_undo_restores_nothing_more(reports):
    """Undo walks back one delete at a time. The same request twice must not walk back two."""
    for title in ("A", "B"):
        reports.seed(title)
    first, second = report_ids(reports)
    delete(reports, [first], key="d1")
    reports.clock.advance(hours=1)
    delete(reports, [second], key="d2")
    assert tool(reports, "undo_delete").run({}, ctx(key="u1")).message == "Restored 1 report." and reports.titles() == ["B"]
    assert tool(reports, "undo_delete").run({}, ctx(key="u1")).message == "Restored 1 report." and reports.titles() == ["B"]  # a retry
    assert [row["outcome"] for row in reports.audit("undo")] == ["ok"]  # no second row
    assert tool(reports, "undo_delete").run({}, ctx(key="u2")).message == "Restored 1 report." and reports.titles() == ["A", "B"]  # a new request


# (c) kill and rebuild the graph from the same SQLite file, then resume --------------------------------------------------------------------

def test_kill_and_rebuild_the_graph_then_resume_deletes_exactly_once(durable):
    durable.seed("Revenue Q1")
    durable.seed("Revenue Q2")
    assert durable.delete(text_match("revenue")).awaiting_confirmation
    durable.restart()  # the process is gone: agent, store, tools and saver are built again from the files
    assert durable.rig.agent.awaiting_confirmation(durable.rig.conversation_id)
    assert durable.reply("yes").outcome == "action_done"
    assert len(durable.rows("SELECT * FROM delete_batches")) == 1
    assert [row["outcome"] for row in durable.audit("delete_executed")] == ["ok"]
    assert durable.titles() == []


def test_the_key_of_a_delete_is_the_same_before_and_after_a_restart(durable):
    """conversation | the turn that asked | delete | the keyed hash of the exact set: so a resume finds the first effect."""
    durable.seed("Revenue Q1")
    durable.delete(text_match("revenue"))
    pending = durable.state().values["pending_action"]
    expected = idempotency.delete_key(durable.rig.conversation_id, pending["request_turn_id"], pending["id_set_hash"])
    durable.restart()
    durable.reply("yes")
    [batch] = durable.rows("SELECT idempotency_key, id_set_hash FROM delete_batches")
    assert batch["idempotency_key"] == expected and batch["id_set_hash"] == pending["id_set_hash"]
    [confirmed] = durable.audit("delete_confirmed")
    assert confirmed["idempotency_key"] == expected  # the confirmation and the effect are tied by the same key


def test_a_resume_after_the_effect_was_committed_but_the_checkpoint_was_lost_has_no_second_effect(durable):
    """The process dies after the delete commits and before the checkpoint moves on. The resumed gate asks again; the reports are
    already gone, so it finds nothing, and the one batch stays one."""
    durable.seed("Revenue Q1")
    durable.delete(text_match("revenue"))
    pending = durable.state().values["pending_action"]
    key = idempotency.delete_key(durable.rig.conversation_id, pending["request_turn_id"], pending["id_set_hash"])
    assert delete(durable, report_ids(durable), key=key).count == 1  # the effect that was committed before the crash
    durable.restart()
    result = durable.reply("yes")
    assert result.outcome == "action_cancelled" and not result.awaiting_confirmation
    assert len(durable.rows("SELECT * FROM delete_batches")) == 1 and len(durable.audit("delete_executed")) == 1


def test_a_resume_that_finds_the_same_set_again_returns_the_first_result_and_deletes_nothing_more(durable):
    """The same crash, and the delete was undone before the user came back: the set is the same again. The key is the same,
    so the replay is answered from the first effect instead of deleting a second time."""
    durable.seed("Revenue Q1")
    durable.delete(text_match("revenue"))
    pending = durable.state().values["pending_action"]
    key = idempotency.delete_key(durable.rig.conversation_id, pending["request_turn_id"], pending["id_set_hash"])
    delete(durable, report_ids(durable), key=key)
    assert tool(durable, "undo_delete").run({}, ctx(key="u")).message == "Restored 1 report."
    durable.restart()
    assert durable.reply("yes").outcome == "action_done"
    assert durable.titles() == ["Revenue Q1"] and len(durable.rows("SELECT * FROM delete_batches")) == 1
    assert len(durable.audit("delete_executed")) == 1


# (d) a second turn saving the same text makes a second report -------------------------------------------------------------------------

def test_a_later_turn_saving_the_same_text_makes_a_second_report(reports):
    reports.fake_bigquery.add("revenue", df=pd.DataFrame({"brand": ["Brand A"], "revenue": [1234.5]}))
    reports.rig.gateway.queue(Step.INPUT_GUARD, guard_reply(standalone="Revenue by brand"))
    reports.rig.gateway.queue(Step.SQL_WRITE, sql_reply())
    reports.rig.gateway.queue(Step.COMPOSER, composer_reply("Brand A earned 1,234.5.", "Revenue by brand"))
    assert reports.rig.ask("revenue by brand").outcome == "answered"
    assert reports.say("save_report").outcome == "action_done"
    assert reports.say("save_report").outcome == "action_done"  # the next turn: the same answer, a new request (Q23)
    assert reports.titles() == ["Revenue by brand", "Revenue by brand"]
    assert len(reports.audit("save")) == 2


def test_without_a_key_the_same_turn_and_text_is_the_same_request_and_a_new_turn_is_not(reports):
    last = LastAnswer("T", "same text")
    save = tool(reports, "save")
    save.run({}, ctx(turn="t-1", last=last))
    save.run({}, ctx(turn="t-1", last=last))  # a retry of that turn
    assert reports.titles() == ["T"]
    save.run({}, ctx(turn="t-2", last=last))
    assert reports.titles() == ["T", "T"]
    save.run({}, ctx(turn="t-2", last=LastAnswer("T", "other text")))  # same turn, other text: another request
    assert reports.titles() == ["T", "T", "T"]


# at once ---------------------------------------------------------------------------------------------------------------------------------------

def test_eight_threads_with_one_key_make_one_effect(reports):
    reports.seed("A")
    ids = report_ids(reports)
    with ThreadPoolExecutor(max_workers=8) as pool:
        saved = list(pool.map(lambda _: tool(reports, "save").run({}, ctx(last=LastAnswer("T", "body"), key="save-key")), range(8)))
        deleted = list(pool.map(lambda _: delete(reports, ids, key="delete-key"), range(8)))
    assert len({r.message for r in saved}) == 1 and len({r.message for r in deleted}) == 1
    assert len(reports.rows("SELECT * FROM saved_reports")) == 2  # "A" and one "T"
    assert len(reports.rows("SELECT * FROM delete_batches")) == 1 and len(reports.audit("delete_executed")) == 1
    effective = idempotency.save_key("conv-test0001", "t-1", "body", "save-key")
    assert len([row for row in reports.audit("save") if row["idempotency_key"] == effective]) == 1


def test_the_builders_make_the_keys_the_graph_makes():
    import hashlib

    graph_key = hashlib.sha256("|".join(["conv-1", "t-4", "saved_reports.delete", "abc"]).encode("utf-8")).hexdigest()
    assert idempotency.delete_key("conv-1", "t-4", "abc") == graph_key
    assert idempotency.undo_key("conv-1", "t-5") == hashlib.sha256(b"conv-1|t-5|saved_reports.undo_delete").hexdigest()
    assert idempotency.save_key("conv-1", "t-2", "text") != idempotency.save_key("conv-1", "t-3", "text")  # the turn is part of the key
    assert idempotency.save_key("conv-1", "t-2", "text") != idempotency.save_key("conv-1", "t-2", "other")


# a request that ended without an effect is recorded once (review finding) ---------------------------------------------------------------------------

def test_a_repeated_delete_that_found_nothing_writes_one_audit_row(reports):
    reports.seed("A")
    for _ in range(3):
        assert delete(reports, ["r-00000000"], key="same-key").message == messages.NO_MATCH
    assert [row["outcome"] for row in reports.audit("delete_executed")] == ["no_match"]


def test_a_repeated_undo_with_nothing_to_restore_writes_one_audit_row_for_each_outcome(reports):
    undo = tool(reports, "undo_delete")
    for _ in range(3):
        assert undo.run({}, ctx(key="u-none")).message == messages.NO_DELETE_TO_UNDO
    reports.seed("A")
    delete(reports, report_ids(reports), key="d1")
    assert undo.run({}, ctx(key="u-ok")).message == "Restored 1 report."
    for _ in range(3):
        assert undo.run({}, ctx(key="u-already")).message == messages.ALREADY_UNDONE
    delete(reports, report_ids(reports), key="d2")
    reports.clock.advance(days=31)
    for _ in range(3):
        assert undo.run({}, ctx(key="u-expired")).message == messages.UNDO_EXPIRED
    assert [row["outcome"] for row in reports.audit("undo")] == ["none", "ok", "already", "expired"]


def test_a_repeated_save_that_was_refused_writes_one_audit_row(reports):
    last = LastAnswer("Customers", "Write to jane.roe@example.test.")
    for _ in range(3):
        assert tool(reports, "save").run({}, ctx(last=last, key="same-key")).message == messages.NOT_SAVED_PERSONAL
    assert [row["outcome"] for row in reports.audit("save")] == ["blocked"]


def test_a_repeated_failure_is_recorded_each_time_because_it_failed_each_time(reports):
    reports.execute("CREATE TRIGGER boom BEFORE INSERT ON saved_reports BEGIN SELECT RAISE(ABORT, 'boom'); END")
    for _ in range(2):
        try:
            tool(reports, "save").run({}, ctx(last=LastAnswer("T", "body"), key="same-key"))
        except Exception:  # noqa: BLE001 - the graph reports it as TOOL_FAILED
            pass
    assert [row["outcome"] for row in reports.audit("save")] == ["failed", "failed"]


# the owner is in the lookup of a key too (review finding) -------------------------------------------------------------------------------------------

def test_another_owner_presenting_a_save_key_gets_nothing_of_the_first_owners_and_nothing_is_stored(reports):
    reports.have_conversation("conv-test0001", BOB)
    last = LastAnswer("Alice private plan", "the plan")
    tool(reports, "save").run({}, ctx(ALICE, last=last, key="shared-key"))
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):  # the key is taken: it fails closed, it does not hand back Alice's report
        tool(reports, "save").run({}, ctx(BOB, last=last, key="shared-key"))
    assert reports.titles(BOB) == [] and reports.titles(ALICE) == ["Alice private plan"]


def test_another_owner_presenting_a_delete_or_undo_key_gets_nothing_of_the_first_owners(reports):
    reports.have_conversation("conv-test0001", BOB)
    reports.seed("Alice one")
    delete(reports, report_ids(reports), key="shared-key")
    tool(reports, "undo_delete").run({}, ctx(ALICE, key="shared-undo"))
    delete(reports, report_ids(reports), key="other-key")
    assert delete(reports, ["r-00000000"], BOB, key="shared-key").message == messages.NO_MATCH  # not "Deleted 1 report" from Alice's batch
    assert tool(reports, "undo_delete").run({}, ctx(BOB, key="shared-undo")).message == messages.NO_DELETE_TO_UNDO  # not "Restored 1"
    reports.seed("Bob one", user=BOB, conversation="conv-test0001")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        delete(reports, report_ids(reports, BOB), BOB, key="shared-key")
    assert reports.titles(BOB) == ["Bob one"]  # rolled back whole


# one recipe for the key (review finding) ----------------------------------------------------------------------------------------------------------------

def test_the_graph_key_of_a_save_is_the_one_recipe_with_the_text_added(reports):
    from src.tools.base import idempotency_key

    reports.fake_bigquery.add("revenue", df=pd.DataFrame({"brand": ["Brand A"], "revenue": [1234.5]}))
    reports.rig.gateway.queue(Step.INPUT_GUARD, guard_reply(standalone="Revenue by brand"))
    reports.rig.gateway.queue(Step.SQL_WRITE, sql_reply())
    reports.rig.gateway.queue(Step.COMPOSER, composer_reply("Brand A earned 1,234.5.", "Revenue by brand"))
    reports.rig.ask("revenue by brand")
    reports.say("save_report")
    [row] = reports.rows("SELECT body, idempotency_key FROM saved_reports")
    graph_key = idempotency_key(reports.rig.conversation_id, f"t-{reports.rig.turns}", "saved_reports.save")
    assert row["idempotency_key"] == idempotency.save_key(reports.rig.conversation_id, f"t-{reports.rig.turns}", row["body"], graph_key)
    assert idempotency.make_key is idempotency_key  # the graph and the store use the same function, so the recipes cannot drift


def test_the_same_request_key_with_other_text_is_another_request(reports):
    save = tool(reports, "save")
    save.run({}, ctx(last=LastAnswer("T", "first text"), key="request-key"))
    save.run({}, ctx(last=LastAnswer("T", "other text"), key="request-key"))
    save.run({}, ctx(last=LastAnswer("T", "first text"), key="request-key"))  # and the first again is the first
    assert len(reports.rows("SELECT * FROM saved_reports")) == 2
