"""The Tool Router and the HITL Gate (plan 07, F22 to F24, decision 3.6 and 3.14), on the real Saved Reports tools and on dummy ones.

What is about the store (the friction levels with a real count, the hash, soft delete, undo, the owner) is in test_delete_flow.py and
test_reports_store.py. What is here is about the graph around any tool: the gate, the registry, the manifest, the timeouts, the ids."""
import json
import re
import time
from dataclasses import replace
from typing import Any, Mapping

import pytest

from src.errors import ConfigError, ErrorKind
from src.llm.routing import Step
from src.tools.base import Target, ToolContext, ToolManifest, ToolResult
from src.tools.confirmation import confirmation_for, id_set_hash, preview_text, reply_confirms
from src.tools.registry import ToolRegistry
from tests.agent_support import guard_reply, make_rig
from tests.guard_support import ALICE
from tests.reports_support import ReportsEnv, text_match

CURRENT = {"kind": "conversation", "text": None}
ID_PATTERN = re.compile(r"r-[0-9a-f]{8}")


@pytest.fixture
def env(reports):
    """The graph on the real Saved Reports tools (see `tests/reports_support.py`)."""
    return reports


# the preview and the levels -------------------------------------------------------------------------------------------


def test_the_confirmation_helpers_compare_in_code():
    assert confirmation_for(1, "text_match", 10, "report").level == 1
    assert confirmation_for(9, "text_match", 10, "report").level == 2
    assert confirmation_for(10, "text_match", 10, "report").level == 3 and confirmation_for(2, "all", 10, "report").level == 3
    assert reply_confirms(1, "yes", "Y") and not reply_confirms(2, "yes", "y") and not reply_confirms(3, "DELETE 5", "delete 5")
    assert not reply_confirms(1, "yes", None) and not reply_confirms(1, "yes", ["yes"]) and not reply_confirms(3, "DELETE 5", " DELETE 4")
    KEY = b"k" * 32
    assert id_set_hash(["a", "b"], KEY) == id_set_hash(["b", "a", "a"], KEY) and id_set_hash(["a"], KEY) != id_set_hash(["a", "b"], KEY)


def test_a_long_preview_is_cut_but_the_count_is_the_whole_count():
    targets = [Target(f"id{n}", f"Report {n}", "2026-10-22") for n in range(14)]
    text = preview_text(targets, "report")
    assert text.startswith("I found 14 reports:") and "10. Report 9" in text and "11." not in text and text.endswith("... and 4 more.")


# the hash binding (F24) ------------------------------------------------------------------------------------------------


def test_a_stale_preview_still_needs_its_own_confirmation_and_a_no_cancels_it(env):
    env.seed("Revenue Q1")
    env.delete(text_match("revenue"))
    env.seed("Revenue Q2")
    assert env.rig.ask("yes").awaiting_confirmation
    assert env.rig.ask("no").outcome == "action_cancelled" and len(env.titles()) == 2


# only the user's typed reply confirms ----------------------------------------------------------------------------------


def test_what_the_model_says_never_sets_the_reply(env):
    env.seed("Revenue Q1")
    env.delete(text_match("revenue"))
    pending = env.rig.agent.graph.get_state({"configurable": {"thread_id": env.rig.conversation_id}})
    assert pending.next == ("hitl_gate",) and pending.values["reply"] is None
    assert env.titles() == ["Revenue Q1"]
    env.rig.ask("yes")
    done = env.rig.agent.graph.get_state({"configurable": {"thread_id": env.rig.conversation_id}})
    assert done.values["pending_action"] is None and done.values["reply"] is None  # cleared once it was used


def test_the_access_check_runs_again_when_the_confirmation_arrives(project, fake_bigquery):
    users = iter([ALICE])
    from tests.guard_support import NOBODY

    env = ReportsEnv(project, fake_bigquery, load_user=lambda: next(users, NOBODY))
    env.rig.user = ALICE
    env.seed("Revenue Q1", user=ALICE)
    env.delete(text_match("revenue"))
    result = env.rig.ask("yes")  # access was taken away in between
    assert result.error.kind is ErrorKind.NO_ACCESS and env.titles(ALICE) == ["Revenue Q1"]


# ids stay out of state, traces and the model ---------------------------------------------------------------------------


def test_the_pending_action_holds_the_selector_and_a_hash_never_the_reports(env):
    env.seed("Revenue Q1")
    env.delete(text_match("revenue"))
    values = env.rig.agent.graph.get_state({"configurable": {"thread_id": env.rig.conversation_id}}).values
    pending = values["pending_action"]
    assert pending["args"] == {"selector": text_match("revenue")} and re.fullmatch(r"[0-9a-f]{64}", pending["id_set_hash"])
    assert pending["count"] == 1 and pending["level"] == 1 and pending["expected_reply"] == "yes"
    assert "ids" not in pending["args"] and "Revenue Q1" in pending["preview"]  # the preview has titles, which the user sees anyway


# traces and metrics ---------------------------------------------------------------------------------------------------

def test_the_request_and_the_confirmation_are_two_traces_with_the_events_of_plan_02(env):
    env.seed("Revenue Q1")
    env.delete(text_match("revenue"))
    request = env.rig.spans()
    names = [s["name"] for s in request]
    assert names[-3:] == ["tool_router", "prepare_action", "hitl_gate"] or {"tool_router", "prepare_action", "hitl_gate"} <= set(names)
    gate = next(s for s in request if s["name"] == "hitl_gate")
    assert gate["kind"] == "hitl" and gate["status"] == "ok" and gate["attrs"].get("paused")  # a pause is not a failure
    requested = next(e for s in request for e in s["events"] if e["name"] == "hitl.requested")
    assert requested["attrs"]["level"] == 1 and requested["attrs"]["count"] == 1 and len(requested["attrs"]["id_set_hash"]) == 12
    assert env.rig.last_row["outcome"] == "answered" and env.rig.last_row["turn_kind"] == "tool"

    env.rig.ask("yes")
    confirm = env.rig.spans()
    assert any(e["name"] == "hitl.approved" for s in confirm for e in s["events"])
    assert any(s["name"] == "tool.saved_reports.delete" and s["kind"] == "tool" for s in confirm)
    assert env.rig.last_row["outcome"] == "action_done" and env.rig.last_row["intent"] == "delete_reports" and env.rig.last_row["turn_kind"] == "tool"
    assert "Revenue Q1" not in json.dumps(confirm)  # the report's text is not in the trace


def test_a_cancelled_confirmation_is_a_hitl_block_and_an_action_cancelled_outcome(env):
    env.seed("Revenue Q1")
    env.delete(text_match("revenue"))
    env.rig.ask("no")
    assert env.rig.last_row["outcome"] == "action_cancelled" and env.rig.last_row["guard_blocks"]["hitl"] == 1
    assert any(e["name"] == "hitl.denied" for s in env.rig.spans() for e in s["events"])


# save, list, undo, no match, owners ------------------------------------------------------------------------------------

def test_save_stores_the_last_answer_and_list_shows_it(env):
    import pandas as pd

    from tests.agent_support import composer_reply, sql_reply

    assert "nothing to save yet" in env.say("save_report").text
    env.rig.client.add("revenue", df=pd.DataFrame({"brand": ["Brand A"], "revenue": [1234.5]}))
    env.rig.gateway.queue(Step.INPUT_GUARD, guard_reply(standalone="Revenue by brand"))
    env.rig.gateway.queue(Step.SQL_WRITE, sql_reply())
    env.rig.gateway.queue(Step.COMPOSER, composer_reply("Brand A earned 1,234.5."))
    assert env.rig.ask("revenue by brand").outcome == "answered"  # an answered turn gives `save_report` something to save
    saved = env.say("save_report")
    assert saved.outcome == "action_done" and saved.text.startswith('Saved "Revenue by brand" on ')
    assert env.titles() == ["Revenue by brand"]
    assert "You have 1 saved report:\n1. Revenue by brand (" in env.say("list_reports").text
    body = env.rows("SELECT body FROM saved_reports")[0]["body"]
    assert "Brand A earned 1,234.5." in body and "Scope: your brands" in body  # the answer that passed the Output Guard, as it was shown


# the registry: a new tool needs no graph change -------------------------------------------------------------------------

class Dummy:
    def __init__(self, name, risk, *, intents=(), finder=None, roles=frozenset({"brand_manager", "ceo"}), refs=("a", "b"), timeout=1.0, verb="", past=""):
        self.manifest = ToolManifest(name, f"The {name} tool.", {}, risk, roles, timeout, "free", True, False, None, intents, finder, "thing", verb, past)
        self.refs, self.ran = refs, []

    def run(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        self.ran.append(dict(args))
        if self.manifest.risk_class == "read" and "find" in self.manifest.name:
            return ToolResult("", len(self.refs), tuple(Target(ref, f"Thing {ref}", "2026-10-22") for ref in self.refs))
        return ToolResult(f"{self.manifest.name} ran")


def test_a_dummy_tool_registers_and_runs_with_no_edit_to_the_graph(project, fake_bigquery):
    registry = ToolRegistry([Dummy("dummy.echo", "read", intents=("dummy_echo",))])
    rig = make_rig(project, fake_bigquery=fake_bigquery, registry=registry)
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply("dummy_echo"))
    result = rig.ask("echo")
    assert result.outcome == "action_done" and result.text == "dummy.echo ran"
    assert "dummy_echo" in rig.gateway.calls_for(Step.INPUT_GUARD)[0].system  # the Input Guard is told what the tool is for
    assert "The dummy.echo tool." in rig.gateway.calls_for(Step.INPUT_GUARD)[0].system


def test_a_destructive_dummy_tool_always_reaches_the_gate_and_gets_the_ids_only_at_the_end(project, fake_bigquery):
    wipe = Dummy("dummy.wipe", "destructive", intents=("wipe_things",), finder="dummy.find")
    registry = ToolRegistry([Dummy("dummy.find", "read"), wipe])
    rig = make_rig(project, fake_bigquery=fake_bigquery, registry=registry)
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply("wipe_things", selector={"kind": "all", "text": None}))
    asked = rig.ask("wipe everything")
    assert asked.awaiting_confirmation and "I found 2 things:" in asked.text and wipe.ran == []  # nothing ran before the confirmation
    assert rig.ask("no").outcome == "action_cancelled" and wipe.ran == []
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply("wipe_things", selector={"kind": "all", "text": None}))
    rig.ask("wipe everything")
    assert rig.ask("DELETE 2").outcome == "action_done"
    assert wipe.ran == [{"selector": {"kind": "all", "text": None}, "ids": ["a", "b"]}]
    state = json.dumps(rig.agent.graph.get_state({"configurable": {"thread_id": rig.conversation_id}}).values, default=str)
    assert '"ids"' not in state  # the IDs went to the tool and nowhere else


def test_a_tool_that_does_not_allow_the_users_role_is_refused(project, fake_bigquery):
    admin_only = Dummy("dummy.admin", "read", intents=("admin_thing",), roles=frozenset({"admin"}))
    rig = make_rig(project, fake_bigquery=fake_bigquery, registry=ToolRegistry([admin_only]))
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply("admin_thing"))
    result = rig.ask("do the admin thing")
    assert result.outcome == "refused" and result.error.rule_id == "TOOL_ROLE" and admin_only.ran == []


def test_a_failing_tool_is_an_honest_message_with_no_claim_of_success(project, fake_bigquery):
    class Broken(Dummy):
        def run(self, args, ctx):
            raise RuntimeError("secret detail: password=hunter2")

    registry = ToolRegistry([Broken("dummy.broken", "write", intents=("break_it",))])
    rig = make_rig(project, fake_bigquery=fake_bigquery, registry=registry)
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply("break_it"))
    result = rig.ask("break it")
    assert result.outcome == "failed" and result.error.kind is ErrorKind.TOOL_FAILED and "nothing was changed" in result.text
    assert "hunter2" not in json.dumps(rig.spans())  # the exception text is never stored


def test_the_registry_checks_its_tools():
    with pytest.raises(ConfigError, match="registered twice"):
        ToolRegistry([Dummy("x", "read"), Dummy("x", "read")])
    with pytest.raises(ConfigError, match="claimed by"):
        ToolRegistry([Dummy("x", "read", intents=("i",)), Dummy("y", "read", intents=("i",))])
    with pytest.raises(ConfigError, match="needs a registered read tool as its finder"):
        ToolRegistry([Dummy("wipe", "destructive", intents=("w",))]).validate()
    with pytest.raises(ConfigError, match="finder"):
        ToolRegistry([Dummy("find", "write"), Dummy("wipe", "destructive", intents=("w",), finder="find")]).validate()
    registry = ToolRegistry([Dummy("find", "read"), Dummy("wipe", "external-send", intents=("w",), finder="find")])
    registry.validate()
    assert registry.intents() == ("w",) and registry.for_intent("w").manifest.name == "wipe" and registry.for_intent("nope") is None


# the wording comes from the manifest, not from the graph ---------------------------------------------------------------------

def send_rig(project, fake_bigquery, refs=("a", "b"), verb="", past=""):
    send = Dummy("dummy.send", "external-send", intents=("send_things",), finder="dummy.find", verb=verb, past=past)
    rig = make_rig(project, fake_bigquery=fake_bigquery, registry=ToolRegistry([Dummy("dummy.find", "read", refs=refs), send]))
    return rig, send


def test_an_external_send_tool_is_confirmed_with_its_own_verb_and_noun(project, fake_bigquery):
    rig, send = send_rig(project, fake_bigquery)
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply("send_things", selector={"kind": "all", "text": None}))
    asked = rig.ask("send everything")
    assert asked.text.endswith("This will send 2 things. Type SEND 2 to confirm.") and "delete" not in asked.text.lower()
    cancelled = rig.ask("DELETE 2")  # the delete wording confirms nothing here
    assert cancelled.outcome == "action_cancelled" and send.ran == [] and cancelled.text == "Cancelled. Nothing was sent."
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply("send_things", selector=text_match("thing")))
    assert rig.ask("send things").text.endswith("Send these 2 things? Type yes to confirm.")


def test_a_manifest_verb_replaces_the_default_of_the_risk_class(project, fake_bigquery):
    rig, _ = send_rig(project, fake_bigquery, refs=("a",), verb="publish", past="published")
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply("send_things", selector=text_match("thing")))
    assert rig.ask("publish it").text.endswith("Publish this thing? (y/N)")
    assert rig.ask("no").text == "Cancelled. Nothing was published."  # the past tense is the manifest's own


@pytest.mark.parametrize("verb", ["drop", "submit", "notify", "ship", "reset"])
def test_a_verb_with_no_past_tense_named_is_not_guessed_at(project, fake_bigquery, verb):
    """"droped" and "notifyed" would be wrong in front of a user: without a `past` in the manifest the cancel text says "changed"."""
    rig, _ = send_rig(project, fake_bigquery, refs=("a",), verb=verb)
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply("send_things", selector=text_match("thing")))
    assert rig.ask(f"{verb} it").text.endswith(f"{verb.capitalize()} this thing? (y/N)")
    assert rig.ask("no").text == "Cancelled. Nothing was changed."


def test_the_questions_back_name_the_tools_noun_and_verb(project, fake_bigquery):
    rig, _ = send_rig(project, fake_bigquery, refs=())
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply("send_things"))  # no selector: ask, never guess
    asked = rig.ask("send")
    assert "Which things do you mean?" in asked.text and '"send my last thing"' in asked.text and "report" not in asked.text
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply("send_things", selector=text_match("zebra")))
    assert rig.ask("send zebras").text == "I found no things matching that."


# the role is checked again at the confirmation -------------------------------------------------------------------------------

def test_a_role_taken_away_before_the_confirmation_stops_the_action(project, fake_bigquery):
    current = [ALICE]
    wipe = Dummy("dummy.wipe", "destructive", intents=("wipe_things",), finder="dummy.find", roles=frozenset({"brand_manager"}))
    registry = ToolRegistry([Dummy("dummy.find", "read", roles=frozenset({"brand_manager", "analyst"})), wipe])
    rig = make_rig(project, fake_bigquery=fake_bigquery, registry=registry, load_user=lambda: current[0])
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply("wipe_things", selector={"kind": "all", "text": None}))
    assert rig.ask("wipe everything").awaiting_confirmation
    current[0] = replace(ALICE, role="analyst")  # users.yaml changed while the preview was on the screen
    result = rig.ask("DELETE 2")
    assert result.outcome == "refused" and result.error.rule_id == "TOOL_ROLE" and result.text == "Your role cannot do that."
    assert wipe.ran == []
    assert rig.agent.awaiting_confirmation(rig.conversation_id) is False  # the request is gone, not left waiting


# the manifest's timeout is applied --------------------------------------------------------------------------------------------

class Slow(Dummy):
    def run(self, args, ctx):
        time.sleep(0.5)
        return ToolResult("done late")


def test_a_tool_that_outlives_its_timeout_is_a_timeout_that_does_not_claim_nothing_changed(project, fake_bigquery):
    registry = ToolRegistry([Slow("dummy.slow", "write", intents=("be_slow",), timeout=0.05)])
    rig = make_rig(project, fake_bigquery=fake_bigquery, registry=registry)
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply("be_slow"))
    started = time.monotonic()
    result = rig.ask("be slow")
    assert time.monotonic() - started < 0.4  # the turn gave up; it did not wait for the tool
    assert result.outcome == "failed" and result.error.kind is ErrorKind.TOOL_TIMEOUT
    assert "can't tell whether it finished" in result.text and "nothing was changed" not in result.text
    assert any(s["status"] == "error" and s["error_kind"] == "TOOL_TIMEOUT" for s in rig.spans())


def test_a_timeout_error_raised_by_the_tool_itself_is_a_failure_not_a_timeout(project, fake_bigquery):
    class Raises(Dummy):
        def run(self, args, ctx):
            raise TimeoutError("its own")

    rig = make_rig(project, fake_bigquery=fake_bigquery, registry=ToolRegistry([Raises("dummy.raises", "write", intents=("raise_it",))]))
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply("raise_it"))
    assert rig.ask("raise it").error.kind is ErrorKind.TOOL_FAILED


def test_a_tool_runs_in_the_context_of_the_turn_so_its_spans_stay_in_the_trace(project, fake_bigquery):
    from src.observability import span

    class Spans(Dummy):
        def run(self, args, ctx):
            with span("tool.inner", "step"):
                return ToolResult("ok")

    rig = make_rig(project, fake_bigquery=fake_bigquery, registry=ToolRegistry([Spans("dummy.spans", "read", intents=("span_it",))]))
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply("span_it"))
    assert rig.ask("span it").text == "ok"
    spans = {s["name"]: s for s in rig.spans()}
    assert spans["tool.inner"]["parent_id"] == spans["tool.dummy.spans"]["span_id"]


# undo walks back through the deletes, newest first (review finding) --------------------------------------------------------------


# a search does not match the envelope of a saved answer (review finding) ------------------------------------------------------------


# a finder is a tool call too (review findings) ----------------------------------------------------------------------------------------

class Finder(Dummy):
    """A read tool that finds the targets of a gated one, with a behaviour a test can choose."""

    def __init__(self, name="dummy.find", *, action=None, **options):
        super().__init__(name, "read", **options)
        self.action = action

    def run(self, args, ctx):
        if self.action is not None:
            return self.action()
        return super().run(args, ctx)


def wipe_rig(project, fake_bigquery, finder):
    wipe = Dummy("dummy.wipe", "destructive", intents=("wipe_things",), finder=finder.manifest.name)
    rig = make_rig(project, fake_bigquery=fake_bigquery, registry=ToolRegistry([finder, wipe]))
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply("wipe_things", selector={"kind": "all", "text": None}))
    return rig, wipe


def test_the_text_a_finder_returns_when_nothing_matches_goes_through_the_output_guard(project, fake_bigquery):
    finder = Finder(action=lambda: ToolResult("No match for jane.roe@example.test", 0, ()))
    rig, wipe = wipe_rig(project, fake_bigquery, finder)
    result = rig.ask("wipe everything")
    assert result.outcome == "failed" and result.error.kind is ErrorKind.OUTPUT_BLOCKED
    assert "jane.roe" not in result.text and "jane.roe" not in json.dumps(rig.agent.graph.get_state({"configurable": {"thread_id": rig.conversation_id}}).values, default=str)
    assert wipe.ran == []


def test_the_fixed_words_for_no_match_still_arrive(project, fake_bigquery):
    rig, _ = wipe_rig(project, fake_bigquery, Finder(action=lambda: ToolResult("", 0, ())))
    result = rig.ask("wipe everything")
    assert result.outcome == "answered" and result.text == "I found no things matching that." and not result.awaiting_confirmation


def test_a_finder_that_raises_is_a_tool_failure_not_an_internal_error(project, fake_bigquery):
    def boom():
        raise RuntimeError("secret detail: password=hunter2")

    rig, wipe = wipe_rig(project, fake_bigquery, Finder(action=boom))
    result = rig.ask("wipe everything")
    assert result.error.kind is ErrorKind.TOOL_FAILED and wipe.ran == []
    assert "hunter2" not in json.dumps(rig.spans())
    assert any(s["name"] == "tool.dummy.find" and s["status"] == "error" for s in rig.spans())  # the finder has its own span


def test_a_finder_that_outlives_its_timeout_does_not_hold_the_turn(project, fake_bigquery):
    def slow():
        time.sleep(0.5)
        return ToolResult("", 0, ())

    rig, wipe = wipe_rig(project, fake_bigquery, Finder(action=slow, timeout=0.05))
    started = time.monotonic()
    result = rig.ask("wipe everything")
    assert time.monotonic() - started < 0.4
    assert result.error.kind is ErrorKind.TOOL_TIMEOUT and "Nothing was changed" in result.text  # a read changed nothing, and says so
    assert wipe.ran == []


def test_a_finder_that_fails_at_the_confirmation_stops_the_action(project, fake_bigquery):
    calls = []

    def second_time_it_fails():
        calls.append(1)
        if len(calls) > 1:
            raise RuntimeError("down")
        return ToolResult("", 2, (Target("a", "Thing a", "2026-10-22"), Target("b", "Thing b", "2026-10-22")))

    rig, wipe = wipe_rig(project, fake_bigquery, Finder(action=second_time_it_fails))
    assert rig.ask("wipe everything").awaiting_confirmation
    result = rig.ask("DELETE 2")
    assert result.error.kind is ErrorKind.TOOL_FAILED and wipe.ran == []


# the hash of the ID set is keyed (review finding) -------------------------------------------------------------------------------------

def test_the_hash_of_an_id_set_is_keyed_so_short_ids_cannot_be_recovered_from_it():
    import hashlib

    one, other = b"a" * 32, b"b" * 32
    assert id_set_hash(["r-1234abcd"], one) != id_set_hash(["r-1234abcd"], other)
    assert id_set_hash(["r-1234abcd"], one) != hashlib.sha256(b"r-1234abcd").hexdigest()


def test_no_plain_hash_of_the_ids_reaches_the_checkpoint_or_the_trace(env):
    import hashlib

    env.seed("Revenue Q1")
    env.seed("Revenue Q2")
    refs = sorted(row["report_id"] for row in env.rows("SELECT report_id FROM saved_reports"))
    plain = hashlib.sha256("\n".join(refs).encode("utf-8")).hexdigest()
    assert env.delete(text_match("revenue")).awaiting_confirmation
    state = json.dumps(env.rig.agent.graph.get_state({"configurable": {"thread_id": env.rig.conversation_id}}).values, default=str)
    assert plain not in state and plain[:12] not in json.dumps(env.rig.spans())
    assert env.rig.agent.deps.hash_key and len(env.rig.agent.deps.hash_key) == 32


def test_the_key_comes_from_the_installation_so_a_restarted_agent_recognizes_a_confirmation(project, fake_bigquery):
    first = make_rig(project, fake_bigquery=fake_bigquery).agent.deps.hash_key
    again = make_rig(project, fake_bigquery=fake_bigquery).agent.deps.hash_key  # a new process on the same data folder
    assert first == again


# one look at the checkpoint per typed line (review finding) --------------------------------------------------------------------------------

def test_a_turn_reads_the_checkpoint_once_to_know_whether_it_is_a_reply(env):
    env.seed("Revenue Q1")
    graph = env.rig.agent.graph
    reads = []
    real = graph.get_state
    graph.get_state = lambda *a, **k: (reads.append(1), real(*a, **k))[1]
    assert env.delete(text_match("revenue")).awaiting_confirmation
    assert env.rig.ask("yes").outcome == "action_done"
    assert len(reads) == 2  # one per typed line, and none to read the result


# a tool cannot take an intent of the Input Guard (review finding) ---------------------------------------------------------------------

@pytest.mark.parametrize("intent", ["data_question", "why_question", "report_request", "schema_question", "out_of_scope"])
def test_a_tool_that_claims_an_intent_of_the_input_guard_is_refused_when_the_graph_is_built(project, fake_bigquery, intent):
    with pytest.raises(ConfigError, match=f"claims the intent {intent}"):
        make_rig(project, fake_bigquery=fake_bigquery, registry=ToolRegistry([Dummy("dummy.hijack", "read", intents=(intent,))]))


def test_the_registry_alone_does_not_know_the_reserved_names():
    ToolRegistry([Dummy("dummy.fine", "read", intents=("data_question",))]).validate()  # nothing reserved: the graph passes the list
    with pytest.raises(ConfigError, match="already uses"):
        ToolRegistry([Dummy("dummy.fine", "read", intents=("data_question",))]).validate(reserved_intents=["data_question"])


# a gated tool whose verb is not a plain word still works and is audited (review finding) -------------------------------------------------------------------

def test_a_gated_tool_with_a_verb_that_is_not_a_plain_word_names_valid_audit_rows(project, fake_bigquery):
    entries = []
    send = Dummy("dummy.mail", "external-send", intents=("mail_things",), finder="dummy.find", verb="e-mail", past="e-mailed")
    rig = make_rig(project, fake_bigquery=fake_bigquery, registry=ToolRegistry([Dummy("dummy.find", "read", refs=("a",)), send]), audit=entries.append)
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply("mail_things", selector=text_match("thing")))
    assert rig.ask("e-mail it").text.endswith("E-mail this thing? (y/N)")
    assert rig.ask("y").outcome == "action_done"
    assert [entry.action for entry in entries] == ["e_mail_requested", "e_mail_confirmed"]
    from src.storage.audit import write_entry

    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE audit_log (seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, user_hash TEXT, action TEXT, target_hash TEXT, count INTEGER, outcome TEXT, trace_id TEXT, turn_id TEXT, idempotency_key TEXT, detail TEXT)")
    for entry in entries:
        write_entry(conn, entry)  # the names are accepted by the audit log itself
    conn.close()


def test_a_gated_tool_whose_verb_cannot_make_an_audit_name_is_refused_when_the_registry_is_checked():
    for verb in ("!!!", "1234", "x" * 30):
        registry = ToolRegistry([Dummy("find", "read"), Dummy("wipe", "destructive", intents=("w",), finder="find", verb=verb)])
        with pytest.raises(ConfigError, match="cannot name audit rows"):
            registry.validate()
    ToolRegistry([Dummy("find", "read"), Dummy("wipe", "destructive", intents=("w",), finder="find", verb="E-mail")]).validate()
