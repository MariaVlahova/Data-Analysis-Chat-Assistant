"""Tool manifests and the `Tool` protocol (plan 07, decision 3.14, NFR24).

A tool is described by a manifest: schema, risk class, roles, timeout, cost, and whether it can repeat or undo. The graph
never names a tool. The Tool Router picks one by intent through the registry, so a new tool needs no graph change.
`destructive` and `external-send` tools go through the HITL Gate.

Two rules every tool follows (plan 07, CLAUDE.md):
- The model never sees IDs. It produces an action and a selector. A finder tool turns the selector into `Target`s, and the
  `ref` of a target (the real ID) stays in the node that holds it: never in graph state, a span or a log.
- The owner is `ToolContext.user`, never an argument the model could have written.

Plan 08 replaces `stub_reports.py` with the real store and keeps this interface unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Protocol

from src.access.users import UserContext
from src.textutil import squash

RiskClass = Literal["read", "write", "destructive", "external-send"]
GATED_RISKS = frozenset({"destructive", "external-send"})

SELECTOR_KINDS = ("conversation", "text_match", "all", "last")
MAX_SELECTOR_TEXT = 100  # the model's search text is cut here (plan 07)


@dataclass(frozen=True)
class ToolManifest:
    name: str  # "saved_reports.delete"
    description: str  # one sentence; the Input Guard prompt shows it next to the tool's intents
    input_schema: Mapping[str, str]  # argument name -> what it holds. IDs never appear here: the model never writes them
    risk_class: RiskClass
    allowed_roles: frozenset[str]
    timeout_s: float
    cost_hint: str  # for example "free" or "one SQL query"
    idempotent: bool  # a repeat with the same idempotency key has no second effect
    reversible: bool
    inverse: str | None = None  # the tool that undoes this one
    intents: tuple[str, ...] = ()  # Input Guard intents routed to this tool
    finder: str | None = None  # a gated tool names the read tool that lists what it would change
    noun: str = "item"  # for the confirmation text: "report"
    verb: str = ""  # for the confirmation text: "delete". Empty means the usual verb of the risk class (`action_verb`)

    @property
    def gated(self) -> bool:
        return self.risk_class in GATED_RISKS

    @property
    def action_verb(self) -> str:
        """The word the confirmation uses ("Delete this report?", "Type SEND 3 to confirm")."""
        return self.verb or ("send" if self.risk_class == "external-send" else "delete")


@dataclass(frozen=True)
class Target:
    """One thing a gated action would change, as the user may see it."""

    ref: str = field(repr=False)  # the real ID. Held in the node that found it, never stored or printed
    label: str = ""  # shown in the preview: the report title
    when: str = ""  # shown in the preview: the saved date


@dataclass(frozen=True)
class LastAnswer:
    """The final answer of the newest answered turn, which `save_report` stores (it passed the Output Guard)."""

    title: str
    text: str
    searchable: str = ""  # the part of `text` a search looks in: the answer without its scope line, reading and notes ("" means all of it)


@dataclass(frozen=True)
class ToolContext:
    user: UserContext  # the owner of whatever the tool touches
    conversation_id: str
    turn_id: str
    trace_id: str
    last_answer: LastAnswer | None = None
    idempotency_key: str | None = None


@dataclass(frozen=True)
class ToolResult:
    message: str  # what the user is told. Written by the tool, never by the model
    count: int = 0
    targets: tuple[Target, ...] = ()  # a finder fills this


class Tool(Protocol):
    manifest: ToolManifest

    def run(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult: ...


def clean_selector(raw: Any) -> dict[str, str | None] | None:
    """The selector the model produced, checked in code: `{kind, text}` or None when it cannot be used.

    `text_match` needs text; the other kinds carry none. The text is cut to `MAX_SELECTOR_TEXT` characters.
    """
    if raw is None:
        return None
    kind = getattr(raw, "kind", None) if not isinstance(raw, Mapping) else raw.get("kind")
    text = getattr(raw, "text", None) if not isinstance(raw, Mapping) else raw.get("text")
    if kind not in SELECTOR_KINDS:
        return None
    if kind == "text_match":
        text = squash(text, MAX_SELECTOR_TEXT)
        return {"kind": kind, "text": text} if text else None
    return {"kind": kind, "text": None}
