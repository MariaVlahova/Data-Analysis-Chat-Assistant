"""``ValidationContext``: everything a metric function is allowed to read.

This is the one interface every custom metric -- static, computation, or
judge-graded -- is written against. It never depends on your application's
settings, models, or database; it only reads the normalized ``EvalSession``
plus whatever ground truth and extras you attach for a given run. That is what
lets the same metric function run identically offline, against a live
pipeline, and inside a unit test with a hand-built session.

When ``turn`` is set the context narrows to that turn: ``state`` reports the
turn's answer and sources, and the tool views report only the calls that turn
made. A metric written against the whole-session view therefore scores one
turn of a conversation without knowing conversations exist -- see
``EvalSession.turns``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from evalkit.session import EvalEvent, EvalSession, EvalTurn


@dataclass
class ValidationContext:
    """The read-only view a metric scores against.

    ``reference`` is your per-sample ground truth (expected values, gold
    answers, budgets) -- whatever your golden set declares. ``extras`` is an
    open bag for anything else a metric needs that does not belong on every
    context: a snapshot of an external store, a price table, model names, a
    project's own settings object. Neither field is interpreted by the
    framework; both exist purely so your metric functions have somewhere to
    read project-specific input from without the framework needing to know
    about it.
    """

    session: EvalSession
    reference: dict[str, Any] | None = None
    turn: EvalTurn | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def state(self) -> dict[str, Any]:
        if self.turn is None:
            return self.session.state
        return {
            **self.session.state,
            **self.turn.state,
            "final_answer": self.turn.answer,
            "sources": self.turn.sources,
        }

    @property
    def pipeline(self) -> str:
        return self.session.pipeline

    @property
    def turn_index(self) -> int:
        return self.turn.index if self.turn is not None else 0

    # -- event views, narrowed to the turn when there is one ----------------- #

    @property
    def events(self) -> list[EvalEvent]:
        return self.turn.events if self.turn is not None else self.session.events

    @property
    def tool_calls(self) -> list[dict[str, Any]]:
        return self.turn.tool_calls if self.turn is not None else self.session.tool_calls

    @property
    def tool_results(self) -> list[EvalEvent]:
        return [event for event in self.events if event.type == "tool"]

    def tool_result_for(self, tool_call_id: str) -> EvalEvent | None:
        return next((e for e in self.tool_results if e.tool_call_id == tool_call_id), None)

    @property
    def prior_turns(self) -> list[EvalTurn]:
        """Turns that completed before this one, oldest first."""
        if self.turn is None:
            return []
        return [turn for turn in self.session.turns if turn.index < self.turn.index]

    # -- convenience accessors ------------------------------------------------ #

    def ref(self, key: str, default: Any = None) -> Any:
        """One field from this sample's ground truth/reference dict."""
        return (self.reference or {}).get(key, default)

    def extra(self, key: str, default: Any = None) -> Any:
        """One field from the run-level extras bag (snapshots, config, etc.)."""
        return self.extras.get(key, default)

    def calls_named(self, name: str) -> list[dict[str, Any]]:
        return [call for call in self.tool_calls if call.get("tool") == name]

    def tool_results_json(self, tool_name: str) -> list[dict[str, Any]]:
        """Parsed JSON bodies of results for `tool_name`, in order."""
        ids = {call.get("id") for call in self.calls_named(tool_name)}
        bodies = []
        for event in self.tool_results:
            if event.tool_call_id not in ids:
                continue
            try:
                bodies.append(json.loads(event.content))
            except (json.JSONDecodeError, TypeError):
                continue
        return bodies


__all__ = ["ValidationContext"]
