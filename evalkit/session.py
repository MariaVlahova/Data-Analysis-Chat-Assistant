"""Session log format: the one input shape every metric reads.

Most agent frameworks do not emit evaluation logs natively -- history lives in
a checkpointer, a message list, or is discarded once a request completes. This
module gives you one normalized shape, ``EvalSession``, to convert into before
anything scores it. Write a small adapter for your own agent framework (a
LangGraph checkpointer, an OpenAI Assistants thread, a hand-rolled agent loop)
that produces one of these, and every metric in this framework -- and every
metric you add -- works against it identically, whether the session came from
a live pipeline, a captured production trace, or a hand-built test fixture.

A session also carries turn boundaries. A single-turn session has exactly one,
which costs nothing and keeps one code path; a conversation has several, and
they are what make cross-turn behaviour -- memory, carried facts, cumulative
cost -- measurable at all. Boundaries are derived from the event stream rather
than recorded separately, so a session written before turns existed still
splits correctly when it is read back.

Nothing here names a specific pipeline. ``pipeline`` is a free-form string you
choose per project (e.g. ``"ingestion"``, ``"chat"``, ``"support_agent"``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

#: Chars-per-token used by the built-in estimator when a backend reports no
#: usage of its own. A rough lower bound, not a measurement -- see
#: ``estimated_tokens``.
CHARS_PER_TOKEN = 4


def tokens_from_chars(chars: int) -> int:
    return max(0, chars) // CHARS_PER_TOKEN


@dataclass
class EvalEvent:
    """One step: a user message, a model response, or a tool result."""

    type: str  # "user" | "model" | "tool"
    author: str = ""
    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_call_id: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    timestamp: float = 0.0
    #: Per-token log probabilities, when the provider volunteered them.
    logprobs: list[float] = field(default_factory=list)
    #: True when this event is bookkeeping (e.g. a grader retry re-injected as
    #: a user message) rather than a real turn boundary. Turn splitting skips
    #: these -- see ``EvalSession.turns``.
    is_feedback: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": self.type, "author": self.author, "content": self.content}
        if self.tool_calls:
            payload["tool_calls"] = self.tool_calls
        if self.tool_call_id:
            payload["tool_call_id"] = self.tool_call_id
        if self.usage:
            payload["usage"] = self.usage
        if self.timestamp:
            payload["timestamp"] = self.timestamp
        if self.logprobs:
            payload["logprobs"] = self.logprobs
        if self.is_feedback:
            payload["is_feedback"] = True
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvalEvent:
        return cls(
            type=data.get("type", ""),
            author=data.get("author", ""),
            content=data.get("content", ""),
            tool_calls=list(data.get("tool_calls") or []),
            tool_call_id=data.get("tool_call_id", ""),
            usage=dict(data.get("usage") or {}),
            timestamp=float(data.get("timestamp") or 0.0),
            logprobs=[float(v) for v in (data.get("logprobs") or [])],
            is_feedback=bool(data.get("is_feedback", False)),
        )


def estimated_tokens(events: Iterable[EvalEvent]) -> int:
    """Tokens implied by the text of a stretch of events.

    For backends that report no usage of their own. A lower bound rather than a
    measurement: a ReAct-style loop resends its whole history on every turn and
    none of those re-sends appear in the transcript, so the true figure is
    higher. Anything derived from this should say so.
    """
    chars = 0
    for event in events:
        chars += len(event.content or "")
        for call in event.tool_calls:
            chars += len(json.dumps(call.get("args") or {}))
    return tokens_from_chars(chars)


@dataclass
class EvalTurn:
    """One user turn and everything the system did in response to it.

    ``events`` is a slice of the session's stream, not a copy of it, so a
    turn-scoped metric sees exactly the tool calls and results that turn
    caused -- which is the whole point: a fact recalled at turn 5 must not be
    creditable to a search issued at turn 1.
    """

    index: int
    prompt: str = ""
    answer: str = ""
    events: list[EvalEvent] = field(default_factory=list)
    sources: list[dict[str, Any]] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)
    duration_s: float = 0.0

    @property
    def tool_calls(self) -> list[dict[str, Any]]:
        return [
            {**call, "agent": event.author} for event in self.events for call in event.tool_calls
        ]

    @property
    def token_usage(self) -> dict[str, int]:
        totals: dict[str, int] = {}
        for event in self.events:
            for key, value in event.usage.items():
                totals[key] = totals.get(key, 0) + int(value)
        return totals

    @property
    def estimated_tokens(self) -> int:
        return estimated_tokens(self.events)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "index": self.index,
            "prompt": self.prompt,
            "answer": self.answer,
            "n_events": len(self.events),
        }
        if self.sources:
            payload["sources"] = self.sources
        if self.state:
            payload["state"] = self.state
        if self.duration_s:
            payload["duration_s"] = round(self.duration_s, 4)
        return payload


@dataclass
class EvalSession:
    """One run of your pipeline -- one document job, one chat thread, one
    agent invocation -- normalized to a shape every metric can read.

    ``pipeline`` and ``state`` are intentionally free-form: this framework
    does not know what your system's typed state looks like, so it stores
    whatever dict you hand it and lets your metrics read the keys they care
    about (see ``evalkit.context.ValidationContext``).
    """

    session_id: str
    pipeline: str
    user_id: str = ""
    events: list[EvalEvent] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    #: Per-turn state a capture layer recorded; boundaries are always derived.
    turn_state: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "session_id": self.session_id,
            "pipeline": self.pipeline,
            "user_id": self.user_id,
            "created_at": self.created_at,
            "state": self.state,
            "events": [event.to_dict() for event in self.events],
        }
        if self.turn_state:
            payload["turn_state"] = self.turn_state
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvalSession:
        return cls(
            session_id=data.get("session_id", "unknown"),
            pipeline=data.get("pipeline", ""),
            user_id=data.get("user_id", ""),
            events=[EvalEvent.from_dict(e) for e in data.get("events") or []],
            state=dict(data.get("state") or {}),
            created_at=data.get("created_at", ""),
            turn_state=[dict(t) for t in data.get("turn_state") or []],
        )

    def write(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @classmethod
    def read(cls, path: str | Path) -> EvalSession:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    # -- derived views ------------------------------------------------------- #

    @property
    def tool_calls(self) -> list[dict[str, Any]]:
        """Every tool call in order, each tagged with the agent that made it."""
        calls: list[dict[str, Any]] = []
        for event in self.events:
            for call in event.tool_calls:
                calls.append({**call, "agent": event.author})
        return calls

    @property
    def tool_results(self) -> list[EvalEvent]:
        return [event for event in self.events if event.type == "tool"]

    def tool_result_for(self, tool_call_id: str) -> EvalEvent | None:
        return next((e for e in self.tool_results if e.tool_call_id == tool_call_id), None)

    @property
    def token_usage(self) -> dict[str, int]:
        totals: dict[str, int] = {}
        for event in self.events:
            for key, value in event.usage.items():
                totals[key] = totals.get(key, 0) + int(value)
        return totals

    @property
    def estimated_tokens(self) -> int:
        return estimated_tokens(self.events)

    # -- turns --------------------------------------------------------------- #

    @property
    def turns(self) -> list[EvalTurn]:
        """The session split into user turns, derived from the event stream.

        Events marked ``is_feedback`` (e.g. a grader retry injected as a user
        message) are excluded from the split -- counting one would end a turn
        in the middle of the answer it was trying to fix.
        """
        boundaries = [
            i for i, event in enumerate(self.events) if event.type == "user" and not event.is_feedback
        ]
        if not boundaries:
            # No user message at all (e.g. a batch/ingestion job): one turn
            # over everything.
            return [self._turn(0, 0, len(self.events))]
        edges = [*boundaries, len(self.events)]
        return [self._turn(n, start, edges[n + 1]) for n, start in enumerate(boundaries)]

    def _turn(self, index: int, start: int, end: int) -> EvalTurn:
        events = self.events[start:end]
        recorded = self.turn_state[index] if index < len(self.turn_state) else {}
        prompt = next((e.content for e in events if e.type == "user" and not e.is_feedback), "")
        answer = next(
            (e.content for e in reversed(events) if e.type == "model" and e.content.strip()), ""
        )
        if self.is_single_turn:
            # The capture layer's own answer wins on a single-turn session, if
            # it recorded one -- it resolves the same message list the live
            # system does, interrupts and retries included.
            answer = str(self.state.get("final_answer") or answer)
        return EvalTurn(
            index=index,
            prompt=prompt,
            answer=str(recorded.get("final_answer", answer) or ""),
            events=events,
            sources=list(recorded.get("sources") or (self.state.get("sources") or [])),
            state={k: v for k, v in recorded.items() if k not in {"final_answer", "sources"}},
            duration_s=float(recorded.get("duration_s") or 0.0),
        )

    @property
    def is_single_turn(self) -> bool:
        return sum(1 for e in self.events if e.type == "user" and not e.is_feedback) <= 1

    def trace(self) -> dict[str, Any]:
        """The trace block a report can attach to each sample."""
        return {
            "events": [event.to_dict() for event in self.events],
            "tool_calls": self.tool_calls,
            "token_usage": self.token_usage,
            "turns": [turn.to_dict() for turn in self.turns],
            "timings": self.state.get("timings") or {},
        }


#: A converter from your framework's native message/history type into events.
MessageConverter = Callable[[Iterable[Any], str], list[EvalEvent]]


__all__ = [
    "CHARS_PER_TOKEN",
    "EvalEvent",
    "EvalSession",
    "EvalTurn",
    "MessageConverter",
    "estimated_tokens",
    "tokens_from_chars",
]
