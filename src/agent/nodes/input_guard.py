"""`input_guard` (plan 07, decision 3.2, S10, F7, F17): the first step. It only routes.

Code goes first and spends no model call: trim, remove control characters, refuse an empty message or one longer than
`guards.max_input_chars`. Then one Flash-Lite call with a fixed output schema says whether the message is in scope, whether it
looks like an injection, what the intent is, how to rewrite it so it needs no history, and (for a delete) a selector.

If the call fails or its reply is unusable, the request is blocked (`INPUT_GUARD_FAILED`, Q13 default). It never falls through
to the next step. If the model is fooled, nothing is lost: the SQL Guard, Result Guard, Output Guard and HITL Gate are code
and still hold (NFR16). The selector the model produces is checked in code, and the model never sees or writes an ID.
"""
from __future__ import annotations

import re
from typing import Literal, Sequence

from pydantic import BaseModel, create_model

from src import pii
from src.agent.boundary import node
from src.agent.deps import Deps
from src.agent.prompts.locked_rules import BASE_INTENTS
from src.agent.prompts.templates import input_guard_prompt
from src.agent.state import HISTORY_EXCHANGES, TurnState
from src.errors import AgentError, ErrorKind
from src.guards.pseudonymizer import LABEL_IN_TEXT
from src.llm.routing import Step
from src.observability import guard_decision, turn_metrics
from src.textutil import squash
from src.tools.base import clean_selector

# Control characters, and the characters that hide text, as (first, last) code points so the list can be read.
_HIDDEN_RANGES = (
    (0x00, 0x08), (0x0E, 0x1B), (0x7F, 0x9F),  # C0 and C1 controls; tab, newline and carriage return stay; separators are below
    (0x00AD, 0x00AD),  # soft hyphen
    (0x061C, 0x061C),  # Arabic letter mark
    (0x115F, 0x1160), (0x3164, 0x3164), (0xFFA0, 0xFFA0),  # Hangul fillers
    (0x17B4, 0x17B5),  # Khmer inherent vowels
    (0x180B, 0x180F),  # Mongolian variation selectors and vowel separator
    (0x200B, 0x200F),  # zero-width characters and left-to-right / right-to-left marks
    (0x202A, 0x202E),  # bidirectional embeddings and overrides
    (0x2060, 0x206F),  # word joiner, invisible operators, bidirectional isolates, deprecated format characters
    (0xFE00, 0xFE0F), (0xE0100, 0xE01EF),  # variation selectors
    (0xFEFF, 0xFEFF),  # byte order mark
    (0xFFF9, 0xFFFB),  # interlinear annotation marks
    (0x1D173, 0x1D17A),  # musical formatting characters
    (0xE0000, 0xE007F),  # "tag" characters: a whole message in ASCII that no one sees
)
_STRIP = re.compile("[" + "".join(f"{re.escape(chr(first))}-{re.escape(chr(last))}" for first, last in _HIDDEN_RANGES) + "]")
# Separators are not hidden text: removing them would glue the words on both sides together ("revenue", a tab-like mark, "by").
# Unicode line and paragraph separators (and NEL) become a line break; vertical tab, form feed and the C0 file, group, record and unit
# separators become a space. Python's `split` and a terminal treat them the same way.
_SEPARATORS = {0x0B: " ", 0x0C: " ", 0x1C: " ", 0x1D: " ", 0x1E: " ", 0x1F: " ", 0x2028: "\n", 0x2029: "\n", 0x85: "\n"}
_DATA_INTENTS = {"data_question": "simple", "schema_question": "simple", "why_question": "why", "report_request": "report"}
SCHEMA_INTENT = "schema_question"
OUT_OF_SCOPE_INTENT = "out_of_scope"

REFUSAL_OUT_OF_SCOPE = "out_of_scope"
REFUSAL_INJECTION = "injection"
REFUSAL_SESSION_LABEL = "session_label"


class SelectorOut(BaseModel):
    kind: Literal["conversation", "text_match", "all", "last"]
    text: str | None = None


def make_input_schema(tool_intents: Sequence[str]) -> type[BaseModel]:
    """The Input Guard's output schema. The intents are the base ones plus those of the registered tools, so a new tool needs
    no change here (NFR24)."""
    names = tuple(dict.fromkeys([name for name, _ in BASE_INTENTS] + list(tool_intents)))
    return create_model(  # type: ignore[call-overload]
        "InputGuardOut",
        in_scope=(bool, ...),
        injection_suspected=(bool, ...),
        intent=(Literal[names], ...),
        standalone_question=(str, ""),
        selector=(SelectorOut | None, None),
    )


def clean_input(text: str) -> str:
    """The message with control and hidden characters removed and the ends trimmed. Newlines and tabs stay."""
    return _STRIP.sub("", text.translate(_SEPARATORS).replace("\r\n", "\n").replace("\r", "\n")).strip()


def typed_text(text: str) -> str:
    """What the user typed, as it is stored and shown to a model: hidden characters removed, then masked (`pii.mask`).

    The order matters. A zero-width character or a soft hyphen inside an address breaks the pattern, so masking first would miss it
    and cleaning afterwards would put the plain address back."""
    return pii.mask(clean_input(text))


def make_input_guard(deps: Deps):
    schema = make_input_schema(deps.registry.intents())
    limit = deps.settings.guards.max_input_chars

    @node(deps, "input_guard", progress="Understanding your question...")
    def input_guard(state: TurnState) -> dict:
        runtime = deps.runtime(state)
        question = typed_text(state.get("question", ""))  # already so when it comes from `AgentRuntime.run`; masking twice changes nothing
        if not question or len(question) > limit:
            guard_decision("input", "block", rule="INPUT_REJECTED", chars=len(question))
            raise AgentError(ErrorKind.INPUT_REJECTED, detail=f"the message is {'empty' if not question else 'longer than ' + str(limit) + ' characters'}")

        history = state.get("history", [])[-HISTORY_EXCHANGES:]
        parts = input_guard_prompt(question, history, deps.registry.describe_intents(), deps.registry.selector_intents())
        try:
            result = deps.gateway.generate(
                Step.INPUT_GUARD, system=parts.system, contents=parts.contents, schema=schema, budget=runtime.budget,
                prompt=parts.attrs(),
            )
        except AgentError as exc:  # any gateway failure or unusable reply blocks the request (S10, Q13)
            guard_decision("input", "block", rule="INPUT_GUARD_FAILED", cause=exc.kind.value)
            # A setup problem (no key, a rejected key) is not worth "please try again": the gateway's own words pass through
            # with its `retryable=False`, so the user is told to call an administrator.
            setup = exc.kind is ErrorKind.LLM_UNAVAILABLE and not exc.retryable
            raise AgentError(
                ErrorKind.INPUT_GUARD_FAILED, exc.user_message if setup else None,
                detail=f"the classifier failed: {exc.kind.value}", retryable=False if setup else None,
            ) from None
        out = result.parsed
        if out is None:
            guard_decision("input", "block", rule="INPUT_GUARD_FAILED", cause="no parsed reply")
            raise AgentError(ErrorKind.INPUT_GUARD_FAILED, detail="the classifier gave no usable reply")

        intent = out.intent
        tool_intent = deps.registry.for_intent(intent) is not None
        turn_kind = "tool" if tool_intent else _DATA_INTENTS.get(intent, "simple")
        update: dict = {"question": question, "intent": intent, "turn_kind": turn_kind, "selector": None, "refusal": None}
        standalone = squash(typed_text(out.standalone_question), limit)  # stored in the checkpoint: cleaned and masked first, then cut

        if out.injection_suspected:
            guard_decision("input", "block", rule="INJECTION_SUSPECTED")
            update["refusal"] = REFUSAL_INJECTION
        elif not out.in_scope or intent == OUT_OF_SCOPE_INTENT:
            guard_decision("input", "block", rule="OUT_OF_SCOPE")
            update["refusal"] = REFUSAL_OUT_OF_SCOPE
        elif not tool_intent and (LABEL_IN_TEXT.search(question) or LABEL_IN_TEXT.search(standalone)):
            # "Customer #48213" is a name for this session, not an ID, and nothing can turn it back into one (plan 06). A question
            # about one customer by label (or a follow-up the rewrite turned into one) is refused here, not left to the SQL writer.
            guard_decision("input", "block", rule="SESSION_LABEL")
            update["refusal"] = REFUSAL_SESSION_LABEL
        else:
            guard_decision("input", "pass")

        turn_metrics().set_intent(intent if not update["refusal"] else OUT_OF_SCOPE_INTENT, turn_kind)
        if update["refusal"]:
            return update
        if turn_kind in ("why", "report"):
            runtime.budget.upgrade(turn_kind)  # S3: more calls and time for the kinds of turn that need them
        if tool_intent:
            update["selector"] = clean_selector(out.selector)  # checked in code; the model never writes an ID
        update["standalone_question"] = standalone or question
        return update

    return input_guard

