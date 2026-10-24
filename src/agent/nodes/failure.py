"""`refuse` and `compose_failure` (plan 07, F7, F32, F33, F35, F36): the honest ends of a turn.

`refuse` is not a failure: the Input Guard said the message is out of scope or looks like an injection, and the answer is a
polite refusal from a template (F7, F17).

`compose_failure` turns whatever stopped the turn into one friendly message, which the CLI shows with the trace ID. It is
reached when a node raised (the error boundary), or when no sub-question has anything to show. It chooses the most useful
cause: a refusal for a guard violation (with the text of the rule that fired), "narrow the question" for a query that was too
expensive or too slow, otherwise the error that ended the turn. It never shows data: whatever a guard held back stays held back.
"""
from __future__ import annotations

from src.agent.boundary import node
from src.agent.deps import Deps
from src.agent.nodes.input_guard import REFUSAL_INJECTION, REFUSAL_SESSION_LABEL
from src.agent.state import BLOCKED, TOO_EXPENSIVE, TOO_SLOW, ErrorRecord, TurnState, with_error
from src.errors import REFUSAL_KINDS, AgentError, ErrorKind, default_message
from src.guards.refusals import refusal_text, violation

REFUSAL_INJECTION_TEXT = (
    "I can't follow instructions that change how I work or ask me to bypass my checks. "
    "I can answer questions about the store's orders, products, customers and sales."
)


def make_refuse(deps: Deps):
    @node(deps, "refuse")
    def refuse(state: TurnState) -> dict:
        reason = state.get("refusal")
        if reason == REFUSAL_SESSION_LABEL:  # a guard violation with a rule of its own, like a refusal of the SQL Guard
            err = violation("SESSION_LABEL", "the message names a customer label")
            return {"errors": with_error(state, err), "answer": err.user_message, "outcome": "refused"}
        text = REFUSAL_INJECTION_TEXT if reason == REFUSAL_INJECTION else default_message(ErrorKind.OUT_OF_SCOPE)
        err = AgentError(ErrorKind.OUT_OF_SCOPE, text, detail=f"refused by the Input Guard: {reason}")
        return {"errors": with_error(state, err), "answer": text, "outcome": "refused"}

    return refuse


def _chosen_error(state: TurnState) -> ErrorRecord:
    """The error to tell the user about."""
    errors = state.get("errors", [])
    if state.get("fatal") and errors:
        return errors[-1]  # the node that raised
    results = state.get("results", {})
    by_status = {status: [r for r in results.values() if r.get("status") == status] for status in (BLOCKED, TOO_EXPENSIVE, TOO_SLOW)}
    if by_status[BLOCKED]:
        first = by_status[BLOCKED][0]
        rule = first.get("rule_id")
        text = first.get("notice") or (refusal_text(rule) if rule else default_message(ErrorKind.GUARD_VIOLATION))
        return {"kind": ErrorKind.GUARD_VIOLATION.value, "detail": f"every sub-question was stopped; rule {rule}", "rule_id": rule, "message": text}
    for status, kind in ((TOO_EXPENSIVE, ErrorKind.QUERY_TOO_EXPENSIVE), (TOO_SLOW, ErrorKind.QUERY_TIMEOUT)):
        if by_status[status]:
            return {"kind": kind.value, "detail": "every sub-question that ran was too big or too slow", "rule_id": None, "message": default_message(kind)}
    failed = [r for r in results.values() if r.get("error_kind")]
    if failed:
        kind = ErrorKind(failed[0]["error_kind"])
        return {"kind": kind.value, "detail": "every sub-question failed", "rule_id": None, "message": failed[0].get("error_message") or default_message(kind)}
    if errors:
        return errors[-1]
    return {"kind": ErrorKind.INTERNAL.value, "detail": "the turn ended with nothing to show", "rule_id": None, "message": default_message(ErrorKind.INTERNAL)}


def make_compose_failure(deps: Deps):
    @node(deps, "compose_failure")
    def compose_failure(state: TurnState) -> dict:
        chosen = _chosen_error(state)
        kind = ErrorKind(chosen["kind"])
        errors = state.get("errors", [])
        if not errors or errors[-1] != chosen:
            errors = [*errors, chosen]  # the adapter reads the last one
        return {
            "errors": errors,
            "answer": chosen.get("message") or default_message(kind),
            "outcome": "refused" if kind in REFUSAL_KINDS else "failed",
            "partial": False,
        }

    return compose_failure
