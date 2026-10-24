"""`context_assembly` (plan 07, no model call): load, once per turn, what the later steps read.

The persona text (the Composer's only), today's date (UTC), the dataset's latest date and the reference date that "today",
"this year" and "last month" mean for the data (A5), the relative dates resolved in code, and the glossary and similar solved
questions (plan 10; empty lists when the Golden Bucket is off). The locked definitions live in code (`locked_rules.py`), so the
answer never depends on plan 10.

The solved questions are chosen for this user: a trio of a brand the user does not have is never a candidate (S5). The retrieved
records are kept apart by what each step may see: `trio_examples` holds the question, the notes and the SQL (the Planner reads the
first two, the SQL writer all three), and `trio_report` holds the report excerpt, which only the Composer reads. The `golden.retrieve`
span has the trio IDs and scores, never their text.

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
from src.agent.nodes.policy import current_user
from src.agent.prompts.locked_rules import date_hints
from src.agent.state import TurnState
from src.errors import crash_summary
from src.observability import span, turn_metrics
from src.textutil import squash

log = logging.getLogger(__name__)

TRIO_KEYS = ("id", "question", "notes", "sql")  # what goes into `trio_examples`; the report excerpt is kept apart


def _optional(read: Callable[[], Any], default: Any, what: str) -> Any:
    try:
        return read()
    except Exception as exc:  # noqa: BLE001 - an optional part must never fail the turn
        log.warning("%s is not available, continuing without it: %s", what, crash_summary(exc))
        return default


def _retrieve(deps: Deps, state: TurnState, question: str) -> list[dict[str, Any]]:
    """Similar solved questions for this user, or [] when the Golden Bucket is off. One span with the IDs and scores."""
    if deps.golden is None or not deps.settings.golden.enabled:
        return []
    with span("golden.retrieve", "step") as sp:
        records = _optional(lambda: list(deps.golden(question, current_user(deps, state))), [], "the golden examples")
        ids, scores = [str(record.get("id")) for record in records], [record.get("score") for record in records]
        # `summary` is the line `/trace` shows (it leaves lists out); it holds the same IDs and scores and no text
        sp.set(trio_ids=ids, scores=scores, summary=", ".join(f"{i} ({s})" for i, s in zip(ids, scores)) or "no similar solved question")
    return records


def make_context_assembly(deps: Deps):
    @node(deps, "context_assembly")
    def context_assembly(state: TurnState) -> dict:
        question = state.get("standalone_question") or state.get("question", "")
        today = deps.today()
        reference = deps.catalog.reference_date(today)
        records = _retrieve(deps, state, question)
        glossary = deps.glossary if deps.settings.golden.enabled else None
        context = {
            "persona_text": _optional(deps.persona_text, "", "the persona"),
            "today": today.isoformat(),
            "dataset_latest": deps.catalog.latest_date.isoformat(),
            "reference_date": reference.isoformat(),
            "date_hints": date_hints(reference),
            "freshness_note": deps.catalog.freshness_note(today),
            "glossary_terms": _optional(lambda: list(glossary(question)), [], "the glossary") if glossary else [],
            "trio_examples": [{key: str(record[key]) for key in TRIO_KEYS if key in record} for record in records],
            "trio_report": str(records[0].get("report", "")) if records else "",  # the best match only: it is for style
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
