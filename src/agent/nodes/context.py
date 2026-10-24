"""`context_assembly` (plan 07, no model call): load, once per turn, what the later steps read.

The persona text (the Composer's only), today's date (UTC), the dataset's latest date and the reference date that "today",
"this year" and "last month" mean for the data (A5), the relative dates resolved in code, and the glossary and similar solved
questions (plan 10's hooks; empty lists until then). The locked definitions live in code (`locked_rules.py`), so the answer
never depends on plan 10.

A missing optional part never fails the turn: the persona, the glossary and the golden examples fall back to nothing. Access
rules are different, and they block (`policy_context`).

A simple question skips the Planner (Q36): its plan is one sub-question equal to the standalone question, made here.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from src.agent.boundary import node
from src.agent.deps import Deps
from src.agent.nodes.planner import MAX_TEXT
from src.agent.prompts.locked_rules import date_hints
from src.agent.state import TurnState
from src.errors import crash_summary
from src.observability import turn_metrics
from src.textutil import squash

log = logging.getLogger(__name__)


def _optional(read: Callable[[], Any], default: Any, what: str) -> Any:
    try:
        return read()
    except Exception as exc:  # noqa: BLE001 - an optional part must never fail the turn
        log.warning("%s is not available, continuing without it: %s", what, crash_summary(exc))
        return default


def make_context_assembly(deps: Deps):
    @node(deps, "context_assembly")
    def context_assembly(state: TurnState) -> dict:
        question = state.get("standalone_question") or state.get("question", "")
        today = deps.today()
        reference = deps.catalog.reference_date(today)
        context = {
            "persona_text": _optional(deps.persona_text, "", "the persona"),
            "today": today.isoformat(),
            "dataset_latest": deps.catalog.latest_date.isoformat(),
            "reference_date": reference.isoformat(),
            "date_hints": date_hints(reference),
            "freshness_note": deps.catalog.freshness_note(today),
            "glossary_terms": _optional(lambda: list(deps.glossary(question)), [], "the glossary"),
            "trio_examples": _optional(lambda: list(deps.golden(question)), [], "the golden examples"),
        }
        update: dict = {"context": context}
        if state.get("turn_kind") == "simple":
            tables = sorted(deps.allowlist.table_names())
            update["plan"] = {
                "subquestions": [{"id": "q1", "text": squash(question, MAX_TEXT), "depends_on": [], "tables": tables}],
                "assumptions": [], "date_range": None, "notices": [],
            }
            turn_metrics().set_subquestions(1)
        return update

    return context_assembly
