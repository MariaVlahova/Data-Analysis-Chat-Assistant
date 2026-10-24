"""The append-only audit log (plan 08, F26): every action writes one row, the table cannot be changed, an effect and its row commit
together, and no row holds report text."""
import sqlite3

import pytest

from src.storage.audit import AuditEntry, AuditLog
from tests.guard_support import ALICE
from tests.reports_support import BATCH_PATTERN, ID_PATTERN, text_match
from tests.test_reports_store import ctx, delete, report_ids, tool

ACTIONS = {"save", "delete_requested", "delete_confirmed", "delete_cancelled", "delete_stale", "delete_executed", "undo", "purge"}  # plan 08
TITLE = "Quarterly outlook"
BODY = "Secret strategy: acquire Zeta Corp next spring."


def run_every_action(env):
    """One of each action of plan 08, through the graph where the graph makes the row."""
    env.seed(TITLE, BODY)                                   # save
    env.delete(text_match("zeta"))                          # delete_requested
    env.reply("no")                                         # delete_cancelled
    env.delete(text_match("zeta"))                          # delete_requested
    env.seed("Zeta follow-up", BODY, key="second")          # a second report appears: the preview is stale
    env.reply("yes")                                        # delete_confirmed? no: delete_stale
    env.reply("yes")                                        # delete_confirmed, delete_executed
    env.say("undo_delete")                                  # undo
    env.clock.advance(days=31)
    env.delete(text_match("zeta"))
    env.reply("yes")
    env.clock.advance(days=31)
    env.store.purge_expired()                               # purge


def test_every_action_writes_a_row(reports):
    run_every_action(reports)
    seen = {row["action"] for row in reports.audit()}
    assert seen == ACTIONS, ACTIONS - seen


def test_a_save_writes_one_row_with_the_user_hash_the_count_and_the_ids_in_a_hash(reports):
    reports.seed(TITLE, BODY)
    [row] = reports.audit()
    assert (row["action"], row["outcome"], row["count"], row["user_hash"]) == ("save", "ok", 1, ALICE.user_hash)
    assert len(row["target_hash"]) == 64 and row["turn_id"] == "t-0" and row["trace_id"] == "00000000" and row["ts"].startswith("2026-10-01T12:00:00")
    assert set(row) == {"seq", "ts", "user_hash", "action", "target_hash", "count", "outcome", "trace_id", "turn_id", "idempotency_key", "detail"}


def test_the_rows_of_one_delete_are_tied_by_the_same_id_set_hash_and_key(reports):
    reports.seed("Revenue Q1")
    request = reports.delete(text_match("revenue"))
    reports.reply("yes")
    requested, confirmed, executed = (reports.audit(action)[0] for action in ("delete_requested", "delete_confirmed", "delete_executed"))
    assert requested["target_hash"] == confirmed["target_hash"] == executed["target_hash"] and len(requested["target_hash"]) == 64
    assert (requested["count"], confirmed["count"], executed["count"]) == (1, 1, 1)
    assert confirmed["idempotency_key"] == executed["idempotency_key"] and executed["outcome"] == "ok"
    assert requested["trace_id"] != confirmed["trace_id"]  # two turns, two traces
    assert request.awaiting_confirmation


def test_one_delete_audits_requested_confirmed_and_executed_in_that_order_once_each(reports):
    reports.seed("Revenue Q1")
    reports.delete(text_match("revenue"))
    reports.reply("yes")
    assert [row["action"] for row in reports.audit()] == ["save", "delete_requested", "delete_confirmed", "delete_executed"]


def test_a_cancelled_delete_audits_the_request_and_the_cancel_and_no_effect(reports):
    reports.seed("Revenue Q1")
    reports.delete(text_match("revenue"))
    reports.reply("no")
    assert [(row["action"], row["outcome"]) for row in reports.audit()][1:] == [("delete_requested", "ok"), ("delete_cancelled", "cancelled")]
    assert reports.audit("delete_executed") == [] and reports.titles() == ["Revenue Q1"]


def test_a_stale_confirmation_is_audited_and_the_new_preview_is_audited_when_it_is_confirmed(reports):
    reports.seed("Revenue Q1")
    reports.delete(text_match("revenue"))
    reports.seed("Revenue Q2")
    reports.reply("yes")
    [stale] = reports.audit("delete_stale")
    assert stale["outcome"] == "stale" and stale["count"] == 1 and "2 found now" in stale["detail"]
    assert reports.audit("delete_executed") == []
    reports.reply("yes")
    assert len(reports.audit("delete_executed")) == 1


def test_a_request_that_matches_nothing_is_audited_as_no_match(reports):
    reports.seed("Revenue Q1")
    reports.delete(text_match("zebra"))
    [row] = reports.audit("delete_requested")
    assert (row["outcome"], row["count"], row["target_hash"]) == ("no_match", 0, None)


# append-only -------------------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("statement", [
    "UPDATE audit_log SET outcome = 'tampered'",
    "UPDATE audit_log SET count = 0 WHERE seq = 1",
    "DELETE FROM audit_log",
    "DELETE FROM audit_log WHERE seq = 1",
])
def test_the_audit_log_cannot_be_updated_or_deleted(reports, statement):
    reports.seed("Revenue Q1")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        reports.execute(statement)
    assert len(reports.audit()) == 1 and reports.audit()[0]["outcome"] == "ok"


def test_the_startup_purge_does_not_touch_the_audit_log(reports):
    reports.seed("Revenue Q1")
    reports.delete(text_match("revenue"))
    reports.reply("yes")
    before = reports.audit()
    reports.clock.advance(days=400)
    assert reports.store.purge_expired() == 1
    assert reports.audit()[: len(before)] == before and reports.audit()[-1]["action"] == "purge"  # only added to


# an effect and its row commit together ------------------------------------------------------------------------------------------------------------

def test_a_save_whose_audit_row_cannot_be_written_is_rolled_back(reports):
    reports.execute("CREATE TRIGGER no_save_audit BEFORE INSERT ON audit_log WHEN NEW.action = 'save' AND NEW.outcome = 'ok' "
                    "BEGIN SELECT RAISE(ABORT, 'forced audit failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="forced audit failure"):
        reports.seed(TITLE, BODY)
    assert reports.rows("SELECT * FROM saved_reports") == []  # no effect without a record
    assert [(row["action"], row["outcome"]) for row in reports.audit()] == [("save", "failed")]  # and the failure itself is recorded


def test_a_delete_whose_audit_row_cannot_be_written_is_rolled_back(reports):
    reports.seed("Revenue Q1")
    ids = report_ids(reports)
    reports.execute("CREATE TRIGGER no_delete_audit BEFORE INSERT ON audit_log WHEN NEW.action = 'delete_executed' AND NEW.outcome = 'ok' "
                    "BEGIN SELECT RAISE(ABORT, 'forced audit failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="forced audit failure"):
        delete(reports, ids)
    assert reports.titles() == ["Revenue Q1"] and reports.rows("SELECT * FROM delete_batches") == []
    [row] = reports.rows("SELECT deleted_at, delete_batch_id, purge_after FROM saved_reports")
    assert row == {"deleted_at": None, "delete_batch_id": None, "purge_after": None}


def test_an_undo_whose_audit_row_cannot_be_written_is_rolled_back(reports):
    reports.seed("Revenue Q1")
    delete(reports, report_ids(reports))
    reports.execute("CREATE TRIGGER no_undo_audit BEFORE INSERT ON audit_log WHEN NEW.action = 'undo' AND NEW.outcome = 'ok' "
                    "BEGIN SELECT RAISE(ABORT, 'forced audit failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        tool(reports, "undo_delete").run({}, ctx(key="u"))
    assert reports.titles() == [] and reports.rows("SELECT undone_at, undo_key FROM delete_batches") == [{"undone_at": None, "undo_key": None}]


def test_a_gate_event_that_cannot_be_audited_stops_the_step_before_anything_changes(reports):
    reports.seed("Revenue Q1")
    reports.execute("CREATE TRIGGER no_request_audit BEFORE INSERT ON audit_log WHEN NEW.action = 'delete_requested' "
                    "BEGIN SELECT RAISE(ABORT, 'forced audit failure'); END")
    result = reports.delete(text_match("revenue"))
    assert result.outcome == "failed" and "nothing was changed" in result.text and not result.awaiting_confirmation
    assert reports.titles() == ["Revenue Q1"] and "forced audit failure" not in str(reports.rig.spans())


def test_a_confirmation_that_cannot_be_audited_does_not_delete(reports):
    reports.seed("Revenue Q1")
    reports.delete(text_match("revenue"))
    reports.execute("CREATE TRIGGER no_confirm_audit BEFORE INSERT ON audit_log WHEN NEW.action = 'delete_confirmed' "
                    "BEGIN SELECT RAISE(ABORT, 'forced audit failure'); END")
    result = reports.reply("yes")
    assert result.outcome == "failed" and reports.titles() == ["Revenue Q1"] and reports.rows("SELECT * FROM delete_batches") == []


def test_if_even_the_failure_row_cannot_be_written_the_original_error_still_reaches_the_graph(reports):
    reports.execute("CREATE TRIGGER no_audit_at_all BEFORE INSERT ON audit_log BEGIN SELECT RAISE(ABORT, 'forced audit failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="forced audit failure"):
        reports.seed(TITLE, BODY)
    assert reports.rows("SELECT * FROM saved_reports") == [] and reports.rows("SELECT * FROM audit_log") == []


# what a row may hold ------------------------------------------------------------------------------------------------------------------------------

def test_no_row_holds_report_text_a_title_or_an_id(reports):
    run_every_action(reports)
    text = str(reports.audit())
    for forbidden in (TITLE, "Secret strategy", "Zeta", "outlook"):
        assert forbidden not in text, forbidden
    assert not ID_PATTERN.search(text) and not BATCH_PATTERN.search(text)


def test_the_detail_is_masked_and_cut(reports):
    reports.store.audit.append(AuditEntry("save", "hash-alice", "failed", detail="could not reach jane.roe@example.test " + "x" * 400))
    [row] = reports.audit()
    assert "jane.roe" not in row["detail"] and len(row["detail"]) <= 200


def test_an_action_name_must_look_like_one(tmp_path, reports):
    log = AuditLog(reports.settings.db_path)
    for bad in ("", "Save", "save; DROP TABLE audit_log", "x"):
        with pytest.raises(ValueError):
            log.append(AuditEntry(bad, "h", "ok"))
    log.append(AuditEntry("send_requested", "h", "ok"))  # another gated tool names its own steps the same way
    assert [row["action"] for row in log.entries()] == ["send_requested"]


# the graph does not depend on the Saved Reports package (review finding) -------------------------------------------------------------------------

def test_the_graph_imports_nothing_from_the_saved_reports_package(tmp_path):
    """The audit log is in `src/storage`: any gated tool writes to it. The graph names no tool, so it must not load the package of one."""
    from tests.test_cli_imports import run_clean

    result = run_clean(
        """
        import src.agent.graph, src.agent.runtime, src.agent.deps, src.agent.nodes.tools, src.agent.checkpointer
        print("LOADED:", sorted(name for name in sys.modules if name.startswith(("src.reports", "src.tools.saved_reports"))))
        """,
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert "LOADED: []" in result.stdout


# a new preview after a stale confirmation is a request of its own (review finding) -------------------------------------------------------------------------

def test_every_confirmed_and_executed_row_has_a_request_row_with_the_same_hash_even_after_a_stale_loop(reports):
    reports.seed("Revenue Q1")
    reports.delete(text_match("revenue"))
    reports.seed("Revenue Q2")  # the set changes after the preview
    assert reports.reply("yes").awaiting_confirmation  # stale: a new preview
    assert reports.reply("yes").outcome == "action_done"
    rows = reports.audit()
    assert [row["action"] for row in rows] == [
        "save", "delete_requested", "save", "delete_stale", "delete_requested", "delete_confirmed", "delete_executed",
    ]
    requested = {row["target_hash"] for row in reports.audit("delete_requested")}
    assert len(requested) == 2  # the first set and the new one
    for action in ("delete_confirmed", "delete_executed"):
        assert {row["target_hash"] for row in reports.audit(action)} <= requested
    [stale] = reports.audit("delete_stale")
    assert stale["target_hash"] in requested and stale["target_hash"] != reports.audit("delete_executed")[0]["target_hash"]
    again = reports.audit("delete_requested")[1]
    assert again["count"] == 2 and "new preview" in again["detail"]


# a gated tool's verb is free text and still makes valid audit names (review finding) ------------------------------------------------------------------------

def test_the_audit_name_of_a_verb_is_made_of_letters(reports):
    from src.tools.base import ToolManifest

    def manifest(verb):
        return ToolManifest("t", "d", {}, "destructive", frozenset(), 1.0, "free", True, False, None, (), None, "thing", verb)

    assert [manifest(verb).audit_verb for verb in ("delete", "E-mail", "send out", "Publish", "über-send")] == ["delete", "e_mail", "send_out", "publish", "ber_send"]
    assert manifest("").audit_verb == "delete" and manifest("!!!").audit_verb == ""
