"""What a judge may read of a conversation, for the offline runs (`eval/evaluate.py`) and for the chat's `/eval` alike (plan 09).

The words of the user and of the app, the path through the graph, the SQL of the queries (as the trace stores it, masked), how many rows each returned and
the confirmation events. Never a result row. A turn in which personal data was found is not copied: its text is withheld, its SQL is dropped, and the judge
scores nothing of it.

This module imports no model SDK and no `evalkit`, so the chat can load it at startup.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from src import pii

WITHHELD = "[withheld: personal data was found in this turn]"
NODE_SPANS = (
    "input_guard", "context_assembly", "planner", "sql_loop", "evidence_check", "composer", "tool_router", "prepare_action", "hitl_gate", "execute_action",
    "compose_failure",
)
LIVE_PIPELINE = "chat"  # the `pipeline` of a real conversation; the rubrics list golden, attack and chaos, and `/eval` adds this one


def turn_evidence(
    *, say: str, outcome: str, text: str, seconds: float, spans: Sequence[Mapping[str, Any]], row: Mapping[str, Any], withheld: bool = False,
    reference: Mapping[str, Any] | None = None, expects: Mapping[str, bool] | None = None,
) -> dict[str, Any]:
    """One turn, as the files of a run and the judge read it. `reference` and `expects` come from a golden case (what a correct answer states, what the
    case promised); a real conversation has neither."""
    queries = [] if withheld else [
        {"sql": str(attrs.get("sql", "")), "rows": attrs.get("rows"), "status": span.get("status")}
        for span in spans if span.get("name") == "bq.query" for attrs in [span.get("attrs") or {}]
    ]
    confirmations = [
        {"event": item["name"].split(".", 1)[1], "level": (item.get("attrs") or {}).get("level"), "count": (item.get("attrs") or {}).get("count")}
        for span in spans for item in span.get("events") or [] if str(item.get("name", "")).startswith("hitl.")
    ]
    return {
        "say": say, "outcome": outcome, "turn_kind": row.get("turn_kind"), "withheld": withheld, "text": WITHHELD if withheld else text,
        "seconds": round(seconds, 1), "path": [name for name in dict.fromkeys(str(span.get("name")) for span in spans) if name in NODE_SPANS],
        "queries": queries, "confirmations": confirmations, "repairs": int(row.get("repairs") or 0), "replans": int(row.get("replans") or 0),
        "subquestions": int(row.get("subquestions") or 0), "llm_calls": int(row.get("llm_calls") or 0),
        "reference": dict(reference) if reference else None,
        # what the entry promised, so a rubric that did not fire is "not exercised" and not "does not apply" (never shown to the judge)
        "expects": dict(expects) if expects else {"figures": False, "confirmation": False},
    }


@dataclass(frozen=True)
class LiveTurn:
    """One turn of the chat that is running, kept in memory so `/eval` can judge it. The text is what the user saw (it passed the Output Guard, or it is the
    friendly message of a refusal or a failure); what the user typed was masked first, as in a trace. Nothing here is written to a file."""

    index: int  # 1-based, in this chat
    turn_id: str
    trace_id: str
    conversation_id: str
    say: str
    text: str
    outcome: str
    seconds: float
    row: Mapping[str, Any]  # the turn's metrics row: counts, tokens and the models that answered

    @property
    def models(self) -> tuple[str, ...]:
        return tuple(str(model) for model in self.row.get("models") or ())


def chat_evidence(
    session_id: str, *, user_id: str, scope_mode: str, brands: Sequence[str], turns: Sequence[LiveTurn],
    read_spans: Callable[[str], Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    """The evidence of a real conversation, in the shape `session_from_evidence` reads. `read_spans(trace_id)` gives the stored spans of a turn (an empty
    list when the trace is gone). A turn whose text still holds personal data (the Output Guard should have stopped it) is withheld, as in the offline runs."""
    items: list[dict[str, Any]] = []
    for turn in turns:
        leaked = bool(pii.scan(turn.text, extended=True))
        items.append(turn_evidence(
            say=turn.say, outcome=turn.outcome, text=turn.text, seconds=turn.seconds, spans=[] if leaked else read_spans(turn.trace_id), row=turn.row,
            withheld=leaked,
        ))
    return {
        "id": session_id, "kind": LIVE_PIPELINE, "title": "a real conversation", "user": user_id, "scope_mode": scope_mode, "brands": list(brands),
        "other_brands": [], "turns": items,
    }
