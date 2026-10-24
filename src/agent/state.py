"""The graph state (plan 07): plain, JSON-serializable types only, so a checkpointer can save it.

Never in state: DataFrames, real customer IDs, report IDs, the pseudonymizer, the budget, API clients, the SQL Guard's
`GuardedQuery`, and any text the Output Guard has not passed. Those live in `Deps` and `TurnRuntime`, which the node
functions close over (`deps.py`). What is here is questions, plans, guarded results (masked, with fake customer IDs and at
most `guards.rows_to_model` rows each), counts, and the final answer.

State carries over between the turns of one conversation (the checkpoint thread is the conversation). So every turn starts
from `fresh_turn_input`, which resets every per-turn field. Only `history`, `last_answer` and `shown_labels` are left out on
purpose: they are how a follow-up question and "save that" know what came before, and which customer labels the user has seen.
"""
from __future__ import annotations

from typing import Any, Literal, TypedDict

from src.errors import AgentError

Intent = str  # an Input Guard intent: the base intents in `locked_rules.BASE_INTENTS` plus the intents of registered tools
TurnKind = Literal["simple", "why", "report", "tool"]
Outcome = Literal["answered", "partial", "refused", "action_done", "action_cancelled", "failed"]

# Result statuses of one sub-question. The Composer is told every one, so gaps are stated and never hidden.
OK, EMPTY, BLOCKED, TOO_EXPENSIVE, TOO_SLOW, FAILED, SKIPPED = (
    "ok", "empty", "blocked", "too_expensive", "too_slow", "failed", "skipped"
)
ANSWERABLE = frozenset({OK, EMPTY})  # a result the answer can stand on: data, or an honest "no data"
GAP_STATUSES = frozenset({BLOCKED, TOO_EXPENSIVE, TOO_SLOW, FAILED, SKIPPED})

HISTORY_EXCHANGES = 6  # how many earlier exchanges the Input Guard may see
HISTORY_ANSWER_CHARS = 600  # an earlier answer is cut to this many characters in the history


class SubQuestion(TypedDict):
    id: str
    text: str
    depends_on: list[str]
    tables: list[str]


class Plan(TypedDict, total=False):
    subquestions: list[SubQuestion]
    assumptions: list[str]
    date_range: dict[str, str] | None  # {start, end, label}
    notices: list[str]  # for the answer's notes, for example "only the first 4 sub-questions were run"


class SubResult(TypedDict, total=False):
    status: str
    final_sql: str  # the SQL that ran, as the SQL Guard wrote it
    interpretation: str  # one sentence on how vague words were read
    guarded: dict[str, Any] | None  # a `GuardedResult` as a dict: masked rows, notices, summary
    error_kind: str | None
    error_message: str | None  # the friendly text the error carried, so a permanent setup problem is not told as a transient one
    rule_id: str | None
    notice: str | None  # a refusal text or another sentence for the answer's notes
    scope_label: str | None


class Attempts(TypedDict, total=False):
    repairs: int
    broaden_used: int  # the widening was asked for (the writer may still fail, or be refused)
    broaden_ran: int  # a widening query was run: the only thing that lets the answer say the search was widened
    fingerprints: list[str]
    last_error_signature: str | None


class ErrorRecord(TypedDict, total=False):
    kind: str
    detail: str  # masked, short
    rule_id: str | None
    message: str  # the friendly text for the user


class PendingAction(TypedDict, total=False):
    tool: str
    args: dict[str, Any]  # the selector, never IDs
    intent: str
    request_turn_id: str
    id_set_hash: str
    count: int
    level: int
    preview: str
    prompt: str
    expected_reply: str
    note: str | None  # shown above the preview, for example after a stale confirmation


class Exchange(TypedDict):
    user: str
    assistant: str


class TurnState(TypedDict, total=False):
    # identity
    conversation_id: str
    turn_id: str
    trace_id: str
    user_hash: str
    scope_label: str
    # input
    question: str
    history: list[Exchange]  # carries over between turns
    last_answer: dict[str, str] | None  # {title, text} of the newest answered turn; carries over
    shown_labels: list[str]  # the numbers of the `Customer #NNNNN` labels any answer showed; carries over (a label is only a random number)
    # guard
    intent: Intent | None
    selector: dict[str, str | None] | None
    standalone_question: str | None
    turn_kind: TurnKind | None
    refusal: str | None  # "out_of_scope" or "injection": the Input Guard's reason for a refusal
    # context
    context: dict[str, Any]
    # plan
    plan: Plan
    # loop
    results: dict[str, SubResult]
    attempts: dict[str, Attempts]
    replans_used: int
    # evidence
    evidence: dict[str, Any]  # {enough, missing}
    # output (only after the Output Guard has passed)
    title: str | None
    answer: str | None
    notices: list[str]
    partial: bool
    # action
    pending_action: PendingAction | None
    reply: str | None  # the user's typed reply to a confirmation. Only `hitl_gate` sets it, from the resume value
    restart_gate: bool  # a stale confirmation: ask again with the new preview
    # outcome
    errors: list[ErrorRecord]
    fatal: bool  # a node failed: the next step is `compose_failure`
    outcome: Outcome | None


def error_record(err: AgentError) -> ErrorRecord:
    return {"kind": err.kind.value, "detail": err.detail, "rule_id": err.rule_id, "message": err.user_message}


def fresh_turn_input(
    *, conversation_id: str, turn_id: str, trace_id: str, user_hash: str, question: str,
) -> TurnState:
    """The input of a new turn: every per-turn field reset, `history`, `last_answer` and `shown_labels` left to carry over."""
    return {
        "conversation_id": conversation_id, "turn_id": turn_id, "trace_id": trace_id, "user_hash": user_hash,
        "scope_label": "", "question": question,
        "intent": None, "selector": None, "standalone_question": None, "turn_kind": None, "refusal": None,
        "context": {}, "plan": {}, "results": {}, "attempts": {}, "replans_used": 0, "evidence": {},
        "title": None, "answer": None, "notices": [], "partial": False,
        "pending_action": None, "reply": None, "restart_gate": False,
        "errors": [], "fatal": False, "outcome": None,
    }


def with_error(state: TurnState, err: AgentError) -> list[ErrorRecord]:
    """The state's errors plus one more. The state has no reducer, so a node returns the whole list."""
    return [*state.get("errors", []), error_record(err)]
