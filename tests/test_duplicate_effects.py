"""No effect happens twice (plan 09, PRD section 9: duplicate saves and deletes on retry = 0; the loop-engineering checklist's missing test), across the layers
that retry: the model gateway inside a turn, the user after a restart of the app, and the chaos profile over many turns.

The unit tests of the store and the graph are tests/test_idempotency.py and tests/test_delete_flow.py. These run the whole app, start it again where a real
process would die, and count what the database and the audit log say.
"""
import sqlite3
from collections import Counter
from contextlib import closing

import pytest

from eval.datasets import ModelScript
from src.llm.chaos import Chaos
from src.llm.routing import Step
from tests.agent_support import guard_reply

pytestmark = pytest.mark.chaos
REVENUE = "What was my revenue in 2025?"


def audit_actions(folder) -> Counter:
    with closing(sqlite3.connect(folder / "db.sqlite3")) as conn:
        return Counter(action for (action,) in conn.execute("SELECT action FROM audit_log"))


def stored_reports(folder, owner: str = "alice") -> int:
    """The owner's reports that are not deleted (the seed gives Bob and the CEO reports too)."""
    with closing(sqlite3.connect(folder / "db.sqlite3")) as conn:
        return conn.execute("SELECT COUNT(*) FROM saved_reports WHERE deleted_at IS NULL AND owner_id = ?", (owner,)).fetchone()[0]


def test_a_restart_between_the_preview_and_the_reply_deletes_exactly_once_and_a_second_yes_deletes_nothing_more(offline_eval):
    delete = ModelScript(intent="delete_reports", selector={"kind": "text_match", "text": "returns"})
    ask = offline_eval.hijack(lambda i: delete, "alice")
    first = offline_eval.harness.play_lines("restart-delete", "alice", ["Delete my reports about returns."], setup=["seed_reports"], before_turn=ask)
    assert first.evidence[0].outcome == "awaiting_confirmation"
    before = stored_reports(first.folder)
    assert before == 4 and audit_actions(first.folder)["delete_executed"] == 0

    # The app is started again (a new process: nothing is in memory). The next chat shows the preview again, and the typed line is the reply.
    second = offline_eval.harness.play_lines("restart-delete", "alice", ["yes"], before_turn=lambda index, line: offline_eval.gateway.reset_script())
    assert second.evidence[0].outcome == "action_done"
    after = stored_reports(second.folder)
    assert after < before and audit_actions(second.folder)["delete_executed"] == 1

    # And again: a stray second "yes" is only a new message. The scripted model takes it for nothing the app can do.
    nothing = ModelScript(intent="out_of_scope", in_scope=False)
    third = offline_eval.harness.play_lines("restart-delete", "alice", ["yes"], before_turn=offline_eval.hijack(lambda i: nothing, "alice"))
    assert third.evidence[0].outcome == "refused"
    assert stored_reports(third.folder) == after and audit_actions(third.folder)["delete_executed"] == 1


def test_a_model_fault_inside_the_save_turn_is_retried_by_the_gateway_and_saves_exactly_once(chaos_eval):
    chaos = Chaos.scripted()

    def before_turn(index, line):
        if index == 2:
            chaos_eval.client.replies["InputGuardOut"] = guard_reply(intent="save_report", standalone=line)
            chaos.fail_llm("rate_limit", times=1, step=Step.INPUT_GUARD)  # the classifier of the save turn fails once, and the gateway tries again

    played = chaos_eval.harness.play_lines("retry-save", "alice", [REVENUE, "Save that answer as a report."], chaos_engine=chaos, before_turn=before_turn)
    assert chaos.injected == ["llm:rate_limit"]
    first, second = played.evidence
    assert first.outcome == "answered" and second.outcome == "action_done"
    assert chaos_eval.clock.sleeps  # it did wait and retry
    assert stored_reports(played.folder) == 1 and audit_actions(played.folder)["save"] == 1
    assert second.row["llm_calls"] == 2  # the failed attempt and the one that worked: each outbound attempt is one charged call, not two turns


def test_over_many_turns_under_harsh_chaos_every_saved_report_has_one_audit_row_and_no_save_happens_twice(chaos_eval):
    """Five pairs of turns, a question and "save that", under the harsh profile with a fixed seed. Some turns fail. Whatever happened, the reports in the store, the
    audit rows of saves, and the save turns that said they were done are the same number: no effect without a record, no record without an effect."""
    chaos = Chaos.from_level("harsh", 3)

    def before_turn(index, line):
        chaos_eval.client.replies["InputGuardOut"] = (
            guard_reply(standalone=REVENUE) if index % 2 else guard_reply(intent="save_report", standalone=line)
        )

    lines = [line for _ in range(5) for line in (REVENUE, "Save that answer as a report.")]
    played = chaos_eval.harness.play_lines("chaos-saves", "alice", lines, chaos_engine=chaos, before_turn=before_turn)
    saves_done = sum(1 for ev in played.evidence[1::2] if ev.outcome == "action_done")
    assert len(played.evidence) == 10 and all(ev.outcome != "crashed" for ev in played.evidence)
    assert stored_reports(played.folder) == audit_actions(played.folder)["save"] == saves_done
    assert saves_done <= 5  # at most one report for each request
