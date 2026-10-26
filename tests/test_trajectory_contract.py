"""The design, written by hand in `eval/trajectory.py`, against the code (plan 09).

The path checks hold a run to a table of what may follow what, and the memory checks to a table of which node makes which field of the state. Those tables are the DESIGN, typed out
on purpose: a table read off the graph would agree with any bug in it. The price is that they can fall behind a change that was meant. These tests are what makes them fall behind
loudly: change the graph, add a span or add a per-turn field of the state, and one of them fails and says which table to read.
"""
import re
from pathlib import Path

import pytest

from eval import trajectory as T
from src.agent.state import TurnState, fresh_turn_input
from tests.agent_support import make_rig

pytestmark = pytest.mark.trajectory

START, END = "__start__", "__end__"
SRC = Path(__file__).resolve().parent.parent / "src"


def compiled(project, fake_bigquery):
    return make_rig(project, fake_bigquery=fake_bigquery).agent.graph.get_graph()


def test_the_nodes_of_the_design_are_the_nodes_of_the_compiled_graph(project, fake_bigquery):
    graph = compiled(project, fake_bigquery)
    assert set(graph.nodes) - {START, END} == set(T.NODES)


def test_every_edge_of_the_design_is_an_edge_of_the_graph_and_the_graph_has_no_other(project, fake_bigquery):
    edges = {(edge.source, edge.target) for edge in compiled(project, fake_bigquery).edges}
    inner = {(a, b) for a, b in edges if START not in (a, b) and END not in (a, b) and b != T.FAIL_NODE}
    designed = {(a, b) for a, followers in T.NEXT.items() for b in followers}
    assert inner == designed, f"in the graph only: {sorted(inner - designed)}; in the design only: {sorted(designed - inner)}"


def test_the_nodes_that_can_hand_over_to_compose_failure_are_the_ones_the_design_says(project, fake_bigquery):
    edges = {(edge.source, edge.target) for edge in compiled(project, fake_bigquery).edges}
    assert {a for a, b in edges if b == T.FAIL_NODE} == set(T.CAN_FAIL)
    assert T.FAIL_NODE in T.NEXT and T.NEXT[T.FAIL_NODE] == {"finish"}


def test_a_turn_starts_at_policy_context_and_the_only_end_is_finish(project, fake_bigquery):
    edges = {(edge.source, edge.target) for edge in compiled(project, fake_bigquery).edges}
    assert {b for a, b in edges if a == START} == {"policy_context"}
    assert {a for a, b in edges if b == END} == {"finish"}
    assert T.START_NODES == ("policy_context", "hitl_gate")  # the second is where a paused turn resumes: LangGraph runs the interrupted node again


def test_every_span_the_code_opens_by_name_is_in_the_vocabulary_a_dataset_may_use():
    opened = set()
    for path in SRC.rglob("*.py"):
        opened |= set(re.findall(r'\bspan\(\s*"([a-z_.]+)"', path.read_text(encoding="utf-8")))
    unknown = opened - T.SPAN_NAMES - {"llm.call", "turn"}
    assert not unknown, f"spans the trajectory vocabulary does not know: {sorted(unknown)}. Add them to SPAN_NAMES in eval/trajectory.py."


def test_every_per_turn_field_of_the_state_has_a_rule_or_a_reason_it_has_none():
    """`fresh_turn_input` resets these fields. A new one must be added to MAKERS (which node makes it), or to the fields below that other checks cover."""
    covered_elsewhere = {
        "conversation_id", "turn_id", "trace_id", "user_hash", "scope_label",  # check_identity
        "question", "replans_used",  # check_fresh, by their own rules
        "outcome", "partial", "restart_gate",  # set by several nodes, and read by check_memory and check_path through the turn's outcome
    }
    fields = set(fresh_turn_input(conversation_id="c", turn_id="t", trace_id="x", user_hash="h", question="q"))
    assert fields - covered_elsewhere == set(T.MAKERS), sorted((fields - covered_elsewhere) ^ set(T.MAKERS))
    assert set(T.MAKERS) <= set(TurnState.__annotations__)


def test_the_fields_that_carry_over_are_not_reset_and_are_the_ones_check_memory_reads():
    reset = set(fresh_turn_input(conversation_id="c", turn_id="t", trace_id="x", user_hash="h", question="q"))
    assert {"history", "last_answer", "shown_labels"}.isdisjoint(reset)


def test_the_constants_written_out_by_hand_are_the_ones_the_app_uses():
    """Written out on purpose (an oracle that imports the app's own value agrees with a wrong one), so a change on the app's side is what fails here."""
    from src.agent.state import HISTORY_EXCHANGES
    from src.guards.query import scope_label_for
    from src.tools.base import GATED_RISKS

    assert T.HISTORY_EXCHANGES == HISTORY_EXCHANGES
    assert set(T.GATED_RISKS) == set(GATED_RISKS)
    for mode, brands in (("all", ()), ("brands", ("Maple & Co",)), ("brands", ("Maple & Co", "Nordic Peak"))):
        assert T.scope_label(mode, brands) == scope_label_for(mode, brands)
