"""`composer`, `numeric_check` and `output_guard` (plan 07, F4, F6, S9, S11): from guarded results to the answer.

The Composer (Flash, temperature about 0.3) gets what it needs and nothing raw: guarded rows, statuses, notices, the plan, a
scope label, the locked rules and, after them, the persona. It returns a title and a body.

The numeric check then holds every number in the body against the guarded results (`numeric_check.py`). A sentence or table row
with a number the data does not back is removed, and the answer says figures were left out. `composer.numeric_retry` (0 by
default) allows one re-ask with the rejected numbers listed; it spends budget.

The code then renders the envelope around the body (`envelope.py`), and the Output Guard (plan 06) checks the whole text. Only
progress is shown while this runs; the answer is printed once, after the guard (S9).

Until the Output Guard has passed it, the text lives in `TurnRuntime.scratch`, never in graph state: state is checkpointed, and
an answer the guard held back must not be stored anywhere, not even masked (plan 06).

If the Composer cannot run (the model is down, the budget or the time is used up) or the numeric check leaves nothing, the code
builds the answer from the guarded figures (`envelope.fallback_body`): degraded, said so, still true and still guarded.
"""
from __future__ import annotations

from pydantic import BaseModel

from src.agent.boundary import node
from src.agent.deps import DRAFT, FINAL_TEXT, NOTES, TITLE, Deps, TurnRuntime
from src.agent.envelope import fallback_body, has_answerable_result, is_partial, render_answer
from src.agent.numeric_check import check_numbers, ground_values
from src.agent.prompts.locked_rules import LOCKED_DEFINITIONS, relevant_definitions
from src.agent.prompts.templates import composer_prompt
from src.agent.state import TurnState, error_record
from src.errors import MODEL_FALLBACK_KINDS, AgentError, ErrorKind
from src.llm.routing import Step
from src.observability import event, guard_decision

NO_DATA_BODY = "I did not find any data for this question."
MAX_TITLE = 80


class ComposerOut(BaseModel):
    title: str = ""
    body_markdown: str


def compose(deps: Deps, runtime: TurnRuntime, state: TurnState, rejected: tuple[str, ...] = ()) -> ComposerOut:
    """One Composer call. `rejected` lists numbers a first draft used that the data does not hold (the optional retry)."""
    plan = state.get("plan", {})
    context = state.get("context", {})
    standalone = state.get("standalone_question") or state.get("question", "")
    results = state.get("results", {})
    notices = [notice for result in results.values() for notice in (result.get("guarded") or {}).get("notices", [])]
    parts = composer_prompt(
        question=standalone, raw_question=state.get("question", ""), intent=state.get("intent") or "data_question",
        subquestions=plan.get("subquestions", []), results=results, assumptions=plan.get("assumptions", []),
        date_range=plan.get("date_range"), scope_label=state.get("scope_label", ""), notices=list(dict.fromkeys(notices)),
        persona_text=context.get("persona_text", ""),
        definitions=relevant_definitions(standalone, context.get("glossary_terms")), rejected_numbers=rejected,
    )
    reply = deps.gateway.generate(
        Step.COMPOSER, system=parts.system, contents=parts.contents, schema=ComposerOut, budget=runtime.budget,
        prompt=parts.attrs(),
    )
    return reply.parsed


def make_composer(deps: Deps):
    @node(deps, "composer", progress="Writing the answer...")
    def composer(state: TurnState) -> dict:
        runtime = deps.runtime(state)
        errors = list(state.get("errors", []))
        try:
            out = compose(deps, runtime, state)
            runtime.hold(DRAFT, {"title": out.title.strip()[:MAX_TITLE], "body": out.body_markdown, "degraded": False})
        except AgentError as exc:
            if exc.kind not in MODEL_FALLBACK_KINDS or not has_answerable_result(state):
                raise
            event("composer.fallback", error_kind=exc.kind.value)  # the model is down or the budget is gone: build it in code
            runtime.hold(DRAFT, {"title": "", "body": fallback_body(state) or NO_DATA_BODY, "degraded": True})
            errors.append(error_record(exc))
        return {"errors": errors}

    return composer


def _ground(state: TurnState):
    plan = state.get("plan", {})
    context = state.get("context", {})
    date_range = plan.get("date_range") or {}
    # The assumptions and the readings are shown to the user in "How I read your question", so the Composer restating one
    # ("in the last 30 days") is consistent with what the answer itself says. They are stated choices, not results.
    reading = [*plan.get("assumptions", []), *(r.get("interpretation", "") for r in state.get("results", {}).values())]
    texts = {
        "reading": " ".join(reading),
        "question": f"{state.get('question', '')} {state.get('standalone_question') or ''}",
        "plan": f"{date_range.get('start', '')} {date_range.get('end', '')}",
        # The dates the code resolved ("last month" is 2026-09-01 to 2026-09-30) are what the query used, so the answer may restate them.
        "context": f"{context.get('today', '')} {context.get('dataset_latest', '')} {context.get('reference_date', '')} "
                   + " ".join(context.get("date_hints", {}).values()),
        "definitions": " ".join(definition for _, definition in LOCKED_DEFINITIONS),
    }
    return ground_values(state.get("results", {}), texts=texts)


def _reading_ground(state: TurnState, ground):
    """What may back a figure in the text the code shows next to the answer but the model wrote (how the question was read, what
    evidence is missing): the data, the question, and the numbers of the SQL that ran, so "orders above $50" is backed by the
    filter that was used. The reading itself is not a source for its own figures."""
    sql = " ".join(result.get("final_sql", "") for result in state.get("results", {}).values())
    return [*ground, *ground_values({}, texts={"sql": sql})]


def make_numeric_check(deps: Deps):
    retry = deps.settings.composer.numeric_retry

    @node(deps, "numeric_check")
    def numeric_check(state: TurnState) -> dict:
        runtime = deps.runtime(state)
        draft = runtime.scratch.pop(DRAFT, None)
        if draft is None:
            raise AgentError(ErrorKind.INTERNAL, detail="there is no draft to check")
        ground = _ground(state)
        body, title, degraded, removed = draft["body"], draft["title"], draft["degraded"], 0

        if not degraded:
            counted = [len(state.get("plan", {}).get("subquestions", []))]
            check = check_numbers(body, ground, counted=counted)
            if check.removed and retry > 0 and runtime.budget.calls_remaining > 0:
                try:  # one re-ask with the numbers the data does not hold
                    again = compose(deps, runtime, state, rejected=check.ungrounded)
                    second = check_numbers(again.body_markdown, ground, counted=counted)
                    if second.removed <= check.removed:
                        check, title = second, again.title.strip()[:MAX_TITLE]
                except AgentError as exc:
                    if exc.kind not in MODEL_FALLBACK_KINDS:
                        raise  # the first draft's check stands
            event("numeric_check", matched=list(check.matched), removed=check.removed)
            if check.removed:
                guard_decision("numeric", "block", rule=ErrorKind.NUMERIC_UNGROUNDED.value, removed=check.removed)
            else:
                guard_decision("numeric", "pass")
            body, removed = check.body, check.removed
            if check.empty:  # nothing useful is left: the figures come straight from the data instead
                body, degraded = fallback_body(state) or NO_DATA_BODY, True

        if title and not check_numbers(title, ground, counted=[len(state.get("plan", {}).get("subquestions", []))]).clean:
            title = ""  # a title is model text too: one with a figure the data does not back is dropped, and a fallback is used
        text, notes = render_answer(
            state, body, title=title, numeric_removed=removed > 0, degraded=degraded, ground=_reading_ground(state, ground),
        )
        runtime.hold(FINAL_TEXT, text)
        runtime.hold(TITLE, title)
        runtime.hold(NOTES, notes)
        return {"partial": degraded or removed > 0 or is_partial(state)}

    return numeric_check


def make_output_guard(deps: Deps):
    @node(deps, "output_guard")
    def output_guard(state: TurnState) -> dict:
        runtime = deps.runtime(state)
        text = runtime.scratch.pop(FINAL_TEXT, None)
        title = runtime.scratch.pop(TITLE, None)
        notes = runtime.scratch.pop(NOTES, [])
        if text is None:
            raise AgentError(ErrorKind.INTERNAL, detail="there is no answer to check")
        checked = deps.output_guard.check(text)  # raises OUTPUT_BLOCKED: the text is gone, and is stored nowhere
        if title and deps.output_guard.scan(title):
            title = None  # a title that is not part of the answer is checked too, and dropped (a fallback is used) rather than stored
        return {"answer": checked, "title": title or None, "notices": notes}

    return output_guard
