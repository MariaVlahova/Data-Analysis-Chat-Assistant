"""`finish` (plan 07): the last node of every turn. It sets the outcome and the numbers of the turn's metrics row and keeps
what the next turn needs: the history of the conversation and the last answer (what "save that" saves).

The history keeps the body of an answer, not its envelope (the reading, the scope line, the notes): it has little room, and a
follow-up question needs what the answer said. A report's title is the Composer's, which the Output Guard has checked; when
there is none, it is made from the question only if the Output Guard finds nothing personal in it, because the title is stored
and shown again by "list my reports".

It does not print. The CLI prints the answer once, after the progress is gone and after the Output Guard (S9), and shows a
refusal or a failure in its own panel with the trace ID.
"""
from __future__ import annotations

from src import pii
from src.agent.boundary import node
from src.agent.deps import Deps
from src.agent.envelope import answer_body
from src.agent.state import HISTORY_ANSWER_CHARS, HISTORY_EXCHANGES, TurnState
from src.guards.pseudonymizer import labels_in
from src.observability import turn_metrics
from src.textutil import squash

KEPT_IN_HISTORY = frozenset({"answered", "partial", "action_done", "action_cancelled"})
TITLE_CHARS = 60
FALLBACK_TITLE = "Saved answer"


def fallback_title(deps: Deps, state: TurnState) -> str:
    """A title from the question, for an answer with none. The question never went through the Output Guard, so it is checked
    here, whole and before it is cut (a cut could hide half an e-mail): anything personal gives the generic title. The question
    in state is already masked (`AgentRuntime.run`), so a mask token in it is the sign that something personal was typed."""
    question = squash(state.get("standalone_question") or state.get("question", ""))
    if not question or deps.output_guard.scan(question) or any(token in question for token in pii.TOKENS.values()):
        return FALLBACK_TITLE
    return question[:TITLE_CHARS]


def make_finish(deps: Deps):
    @node(deps, "finish")
    def finish(state: TurnState) -> dict:
        answer = state.get("answer")
        outcome = state.get("outcome") or (("partial" if state.get("partial") else "answered") if answer else "failed")
        metrics = turn_metrics()
        metrics.set_outcome(outcome)
        metrics.set_subquestions(len(state.get("plan", {}).get("subquestions", [])))
        update: dict = {"outcome": outcome}
        if answer and outcome in KEPT_IN_HISTORY:
            exchange = {"user": state.get("question", ""), "assistant": answer_body(answer)[:HISTORY_ANSWER_CHARS]}
            update["history"] = [*state.get("history", []), exchange][-HISTORY_EXCHANGES:]
        if answer:  # the answer is what the user was shown: the labels in it are names for this conversation only, in any process
            seen = state.get("shown_labels", [])
            fresh = [number for number in labels_in(answer) if number not in seen]
            if fresh:
                update["shown_labels"] = [*seen, *fresh]
        if answer and outcome in ("answered", "partial") and state.get("turn_kind") != "tool":
            title = state.get("title") or fallback_title(deps, state)
            update["last_answer"] = {"title": title, "text": answer}
        deps.runtime(state).scratch.clear()
        return update

    return finish
