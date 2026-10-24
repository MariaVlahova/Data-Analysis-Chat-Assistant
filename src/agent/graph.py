"""The LangGraph state graph (plan 07, decision 3.1): fixed steps and two bounded loops (SQL repair inside `sql_loop`, replan).

    policy_context -> input_guard -+-> refuse ---------------------------------------------+
                                   +-> describe_schema ------------------------------+    |
                                   +-> tool_router -> prepare_action -> hitl_gate     |    |
                                   |                        |             -> execute_action -+-> output_guard -> finish
                                   |                        +-----------------------^   |
                                   +-> context_assembly -> (planner) -> sql_loop -> evidence_check -> composer ->
                                                                  ^                       |           numeric_check
                                                                  +-- replan (max_replans)+
    any node that fails, or no sub-question with data -> compose_failure -> finish

Every node is wrapped by the error boundary (`boundary.py`): a failure is recorded in state and the next step is
`compose_failure`. Edges out of a node check `state.fatal` first. The graph never names a tool: the router asks the registry.

The checkpointer is a `MemorySaver` here. Plan 08 passes a `SqliteSaver` so the confirmation pause survives a restart.
"""
from __future__ import annotations

from typing import Any, Callable

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from src.agent.deps import Deps
from src.agent.nodes.composer import make_composer, make_numeric_check, make_output_guard
from src.agent.nodes.context import make_context_assembly
from src.agent.nodes.evidence import make_evidence_check
from src.agent.nodes.failure import make_compose_failure, make_refuse
from src.agent.nodes.finish import make_finish
from src.agent.nodes.input_guard import SCHEMA_INTENT, make_input_guard
from src.agent.nodes.planner import make_planner
from src.agent.nodes.policy import make_policy_context
from src.agent.nodes.schema_answer import make_describe_schema
from src.agent.nodes.sql_loop import make_sql_loop
from src.agent.nodes.tools import make_execute_action, make_hitl_gate, make_prepare_action, make_tool_router
from src.agent.prompts.locked_rules import BASE_INTENTS
from src.agent.state import TurnState

Route = Callable[[TurnState], str]
FAIL = "compose_failure"


def _unless_fatal(normal: Route) -> Route:
    def route(state: TurnState) -> str:
        return FAIL if state.get("fatal") else normal(state)

    return route


def build_graph(deps: Deps, checkpointer: Any = None):
    """Compile the graph. `deps` is closed over by the nodes; it is never put in the state or the LangGraph config."""
    deps.registry.validate(reserved_intents=[name for name, _ in BASE_INTENTS])
    graph = StateGraph(TurnState)
    factories = {
        "policy_context": make_policy_context, "input_guard": make_input_guard, "refuse": make_refuse,
        "describe_schema": make_describe_schema, "context_assembly": make_context_assembly, "planner": make_planner,
        "sql_loop": make_sql_loop, "evidence_check": make_evidence_check, "composer": make_composer,
        "numeric_check": make_numeric_check, "output_guard": make_output_guard, "compose_failure": make_compose_failure,
        "tool_router": make_tool_router, "prepare_action": make_prepare_action, "hitl_gate": make_hitl_gate,
        "execute_action": make_execute_action, "finish": make_finish,
    }
    for name, factory in factories.items():
        graph.add_node(name, factory(deps))

    def after_input_guard(state: TurnState) -> str:
        if state.get("refusal"):
            return "refuse"
        intent = state.get("intent")
        if intent == SCHEMA_INTENT:
            return "describe_schema"
        if deps.registry.for_intent(intent) is not None:
            return "tool_router"  # a save, list, delete or undo: no SQL and no answer (the shortcut edge)
        return "context_assembly"

    def after_context(state: TurnState) -> str:
        return "sql_loop" if state.get("turn_kind") == "simple" else "planner"  # Q36: a simple question skips the Planner

    def after_evidence(state: TurnState) -> str:
        evidence = state.get("evidence", {})
        if evidence.get("nothing_to_show"):
            return FAIL
        return "planner" if evidence.get("replan") else "composer"

    def after_tool_router(state: TurnState) -> str:
        return "finish" if state.get("answer") else "prepare_action"

    def after_prepare(state: TurnState) -> str:
        pending = state.get("pending_action") or {}
        if not pending:
            return "output_guard"  # nothing matched: said so (the finder's text is a tool's text, so it is checked), nothing to confirm
        tool = deps.registry.get(pending.get("tool", ""))
        return "hitl_gate" if tool is not None and tool.manifest.gated else "execute_action"

    def after_execute(state: TurnState) -> str:
        return "hitl_gate" if state.get("restart_gate") else "output_guard"

    graph.add_edge(START, "policy_context")
    graph.add_conditional_edges("policy_context", _unless_fatal(lambda s: "input_guard"), ["input_guard", FAIL])
    graph.add_conditional_edges(
        "input_guard", _unless_fatal(after_input_guard), ["refuse", "describe_schema", "tool_router", "context_assembly", FAIL]
    )
    graph.add_edge("refuse", "finish")
    graph.add_conditional_edges("describe_schema", _unless_fatal(lambda s: "output_guard"), ["output_guard", FAIL])
    graph.add_conditional_edges("context_assembly", _unless_fatal(after_context), ["sql_loop", "planner", FAIL])
    graph.add_conditional_edges("planner", _unless_fatal(lambda s: "sql_loop"), ["sql_loop", FAIL])
    graph.add_conditional_edges("sql_loop", _unless_fatal(lambda s: "evidence_check"), ["evidence_check", FAIL])
    graph.add_conditional_edges("evidence_check", _unless_fatal(after_evidence), ["planner", "composer", FAIL])
    graph.add_conditional_edges("composer", _unless_fatal(lambda s: "numeric_check"), ["numeric_check", FAIL])
    graph.add_conditional_edges("numeric_check", _unless_fatal(lambda s: "output_guard"), ["output_guard", FAIL])
    graph.add_conditional_edges("output_guard", _unless_fatal(lambda s: "finish"), ["finish", FAIL])
    graph.add_edge(FAIL, "finish")
    graph.add_conditional_edges("tool_router", _unless_fatal(after_tool_router), ["finish", "prepare_action", FAIL])
    graph.add_conditional_edges("prepare_action", _unless_fatal(after_prepare), ["output_guard", "hitl_gate", "execute_action", FAIL])
    graph.add_conditional_edges("hitl_gate", _unless_fatal(lambda s: "execute_action"), ["execute_action", FAIL])
    graph.add_conditional_edges("execute_action", _unless_fatal(after_execute), ["hitl_gate", "output_guard", FAIL])
    graph.add_edge("finish", END)
    return graph.compile(checkpointer=checkpointer or MemorySaver())
