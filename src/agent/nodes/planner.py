"""`planner` (plan 07, decision 3.10, F3): split a "why" or report question into at most 4 sub-questions. It writes no SQL.

Skipped for simple questions (Q36): `context_assembly` makes their one-sub-question plan. Also the replan step: when the
Evidence Check finds evidence missing and a replan is left (`loop.max_replans`, 0 in the prototype), the Planner runs again
with the missing list and the guarded summaries (never rows), and its new sub-questions are added to the plan.

The model proposes; code decides what stays (`normalize_plan`): the number of sub-questions is cut to the limit (and the cut
is said in the answer's notes), ids are made unique, a dependency may only point at an earlier sub-question (so there is no
cycle), a table must be in the allow-list, and a date range must be two real dates in order.

If the model cannot plan, the question is answered as one sub-question and the answer says so. A plan is a help, not a gate.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any, Mapping

from pydantic import BaseModel

from src.agent.boundary import node
from src.agent.deps import Deps
from src.agent.prompts.templates import format_summary, planner_prompt
from src.agent.state import Plan, SubQuestion, TurnState
from src.errors import LLM_FAILURE_KINDS, AgentError
from src.guards.allowlist import Allowlist
from src.llm.routing import Step
from src.observability import event, turn_metrics
from src.textutil import squash

log = logging.getLogger(__name__)

MAX_TEXT = 300
MAX_ASSUMPTION = 200
MAX_ASSUMPTIONS = 6
FALLBACK_NOTICE = "I could not split this question into parts, so I answered it as one question."


class SubQuestionOut(BaseModel):
    id: str = ""
    text: str
    depends_on: list[str] = []
    tables: list[str] = []


class DateRangeOut(BaseModel):
    start: str
    end: str
    label: str = ""


class PlanOut(BaseModel):
    subquestions: list[SubQuestionOut]
    assumptions: list[str] = []
    date_range: DateRangeOut | None = None


def _valid_range(raw: DateRangeOut | None) -> dict[str, str] | None:
    if raw is None:
        return None
    try:
        start, end = date.fromisoformat(raw.start.strip()), date.fromisoformat(raw.end.strip())
    except ValueError:
        return None
    if start > end:
        return None
    return {"start": start.isoformat(), "end": end.isoformat(), "label": squash(raw.label, 60)}


def _tables(raw: list[str], allowlist: Allowlist) -> list[str]:
    known = [allowlist.table_name(name) for name in raw]
    chosen = list(dict.fromkeys(name for name in known if name))
    return chosen or sorted(allowlist.table_names())  # no usable table named: the writer gets the whole (small) schema


def normalize_plan(
    out: PlanOut, *, allowlist: Allowlist, limit: int, existing: Plan | None = None, standalone: str = "",
) -> Plan:
    """The plan the code will run. `existing` is the plan so far, for a replan: new sub-questions are added after it and the
    total stays within `limit`."""
    base = list(existing.get("subquestions", [])) if existing else []
    room = max(limit - len(base), 0)
    proposed = [sub for sub in out.subquestions if squash(sub.text, MAX_TEXT)]
    notices = list(existing.get("notices", [])) if existing else []
    if len(proposed) > room:
        notices.append(f"I planned {len(proposed) + len(base)} sub-questions but run at most {limit} for one question, so I ran the first {limit}.")
        proposed = proposed[:room]

    renamed: dict[str, str] = {sub["id"]: sub["id"] for sub in base}
    added: list[SubQuestion] = []
    for offset, sub in enumerate(proposed, start=len(base) + 1):
        new_id = f"q{offset}"
        # A dependency may only point at a sub-question before this one, which rules out a cycle.
        depends = [renamed[dep] for dep in sub.depends_on if dep in renamed and renamed[dep] != new_id]
        if sub.id:
            renamed.setdefault(sub.id, new_id)
        added.append({"id": new_id, "text": squash(sub.text, MAX_TEXT), "depends_on": list(dict.fromkeys(depends)),
                      "tables": _tables(sub.tables, allowlist)})
    subquestions = base + added
    if not subquestions and standalone:
        subquestions = [{"id": "q1", "text": squash(standalone, MAX_TEXT), "depends_on": [], "tables": sorted(allowlist.table_names())}]

    assumptions = list(existing.get("assumptions", [])) if existing else []
    assumptions += [squash(a, MAX_ASSUMPTION) for a in out.assumptions if squash(a, MAX_ASSUMPTION)]
    date_range = _valid_range(out.date_range) or (existing.get("date_range") if existing else None)
    return {
        "subquestions": subquestions, "assumptions": list(dict.fromkeys(assumptions))[:MAX_ASSUMPTIONS],
        "date_range": date_range, "notices": list(dict.fromkeys(notices)),
    }


def plan_lines(plan: Plan, results: Mapping[str, Mapping[str, Any]]) -> list[str]:
    return [f"{sub['id']}: {sub['text']} ({results.get(sub['id'], {}).get('status', 'not run')})" for sub in plan.get("subquestions", [])]


def make_planner(deps: Deps):
    limit = deps.settings.loop.max_subquestions

    @node(deps, "planner", progress="Planning...")
    def planner(state: TurnState) -> dict:
        runtime = deps.runtime(state)
        context = state.get("context", {})
        existing: Plan = state.get("plan") or {}
        replanning = bool(existing.get("subquestions")) and state.get("replans_used", 0) > 0
        standalone = state.get("standalone_question") or state.get("question", "")

        replan = None
        if replanning:
            results = state.get("results", {})
            replan = {
                "done": plan_lines(existing, results),
                "missing": state.get("evidence", {}).get("missing", []),
                "summaries": [f"{sid}: {format_summary(r['guarded'])}" for sid, r in results.items() if r.get("guarded")],
            }
        parts = planner_prompt(
            question=standalone, intent=state.get("intent") or "why_question", schema_overview=deps.catalog.overview(),
            allowed_tables=sorted(deps.allowlist.table_names()), today=context.get("today", ""),
            dataset_latest=context.get("dataset_latest", ""), hints=context.get("date_hints", {}), max_subquestions=limit,
            definitions=context.get("glossary_terms", []),
            golden_notes=[e.get("notes", "") for e in context.get("trio_examples", []) if e.get("notes")], replan=replan,
        )
        try:
            result = deps.gateway.generate(
                Step.PLANNER, system=parts.system, contents=parts.contents, schema=PlanOut, budget=runtime.budget,
                prompt=parts.attrs(),
            )
            plan = normalize_plan(result.parsed, allowlist=deps.allowlist, limit=limit, existing=existing if replanning else None, standalone=standalone)
        except AgentError as exc:
            if exc.kind not in LLM_FAILURE_KINDS:  # the model could not plan this time: the turn goes on with one sub-question
                raise  # the budget or the time is gone: nothing else can run either
            event("planner.fallback", error_kind=exc.kind.value)
            if replanning:
                return {}  # the plan so far stands; the answer will say what is missing
            plan = normalize_plan(PlanOut(subquestions=[]), allowlist=deps.allowlist, limit=limit, standalone=standalone)
            plan["notices"] = [FALLBACK_NOTICE]
        turn_metrics().set_subquestions(len(plan["subquestions"]))
        return {"plan": plan}

    return planner

