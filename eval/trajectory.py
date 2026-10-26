"""Trajectory and execution-path checks (plan 09): did the turn take the path the design says, and did the conversation's memory stay what it should be?

Two groups, both pure functions of the evidence a turn leaves (`TurnEvidence`: the spans of its trace, its metrics row, and the conversation's checkpoint right
after it). No model, no data, no network, so they run on every turn of every suite and prove themselves on hand-made evidence.

Sequence and handoff accuracy (read from the spans):
| Check | Asks |
|---|---|
| `check_path` | Do the node spans make a walk that `NEXT` allows, ending at `finish` (or paused at the gate), and does a failed node lead to `compose_failure`, `finish` and no more work? |
| `check_guard_order` | Does each guard run before what it guards: SQL Guard, dry run, query, Result Guard; Output Guard on every answer; the gate and its approval before a destructive tool? |
| `check_routing` | Does the path agree with what the Input Guard decided (intent and turn kind)? |
| `check_loops` | Do the loops stay inside `loop.*`, and does every node outside a loop run once? |
| `check_plan_handoff` | Was every planned sub-question run in this turn, in dependency order (no result left over from an earlier turn), and did the Composer get only answerable evidence? |
| `check_confirmation_handoff` | Does the line after a confirmation prompt resume at the gate, and is the set a user confirmed the set they were shown? |

State drift and memory consistency (read from the checkpoint):
| Check | Asks |
|---|---|
| `check_identity` | Do the conversation, the user and the scope stay the same from turn to turn, and does the state belong to the turn it was read after? |
| `check_fresh` | Is every per-turn field in the state there because a node of THIS turn made it (nothing stale from an earlier turn)? |
| `check_memory` | Do `history`, `last_answer` and `shown_labels` change exactly as the design says, and no other way (a refusal never overwrites what "save that" saves)? |

`NEXT` and the other tables below are the DESIGN written out by hand, on purpose: an oracle copied from the graph would agree with any bug. `tests/test_trajectory_contract.py`
fails when the compiled graph and `NEXT` differ, so changing the graph means reading this file. The dataset-driven checks (`path_problems`, `memory_problems`) serve
`eval/datasets/trajectories.yaml`.

A check that cannot be made (no trace, no checkpoint, a crashed turn) is `skip`, never `pass`.
"""
from __future__ import annotations

from collections import Counter
from itertools import pairwise
from typing import Any, Iterable, Sequence

from src.agent.nodes.input_guard import typed_text
from src.guards.pseudonymizer import labels_in
from src.llm_steps import Step
from src.scenarios.checks import FAIL, PASS, SKIP, Check, Facts, TurnEvidence, states

# the design, by hand ------------------------------------------------------------------------------------------------------------------------

FAIL_NODE = "compose_failure"
START_NODES = ("policy_context", "hitl_gate")  # a turn starts at the first node, or resumes at the gate it paused at

# node -> the nodes that may follow it. Every node but `refuse`, `compose_failure` and `finish` may also hand over to FAIL_NODE: when it failed, and (for the
# Evidence Check) when no sub-question has anything to show.
NEXT: dict[str, frozenset[str]] = {
    "policy_context": frozenset({"input_guard"}),
    "input_guard": frozenset({"refuse", "describe_schema", "tool_router", "context_assembly"}),
    "refuse": frozenset({"finish"}),
    "describe_schema": frozenset({"output_guard"}),
    "context_assembly": frozenset({"sql_loop", "planner"}),
    "planner": frozenset({"sql_loop"}),
    "sql_loop": frozenset({"evidence_check"}),
    "evidence_check": frozenset({"planner", "composer"}),
    "composer": frozenset({"numeric_check"}),
    "numeric_check": frozenset({"output_guard"}),
    "output_guard": frozenset({"finish"}),
    "tool_router": frozenset({"finish", "prepare_action"}),
    "prepare_action": frozenset({"output_guard", "hitl_gate", "execute_action"}),
    "hitl_gate": frozenset({"execute_action"}),
    "execute_action": frozenset({"hitl_gate", "output_guard"}),
    "compose_failure": frozenset({"finish"}),
    "finish": frozenset(),
}
NODES = tuple(NEXT)
CAN_FAIL = frozenset(NODES) - {"refuse", "compose_failure", "finish"}  # the nodes with an edge to FAIL_NODE
NOTHING_TO_SHOW_FROM = "evidence_check"  # the one node that hands over to FAIL_NODE without having failed

SQL_SPANS = ("guard.sql", "bq.dry_run", "bq.query", "guard.result")
SPAN_NAMES = frozenset({*NODES, "sub_question", "golden.retrieve", "guard.output", *SQL_SPANS})
WORK_AFTER_A_FAILURE = ("llm.", "bq.", "tool.", "guard.sql", "guard.result")  # what must not start once a node has failed

DATA_INTENTS = ("data_question", "why_question", "report_request")
KIND_OF_INTENT = {"data_question": "simple", "schema_question": "simple", "why_question": "why", "report_request": "report"}
TOOL_NODES = ("tool_router", "prepare_action", "hitl_gate", "execute_action")
DATA_NODES = ("context_assembly", "planner", "sql_loop", "evidence_check", "composer", "numeric_check")
MAX_LOOPS = {"hitl_gate": 2}  # a stale confirmation shows the new preview at the gate again in the same turn; every other node outside the replan loop runs once
ANSWERED = ("answered", "partial")
HISTORY_EXCHANGES = 6  # how many exchanges the history keeps; tests/test_trajectory_contract.py ties it to the app's, so a change there means reading this file
GATED_RISKS = ("destructive", "external-send")  # the risk classes of a tool that needs the gate
KEPT_IN_HISTORY = ("answered", "partial", "action_done", "action_cancelled")  # the outcomes whose exchange `finish` keeps
ANSWERED_ANSWERS = KEPT_IN_HISTORY  # the outcomes whose state.answer is the text the user was shown

# Which node makes each per-turn field of the state. A field that is set while none of its makers ran in this turn is left over from an earlier one.
MAKERS: dict[str, frozenset[str]] = {
    "intent": frozenset({"input_guard"}), "turn_kind": frozenset({"input_guard"}), "standalone_question": frozenset({"input_guard"}),
    "selector": frozenset({"input_guard"}), "refusal": frozenset({"input_guard"}),
    "context": frozenset({"context_assembly"}), "plan": frozenset({"context_assembly", "planner"}), "results": frozenset({"sql_loop"}),
    "attempts": frozenset({"sql_loop"}), "evidence": frozenset({"evidence_check"}),
    "pending_action": frozenset({"tool_router", "prepare_action", "execute_action"}), "reply": frozenset({"hitl_gate"}),
    "title": frozenset({"output_guard"}), "notices": frozenset({"output_guard"}),
    "answer": frozenset({"output_guard", "refuse", "compose_failure", "tool_router"}),
    # or any node that failed. `sql_loop` records the turn limit it met and carries on with what it gathered; `composer` records the model error it built the answer around.
    "errors": frozenset({"refuse", "tool_router", "execute_action", "compose_failure", "sql_loop", "composer"}),
    "fatal": frozenset({"compose_failure"}),
}
RESUME_KEEPS = frozenset({"intent", "turn_kind", "standalone_question", "selector", "pending_action"})  # a turn that resumes at the gate carries these from the one that asked


def scope_label(mode: str, brands: Sequence[str]) -> str:
    """The words for what a user may see, as the state records them. Written out here and tied to `scope_label_for` by the contract test."""
    return "all brands" if mode == "all" else "your brands: " + ", ".join(brands)


# the trace ---------------------------------------------------------------------------------------------------------------------------------

def token(span: dict[str, Any]) -> str:
    """A span as a path expectation names it: its name, and for a model call `llm.<step>` (`llm.sql_write`)."""
    name = str(span.get("name"))
    return f"llm.{(span.get('attrs') or {}).get('step', '?')}" if name == "llm.call" else name


def is_token(value: str) -> bool:
    """Is `value` something a trace can contain? A typo in a dataset must not pass by never matching."""
    return value in SPAN_NAMES or (value.startswith("tool.") and len(value) > len("tool.")) or value in {f"llm.{step.value}" for step in Step}


def tokens(ev: TurnEvidence) -> list[str]:
    return [token(span) for span in ev.spans if span.get("name") != "turn"]


def root_of(ev: TurnEvidence) -> dict[str, Any] | None:
    return next((span for span in ev.spans if span.get("parent_id") is None and span.get("name") == "turn"), None)


def node_spans(ev: TurnEvidence) -> list[dict[str, Any]]:
    """The graph nodes the turn ran, in order: the spans just under the turn's root."""
    root = root_of(ev)
    return [] if root is None else [span for span in ev.spans if span.get("parent_id") == root.get("span_id") and span.get("name") in NEXT]


def _usable(ev: TurnEvidence) -> tuple[list[dict[str, Any]], str]:
    """The node spans of the turn, or why there is nothing to judge."""
    if ev.crashed:
        return [], "the turn crashed before it left a path"
    root = root_of(ev)
    if root is None:
        return [], "no trace was read for this turn"
    if (root.get("attrs") or {}).get("spans_dropped"):
        return [], "the trace dropped spans, so its path is incomplete"
    nodes = node_spans(ev)
    return (nodes, "") if nodes else ([], "the turn ran no graph node")


def _verdict(name: str, problems: Sequence[str], held: str) -> Check:
    problems = list(dict.fromkeys(problems))
    if not problems:
        return Check(name, PASS, held)
    return Check(name, FAIL, "; ".join(problems[:3]) + (f" (and {len(problems) - 3} more)" if len(problems) > 3 else ""))


def _attrs(span: dict[str, Any]) -> dict[str, Any]:
    return span.get("attrs") or {}


# sequence -----------------------------------------------------------------------------------------------------------------------------------

def check_path(ev: TurnEvidence, facts: Facts | None = None) -> Check:
    """The node spans are a walk the design allows, from the first node (or the gate, on a resume) to `finish` (or the pause). After a node fails the next
    step is `compose_failure`, then `finish`, and no model call, query or tool starts again."""
    nodes, why = _usable(ev)
    if why:
        return Check("valid path", SKIP, why)
    names = [node["name"] for node in nodes]
    problems: list[str] = []
    if names[0] not in START_NODES:
        problems.append(f"starts at {names[0]}")
    for before, after in pairwise(nodes):
        a, b = before["name"], after["name"]
        if before.get("status") == "error":
            if b != FAIL_NODE:
                problems.append(f"{a} failed and {b} ran after it, not {FAIL_NODE}")
        elif b == FAIL_NODE and a == NOTHING_TO_SHOW_FROM:
            if ev.state and not (ev.state.get("evidence") or {}).get("nothing_to_show"):  # the one edge that is taken without a failure, and only for this reason
                problems.append("evidence_check handed over to compose_failure although it did not find that there was nothing to show")
        elif b not in NEXT[a]:
            problems.append(f"{a} was followed by {b}")
    paused = ev.outcome == "awaiting_confirmation"
    if paused and names[-1] != "hitl_gate":
        problems.append(f"paused for a confirmation but ends at {names[-1]}")
    elif not paused and names[-1] != "finish":
        problems.append(f"ends at {names[-1]}, not finish")
    if names.count("finish") > 1:
        problems.append("finish ran twice")
    failed = next((i for i, node in enumerate(nodes) if node.get("status") == "error"), None)
    if failed is not None and failed + 1 < len(nodes):
        after_failure = ev.spans[next(i for i, span in enumerate(ev.spans) if span is nodes[failed + 1]):]
        problems += [f"{token(span)} started after {nodes[failed]['name']} failed" for span in after_failure if str(span.get("name")).startswith(WORK_AFTER_A_FAILURE)]
    return _verdict("valid path", problems, " > ".join(names))


def _sql_sequence(children: Iterable[dict[str, Any]]) -> list[str]:
    """Walk one sub-question's guard, dry run, query and Result Guard spans in order. A guard that comes after what it guards is a problem; so is a query whose
    rows never reached the Result Guard."""
    problems: list[str] = []
    state = "idle"  # idle -> guarded (SQL Guard passed) -> dry (dry run passed) -> ran (query passed) -> idle (Result Guard applied)
    for span in children:
        name, ok = span["name"], span.get("status") == "ok"
        if name == "guard.sql":
            if state == "ran":
                problems.append("the rows of a query were not put through the Result Guard before the next query was written")
            state = "guarded" if ok else "idle"
        elif name == "bq.dry_run":
            if state != "guarded":
                problems.append("a dry run without a passed SQL Guard before it")
            state = "dry" if ok else "idle"
        elif name == "bq.query":
            if state != "dry":
                problems.append("a query without a passed dry run before it" if state == "guarded" else "a query without a passed SQL Guard before it")
            state = "ran" if ok else "idle"
        elif name == "guard.result":
            if state != "ran":
                problems.append("the Result Guard ran with no query result before it")
            state = "idle"
    if state == "ran":
        problems.append("the rows of the last query never went through the Result Guard")
    return problems


def check_guard_order(ev: TurnEvidence, facts: Facts | None = None) -> Check:
    """Each guard runs before what it guards."""
    nodes, why = _usable(ev)
    if why:
        return Check("guards before effects", SKIP, why)
    problems: list[str] = []
    by_id = {span.get("span_id"): span for span in ev.spans}
    for span in ev.spans:  # a query outside a sub-question has no SQL Guard of the turn before it
        if str(span.get("name")).startswith("bq.") and (by_id.get(span.get("parent_id")) or {}).get("name") != "sub_question":
            problems.append(f"{span['name']} ran outside a sub-question")
    for sub in (span for span in ev.spans if span.get("name") == "sub_question"):
        problems += _sql_sequence(span for span in ev.spans if span.get("parent_id") == sub.get("span_id") and span.get("name") in SQL_SPANS)
    for node in (node for node in nodes if node["name"] == "output_guard"):
        if not any(span.get("parent_id") == node.get("span_id") and span.get("name") == "guard.output" for span in ev.spans):
            problems.append("the Output Guard step ran without the guard itself")
    names = [node["name"] for node in nodes]
    for tool in (span for span in ev.spans if str(span.get("name")).startswith("tool.") and _attrs(span).get("risk_class") in GATED_RISKS):
        gate_before = "hitl_gate" in names[: names.index("execute_action")] if "execute_action" in names else False
        approvals = [item.get("ts", "") for span in ev.spans for item in span.get("events") or [] if item.get("name") == "hitl.approved"]
        if not gate_before:
            problems.append(f"{tool['name']} ran with no confirmation gate before it in the turn")
        if not any(ts and ts <= str(tool.get("start_ts", "")) for ts in approvals):
            problems.append(f"{tool['name']} started before an approval was recorded")
    return _verdict("guards before effects", problems, "SQL Guard > dry run > query > Result Guard, Output Guard, gate before a delete")


def check_routing(ev: TurnEvidence, facts: Facts | None = None) -> Check:
    """The path agrees with what the Input Guard decided: its intent and the turn kind the code derived from it."""
    nodes, why = _usable(ev)
    if why:
        return Check("route follows the decision", SKIP, why)
    names = [node["name"] for node in nodes]
    seen = set(names)
    attrs = _attrs(root_of(ev) or {})
    intent, kind = attrs.get("intent") or ev.row.get("intent"), attrs.get("turn_kind") or ev.row.get("turn_kind")
    resumed = names[0] == "hitl_gate"
    problems: list[str] = []

    def forbid(group: Iterable[str], because: str) -> None:
        problems.extend(f"{name} ran for {because}" for name in group if name in seen)

    if intent in KIND_OF_INTENT and kind != KIND_OF_INTENT[intent]:
        problems.append(f"{intent} was treated as a {kind} turn, not {KIND_OF_INTENT[intent]}")
    if intent == "out_of_scope" and "refuse" not in seen:
        problems.append("the Input Guard said out of scope and the turn did not end in a refusal")
    if "refuse" in seen and intent not in (None, "out_of_scope"):
        problems.append(f"refused with the intent {intent}")
    if intent == "schema_question":
        if "describe_schema" not in seen:
            problems.append("a question about the data's fields did not reach describe_schema")
        forbid((*DATA_NODES, *TOOL_NODES), "a schema question")
    if intent in DATA_INTENTS:
        if "context_assembly" not in seen:
            problems.append(f"a {intent} did not reach context_assembly")
        forbid((*TOOL_NODES, "describe_schema", "refuse"), f"a {intent}")
        if kind == "simple":
            forbid(("planner",), "a simple question")
        if kind in ("why", "report") and "sql_loop" in seen and ("planner" not in seen or names.index("planner") > names.index("sql_loop")):
            problems.append(f"a {kind} question reached sql_loop before the Planner")
    # A delete the Input Guard took for an injection keeps its turn kind (tool) and gets the intent out_of_scope: the refusal replaces the tool route.
    if (kind == "tool" and "refuse" not in seen) or resumed:
        forbid((*DATA_NODES, "describe_schema", "refuse"), "a tool request")
        if not resumed and "tool_router" not in seen:
            problems.append("a tool request did not reach tool_router")
    if (intent == "out_of_scope" or "refuse" in seen or intent == "schema_question" or kind == "tool" or resumed) and any(
        str(span.get("name")).startswith("bq.") for span in ev.spans
    ):
        problems.append("a query ran for a turn that has no data question")
    return _verdict("route follows the decision", problems, f"{intent or 'no intent'} / {kind or 'no kind'}: {' > '.join(names)}")


def check_loops(ev: TurnEvidence, facts: Facts) -> Check:
    """The loops stay inside the configured limits and every node outside a loop runs once."""
    nodes, why = _usable(ev)
    if why:
        return Check("loops are bounded", SKIP, why)
    loop = facts.settings.loop
    replan_runs = 1 + loop.max_replans
    limits = {"planner": replan_runs, "sql_loop": replan_runs, "evidence_check": replan_runs, **MAX_LOOPS}
    problems = [f"{name} ran {count} times, at most {limits.get(name, 1)}" for name, count in Counter(n["name"] for n in nodes).items() if count > limits.get(name, 1)]
    subs = [span for span in ev.spans if span.get("name") == "sub_question"]
    if len(subs) > loop.max_subquestions:
        problems.append(f"{len(subs)} sub-questions ran, at most {loop.max_subquestions}")
    problems += [f"sub-question {sub_id} ran twice" for sub_id, count in Counter(_attrs(s).get("id") for s in subs).items() if count > 1]
    for sub in subs:
        attrs = _attrs(sub)
        if int(attrs.get("repairs") or 0) > loop.sql_repairs:
            problems.append(f"{attrs.get('id')}: {attrs.get('repairs')} repairs, at most {loop.sql_repairs}")
        if int(attrs.get("broaden_used") or 0) > loop.broaden_attempts:
            problems.append(f"{attrs.get('id')}: widened {attrs.get('broaden_used')} times, at most {loop.broaden_attempts}")
    return _verdict("loops are bounded", problems, f"{len(subs)} sub-question(s), {len(nodes)} node(s)")


def check_plan_handoff(ev: TurnEvidence, facts: Facts | None = None) -> Check:
    """What the Planner (or Context Assembly) handed to the SQL loop is what ran, and what the loop handed to the Composer is answerable."""
    nodes, why = _usable(ev)
    if why:
        return Check("plan is what ran", SKIP, why)
    names = [node["name"] for node in nodes]
    if "sql_loop" not in names:
        return Check("plan is what ran", SKIP, "the turn ran no query loop")
    if not ev.state:
        return Check("plan is what ran", SKIP, "the checkpoint could not be read")
    loop_ok = all(node.get("status") != "error" for node in nodes if node["name"] == "sql_loop")
    plan = (ev.state.get("plan") or {}).get("subquestions") or []
    results = ev.state.get("results") or {}
    planned = {sub["id"]: sub for sub in plan}
    ran = [(_attrs(span).get("id"), _attrs(span).get("status"), span.get("status")) for span in ev.spans if span.get("name") == "sub_question"]
    order = [sub_id for sub_id, _, _ in ran]
    problems: list[str] = []
    for sub_id, status, span_status in ran:
        if sub_id not in planned:
            problems.append(f"{sub_id} ran but the plan has no such sub-question")
            continue
        for dep in planned[sub_id].get("depends_on") or []:
            if dep in order and order.index(dep) > order.index(sub_id):
                problems.append(f"{sub_id} ran before {dep}, which it depends on")
        if status is None:  # the span raised before it set a status: the turn's budget or time ran out inside it, and the loop records the sub-question as skipped
            status = "skipped" if span_status == "error" else None
        if sub_id in results and results[sub_id].get("status") != status:
            problems.append(f"{sub_id} ended {status} in the trace and {results[sub_id].get('status')} in the state")
    if loop_ok:
        problems += [f"{sub_id} is in the plan and left no result" for sub_id in planned if sub_id not in results]
        # A result that no sub-question of this turn made is left over: `sql_loop` skips the sub-questions that already have one, so it would answer from the earlier turn's data.
        problems += [f"{sub_id} has a result ({result.get('status')}) and no sub-question ran for it in this turn" for sub_id, result in results.items()
                     if result.get("status") != "skipped" and sub_id not in order]
    problems += [f"a result for {sub_id}, which is not in the plan" for sub_id in results if sub_id not in planned]
    answerable = any(result.get("status") in ("ok", "empty") for result in results.values())
    nothing = bool((ev.state.get("evidence") or {}).get("nothing_to_show"))
    if "evidence_check" in names and all(n.get("status") != "error" for n in nodes if n["name"] == "evidence_check"):
        if nothing and answerable:
            problems.append("the Evidence Check found nothing to show although a result is answerable")
        if not nothing and not answerable:
            problems.append("the Evidence Check found something to show with no answerable result")
    if "composer" in names:
        if nothing:
            problems.append("the Composer ran although the Evidence Check found nothing to show")
        if not answerable:
            problems.append("the Composer ran with no answerable result")
    return _verdict("plan is what ran", problems, f"{len(ran)} of {len(planned)} planned sub-question(s) ran")


def check_confirmation_handoff(turns: Sequence[TurnEvidence], facts: Facts | None = None) -> Check:
    """What goes across a pause. The line after a confirmation prompt is the reply and resumes at the gate, and a turn that resumes at the gate follows a prompt (a regression
    that took "yes" for a new question would leave no approval to compare, so the pairing is checked on its own). And the set of items a user approved is the set the preview
    showed them, compared by the ID-set hash the gate records in `hitl.requested` and `hitl.approved`. A stale confirmation shows a new preview without that event, so an
    approval after one is not compared. (The hash is compared as recorded; which items the tool then deleted is for the delete-flow and audit tests.)"""
    shown: tuple[Any, Any] | None = None
    shown_known, approvals, pauses, resumes, problems = True, 0, 0, 0, []
    waiting = False  # the conversation is at a confirmation prompt
    for ev in turns:
        nodes = node_spans(ev)
        if nodes and not ev.crashed:
            starts_at_gate = nodes[0]["name"] == "hitl_gate"
            if waiting and not starts_at_gate:
                problems.append(f"turn {ev.index}: followed a confirmation prompt but started at {nodes[0]['name']}, not at the gate")
            elif starts_at_gate and not waiting:
                problems.append(f"turn {ev.index}: resumed at the gate although no confirmation was waiting")
            resumes += starts_at_gate
            waiting = ev.outcome == "awaiting_confirmation"
            pauses += waiting
        for span in ev.spans:
            for item in span.get("events") or []:
                name, attrs = item.get("name"), item.get("attrs") or {}
                if name == "hitl.requested":
                    shown, shown_known = (attrs.get("id_set_hash"), attrs.get("count")), True
                elif name == "guard.decision" and attrs.get("guard") == "hitl" and attrs.get("rule") == "STALE_CONFIRMATION":
                    shown_known = False  # a new preview was shown that this event stream does not describe
                elif name == "hitl.approved":
                    approvals += 1
                    if not shown_known:
                        continue
                    if shown is None:
                        problems.append(f"turn {ev.index}: an approval with no preview before it")
                    elif (attrs.get("id_set_hash"), attrs.get("count")) != shown:
                        problems.append(f"turn {ev.index}: approved a set other than the one shown")
    if not (approvals or pauses or resumes):
        return Check("what was approved was shown", SKIP, "no confirmation in the conversation")
    return _verdict("what was approved was shown", problems, f"{pauses} prompt(s), {resumes} resume(s), {approvals} approval(s), each of the set shown")


# state and memory --------------------------------------------------------------------------------------------------------------------------

def _conversation_states(turns: Sequence[TurnEvidence]) -> list[tuple[TurnEvidence, dict[str, Any]]]:
    return [(ev, ev.state) for ev in turns if ev.state and not ev.crashed]


def check_identity(turns: Sequence[TurnEvidence], facts: Facts) -> Check:
    """The conversation, the user and the scope do not change from turn to turn, and each state is the one of the turn it was read after."""
    seen = _conversation_states(turns)
    if not seen:
        return Check("identity and scope hold", SKIP, "the checkpoint could not be read")
    want_hash, want_scope = facts.user.user_hash, scope_label(facts.user.scope_mode, facts.user.brands)
    problems: list[str] = []
    if len({state.get("conversation_id") for _, state in seen}) != 1 or not seen[0][1].get("conversation_id"):
        problems.append("the conversation ID changed or is missing")
    for ev, state in seen:
        if state.get("user_hash") != want_hash:
            problems.append(f"turn {ev.index}: the state names another user")
        if state.get("scope_label") and state["scope_label"] != want_scope:
            problems.append(f"turn {ev.index}: the scope is '{state['scope_label']}', the user's is '{want_scope}'")
        if state.get("trace_id") != ev.trace_id:
            problems.append(f"turn {ev.index}: the state belongs to trace {state.get('trace_id')}, not this turn's")
    ids = [state.get("turn_id") for _, state in seen]
    if len(set(ids)) != len(ids):
        problems.append("two turns share a turn ID")
    return _verdict("identity and scope hold", problems, f"{len(seen)} turn(s), scope '{want_scope}'")


def check_fresh(ev: TurnEvidence, facts: Facts | None = None) -> Check:
    """Every per-turn field of the state is there because a node of this turn made it. A field that is set while none of its makers ran is left over from
    an earlier turn: the drift `fresh_turn_input` exists to prevent."""
    nodes, why = _usable(ev)
    if why:
        return Check("no stale state", SKIP, why)
    if not ev.state:
        return Check("no stale state", SKIP, "the checkpoint could not be read")
    names = [node["name"] for node in nodes]
    seen, resumed = set(names), names[0] == "hitl_gate"
    failed = any(node.get("status") == "error" for node in nodes)
    state, problems = ev.state, []
    for field, makers in MAKERS.items():
        if (resumed and field in RESUME_KEEPS) or not state.get(field):
            continue
        if not (makers & seen) and not (field == "errors" and failed):
            problems.append(f"{field} is set but {' or '.join(sorted(makers))} did not run in this turn")
    if not resumed and state.get("question") != typed_text(ev.say):
        problems.append("the question in the state is not the line typed this turn")
    planners = names.count("planner")
    if not resumed and int(state.get("replans_used") or 0) != max(planners - 1, 0):
        problems.append(f"{state.get('replans_used')} replan(s) recorded for {planners} Planner run(s)")
    return _verdict("no stale state", problems, f"{len(MAKERS)} per-turn fields checked against {len(names)} node(s)")


def check_memory(turns: Sequence[TurnEvidence], facts: Facts | None = None) -> Check:
    """What carries over from one turn to the next changes exactly as the design says. `history` gains this turn's exchange when the turn is kept, and is
    otherwise as it was. `last_answer` is the newest answered data turn and is never overwritten by a refusal, a failure or a tool turn. `shown_labels` only
    grows and holds every label the answers showed."""
    problems: list[str] = []
    before: dict[str, Any] | None = {}  # the conversation's start; None after a turn whose checkpoint could not be read
    judged = 0
    for ev in turns:
        state = ev.state
        if ev.crashed or not state:
            before = None
            continue
        judged += 1
        who = f"turn {ev.index}"
        answer = state.get("answer") or ""
        if before is not None:
            was, now = before.get("history") or [], state.get("history") or []
            if ev.outcome in KEPT_IN_HISTORY and answer:
                if not now or now[-1].get("user") != state.get("question"):
                    problems.append(f"{who}: the exchange of this turn is not the newest in the history")
                elif now != [*was, now[-1]][-HISTORY_EXCHANGES:]:
                    problems.append(f"{who}: the earlier exchanges in the history changed")
                elif not now[-1].get("assistant") or now[-1]["assistant"] not in answer:  # the history keeps the body of the answer, a part of the text the user was shown
                    problems.append(f"{who}: the history's copy of this turn's answer is not a part of the answer")
            elif now != was:
                problems.append(f"{who}: the history changed in a turn that is not kept ({ev.outcome})")
            last_was, last_now = before.get("last_answer"), state.get("last_answer")
            if ev.outcome in ANSWERED and answer and state.get("turn_kind") != "tool":
                if not last_now or last_now.get("text") != answer:
                    problems.append(f"{who}: last_answer is not the answer this turn gave")
                elif not last_now.get("title") or (state.get("title") and last_now["title"] != state["title"]):  # the title is what "save that" stores
                    problems.append(f"{who}: last_answer has {'no title' if not last_now.get('title') else 'a title other than the one this turn gave'}")
            elif last_now != last_was:
                problems.append(f"{who}: last_answer changed in a turn that gave no answer ({ev.outcome})")
            old, new = before.get("shown_labels") or [], state.get("shown_labels") or []
            if new[: len(old)] != old:
                problems.append(f"{who}: a customer label that was shown was forgotten")
        if ev.outcome in ANSWERED_ANSWERS and answer and answer != ev.text:
            problems.append(f"{who}: the answer in the state is not the text the user was shown")
        shown = state.get("shown_labels") or []
        if len(set(shown)) != len(shown):
            problems.append(f"{who}: a customer label is listed twice")
        problems += [f"{who}: label {number} was shown and is not remembered" for number in labels_in(answer) if number not in shown]
        before = state
    if not judged:
        return Check("memory carries over", SKIP, "the checkpoint could not be read")
    return _verdict("memory carries over", problems, f"{judged} turn(s): history, last answer and shown labels")


# the dataset's expectations -----------------------------------------------------------------------------------------------------------------------

def path_problems(ev: TurnEvidence, *, through: Sequence[str] = (), never: Sequence[str] = ()) -> list[str]:
    """`through`: these spans, in this order (other spans may sit between them). `never`: none of these spans."""
    got = tokens(ev)
    remaining = iter(got)
    problems = []
    for wanted in through:
        if not any(seen == wanted for seen in remaining):
            problems.append(f"{wanted} is not in the path in this order" if wanted in got else f"{wanted} is not in the path")
            remaining = iter(got)  # go on from the start, so one missing span does not hide the rest
    problems += [f"{wanted} is in the path" for wanted in never if wanted in got]
    return problems


def standalone_problems(ev: TurnEvidence, has: Sequence[str], has_any: Sequence[str] = ()) -> list[str]:
    """The question as the Input Guard rewrote it keeps what the earlier turns said: every text in `has` (and one of `has_any`) is in it."""
    if not ev.state:
        return ["the checkpoint could not be read"]
    text = str(ev.state.get("standalone_question") or "").casefold()
    problems = [f"the rewritten question lacks '{want}'" for want in has if want.casefold() not in text]
    if has_any and not any(want.casefold() in text for want in has_any):
        problems.append("the rewritten question has none of " + ", ".join(f"'{want}'" for want in has_any))
    return problems


def recall_problems(earlier: TurnEvidence, later: TurnEvidence, figures: Sequence[float], rel_tol: float) -> tuple[list[str], int]:
    """(problems, how many figures the earlier answer stated). The later answer must state each figure the earlier one did, within `rel_tol`: what the
    conversation said once it says again."""
    stated = [value for value in figures if states(value, earlier.text, rel_tol)]
    return [f"does not repeat {value:,.2f}, which turn {earlier.index} stated" for value in stated if not states(value, later.text, rel_tol)], len(stated)


def label_problems(earlier: TurnEvidence, later: TurnEvidence) -> tuple[list[str], int]:
    """(problems, labels in the earlier answer). The same customers keep the same labels for the whole session: asked the same question again, the later answer names exactly the
    earlier labels. A label is what the app counts as one (`labels_in`, which ignores case and spelling like "customer #123456")."""
    first, second = labels_in(earlier.text), labels_in(later.text)
    if set(first) == set(second):
        return [], len(first)
    new, missing = sorted(set(second) - set(first)), sorted(set(first) - set(second))
    return [f"names {', '.join('Customer #' + n for n in new) or 'no new label'} and misses {', '.join('Customer #' + n for n in missing) or 'none'} of turn {earlier.index}'s labels"], len(first)
