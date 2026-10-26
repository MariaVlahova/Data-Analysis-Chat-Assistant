"""`evidence_check` (plan 07, diagram step 4b): do the results answer the question?

The code check always runs. Every sub-question has a status, and a turn where none of them has data to show (every status is
blocked, failed, too expensive, too slow or skipped) goes to `compose_failure`. An honest "no data" is data to show.

On "why" and report turns that have at least one `ok` result, a Flash-Lite check also reads the question, the plan and the
guarded summaries (counts and totals, never rows) and says whether the evidence is enough, and if not what is missing. If that
call fails, or the turn has no calls left to spare, the code check stands alone.

Not enough, and a replan is left (`loop.max_replans`, ceiling 2, 0 in the prototype) and the plan has room: back to the Planner
with the missing list, and the new sub-questions run in the same loop. Otherwise the Composer writes a partial answer and the
answer says what is missing (Q35).
"""
from __future__ import annotations

from pydantic import BaseModel

from src.agent.boundary import node
from src.agent.deps import COMPOSER_RESERVE, Deps
from src.agent.nodes.planner import plan_lines
from src.agent.prompts.templates import evidence_prompt, format_summary, rows_placeholder
from src.agent.state import ANSWERABLE, OK, TurnState
from src.errors import MODEL_FALLBACK_KINDS, AgentError
from src.llm.routing import Step
from src.observability import event, turn_metrics
from src.textutil import squash

MAX_MISSING = 5
REPLAN_MIN_CALLS = 2 + COMPOSER_RESERVE  # the Planner, at least one query, and the call kept for the answer
MAX_MISSING_CHARS = 120


class EvidenceOut(BaseModel):
    enough: bool
    missing: list[str] = []


def make_evidence_check(deps: Deps):
    loop = deps.settings.loop

    @node(deps, "evidence_check", progress="Checking the results...")
    def evidence_check(state: TurnState) -> dict:
        runtime = deps.runtime(state)
        results = state.get("results", {})
        statuses = [result.get("status") for result in results.values()]
        if not any(status in ANSWERABLE for status in statuses):
            return {"evidence": {"enough": False, "missing": [], "nothing_to_show": True}}

        evidence: dict = {"enough": True, "missing": []}
        asked = state.get("turn_kind") in ("why", "report") and OK in statuses
        if asked and runtime.has_room():
            plan = state.get("plan", {})
            parts = evidence_prompt(
                question=state.get("standalone_question") or state.get("question", ""),
                plan_lines=plan_lines(plan, results),
                status_lines=[f"{sid}: {result.get('status')}" for sid, result in results.items()],
                summaries=[f"{sid}: {format_summary(result['guarded'])}" for sid, result in results.items() if result.get("guarded")],
                summaries_debug=[f"{sid}: {rows_placeholder(sid, result['guarded'])}" for sid, result in results.items() if result.get("guarded")],
            )
            try:
                reply = deps.gateway.generate(
                    Step.EVIDENCE_CHECK, system=parts.system, contents=parts.contents, schema=EvidenceOut,
                    budget=runtime.budget, prompt=parts.attrs(),
                )
                missing = [squash(item, MAX_MISSING_CHARS) for item in reply.parsed.missing if item.strip()][:MAX_MISSING]
                evidence = {"enough": bool(reply.parsed.enough) or not missing, "missing": [] if reply.parsed.enough else missing}
            except AgentError as exc:
                if exc.kind not in MODEL_FALLBACK_KINDS:
                    raise
                event("evidence.fallback", error_kind=exc.kind.value)  # the code check stands alone

        plan = state.get("plan", {})
        replan_left = state.get("replans_used", 0) < loop.max_replans and len(plan.get("subquestions", [])) < loop.max_subquestions
        if not evidence["enough"] and replan_left and runtime.budget.calls_remaining >= REPLAN_MIN_CALLS:  # never the Composer's call
            evidence["replan"] = True
            turn_metrics().add_replan()
            return {"evidence": evidence, "replans_used": state.get("replans_used", 0) + 1}
        return {"evidence": evidence}

    return evidence_check
