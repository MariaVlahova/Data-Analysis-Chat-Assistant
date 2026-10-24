"""The error boundary (plan 07, the loop-engineering "top-level error boundary").

Every node is wrapped by `node(...)`. It opens the node's span, says what is happening (progress), and catches whatever the
node raises: an `AgentError` as it is, anything else as INTERNAL with the exception class and call sites only, never its text.
The error goes into `state.errors` and the graph moves on to `compose_failure`, which ends in a friendly message with a
trace ID. The span records the error on its way through, so `/trace` shows which step failed and why.

Two things pass straight through: LangGraph's own control flow (`interrupt()` raises to pause for the user, which is not a
failure) and KeyboardInterrupt, which is not an `Exception`.

If a node fails, the text it was holding for the Output Guard is dropped here. Nothing unchecked outlives a failure.
"""
from __future__ import annotations

import functools
import logging
from typing import Any, Callable

from src.agent.deps import Deps
from src.agent.state import TurnState, with_error
from src.errors import AgentError
from src.observability import current_trace_id, span
from src.observability.tracing import is_control_flow

log = logging.getLogger(__name__)

NodeFn = Callable[[TurnState], dict[str, Any]]


def node(deps: Deps, name: str, *, kind: str = "step", progress: str | None = None) -> Callable[[NodeFn], NodeFn]:
    def decorate(fn: NodeFn) -> NodeFn:
        @functools.wraps(fn)
        def run(state: TurnState) -> dict[str, Any]:
            try:
                with span(name, kind):
                    if progress:
                        deps.runtime(state).say(progress)
                    return fn(state) or {}
            except Exception as exc:  # noqa: BLE001 - the boundary catches everything a node raises
                if is_control_flow(exc):
                    raise  # the HITL Gate pausing for the user
                err = AgentError.from_exception(exc, current_trace_id())
                _drop_unchecked_text(deps, state)
                return {"errors": with_error(state, err), "fatal": True}

        return run

    return decorate


def _drop_unchecked_text(deps: Deps, state: TurnState) -> None:
    try:
        runtime = deps.runtime(state)
    except AgentError:
        return
    runtime.drop_unchecked()
