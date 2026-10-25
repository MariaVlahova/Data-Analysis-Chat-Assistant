"""Layers 2/3/adaptive: rubric scoring by a judge model.

One code path serves all three judge-graded registries -- standard rubrics,
project-specific rubrics, and adaptive (criteria-generated) rubrics -- because
they differ only in the rubric text (``evalkit.rubrics``) and in whether
criteria are generated per sample first. Applicability is decided in code
before the judge is called, so a rubric that cannot apply costs nothing and is
reported as N/A rather than guessed at by a model asked to grade something
that never happened.

This is the second half of the plugin interface. Adding a new judge-graded
metric never means writing scoring code -- it means adding a row to your
rubric CSV (``evalkit.rubrics``) and, only if the rubric does not always
apply, one small ``Applicability`` function registered in the dict you pass to
``run_graded_metrics``.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from evalkit.context import ValidationContext
from evalkit.judge import AdaptiveCriteria, JudgeClient, RubricVerdict, normalized_scores
from evalkit.rubrics import Rubric
from evalkit.schema import Layer, MetricResult

MAX_EVIDENCE_CHARS = 1200
MAX_TRAJECTORY_CALLS = 24

#: Decides, from the session alone, whether a rubric could possibly apply --
#: before any model runs. The third element of the tuple is the precondition
#: *your sample's own spec* declared (e.g. "conflict declared with doc X"):
#: when non-empty, the rubric was promised and did not fire, which is a
#: NOT_EXERCISED verdict rather than a plain "does not apply". Leave it empty
#: for an ordinary N/A.
Applicability = Callable[[ValidationContext], tuple[bool, str, str]]


def always_applicable(_ctx: ValidationContext) -> tuple[bool, str, str]:
    return True, "", ""


# --------------------------------------------------------------------------- #
# Evidence assembly
# --------------------------------------------------------------------------- #
def _clip(text: str, limit: int = MAX_EVIDENCE_CHARS) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else f"{text[:limit]}\n... [truncated]"


def _trajectory(ctx: ValidationContext) -> str:
    calls = ctx.tool_calls[:MAX_TRAJECTORY_CALLS]
    if not calls:
        return "(no tools were called)"
    lines = []
    for i, call in enumerate(calls, start=1):
        result = ctx.tool_result_for(str(call.get("id", "")))
        body = _clip(result.content, 320) if result else "(no result recorded)"
        lines.append(
            f"{i}. {call.get('agent', '?')} -> {call.get('tool')}({json.dumps(call.get('args', {}))})\n"
            f"   => {body}"
        )
    overflow = len(ctx.tool_calls) - len(calls)
    if overflow > 0:
        lines.append(f"... and {overflow} further call(s)")
    return "\n".join(lines)


def _evidence(ctx: ValidationContext) -> str:
    bodies = [_clip(event.content, 600) for event in ctx.tool_results]
    return "\n\n".join(bodies[-6:]) if bodies else "(no tool results)"


def _history(ctx: ValidationContext) -> str:
    """Earlier turns of the conversation, so a judge can see what was carried."""
    prior = ctx.prior_turns
    if not prior:
        return ""
    lines = [
        f"[turn {turn.index + 1}] user: {_clip(turn.prompt, 200)}\n"
        f"[turn {turn.index + 1}] assistant: {_clip(turn.answer, 300)}"
        for turn in prior
    ]
    return "\n".join(lines)


def default_sample_block(ctx: ValidationContext) -> str:
    """A generic sample rendering: prompt, evidence, tool trajectory, answer.

    Works for a broad class of agent/RAG systems out of the box. Systems with
    a materially different shape (e.g. a batch pipeline with no single
    "prompt") should pass their own ``sample_formatter`` to
    ``run_graded_metrics`` / ``score_rubric`` instead -- the important
    property to preserve is that the *same* text goes to the judge and to a
    human rater in an agreement study (see ``evalkit.rubrics``), so keep one
    formatter and reuse it everywhere a sample needs rendering.
    """
    prompt = next(
        (event.content for event in ctx.events if event.type == "user" and not event.is_feedback), ""
    )
    history = _history(ctx)
    return "\n\n".join(
        part
        for part in [
            f"EARLIER TURNS IN THIS CONVERSATION:\n{history}" if history else "",
            f"REQUEST: {_clip(prompt, 600)}",
            f"EVIDENCE RETRIEVED:\n{_evidence(ctx)}",
            f"TOOL TRAJECTORY:\n{_trajectory(ctx)}",
            f"RESPONSE:\n{_clip(str(ctx.state.get('final_answer') or ''))}",
        ]
        if part
    )


SCORING_INSTRUCTION = (
    "You are evaluating one sample from an AI agent pipeline. Score it against "
    "the rubric below. Judge only what the rubric asks about; ignore style "
    "unless the rubric mentions it. If the rubric could not apply to this "
    "sample, return a null score rather than a middling one."
)

CRITERIA_INSTRUCTION = (
    "You are preparing to evaluate one sample from an AI agent pipeline. "
    "Before scoring, list the specific, checkable expectations this "
    "particular sample should meet under the rubric below. Base them on what "
    "was actually asked and retrieved, not on generic quality. If the rubric "
    "does not apply here, set applicable to false and return no criteria."
)


def build_scoring_prompt(rubric: Rubric, sample: str, criteria: list[str] | None = None) -> str:
    parts = [SCORING_INSTRUCTION, rubric.as_prompt_block()]
    if criteria:
        parts.append("CRITERIA FOR THIS SAMPLE:\n" + "\n".join(f"- {c}" for c in criteria))
    parts.append(f"SAMPLE:\n{sample}")
    return "\n\n".join(parts)


def build_criteria_prompt(rubric: Rubric, sample: str) -> str:
    return "\n\n".join([CRITERIA_INSTRUCTION, rubric.as_prompt_block(), f"SAMPLE:\n{sample}"])


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
def layer_of(rubric: Rubric) -> Layer:
    if rubric.registry == "STANDARD_RUBRICS":
        return Layer.STANDARD
    return Layer.ADAPTIVE if rubric.is_adaptive else Layer.JUDGE


def score_rubric(
    rubric: Rubric,
    ctx: ValidationContext,
    judge: JudgeClient,
    sample: str | None = None,
    *,
    applicability: Applicability = always_applicable,
    criteria_store: Any = None,
    sample_id: str = "",
    sample_formatter: Callable[[ValidationContext], str] = default_sample_block,
) -> MetricResult:
    """Score one rubric against one sample, generating criteria first if adaptive."""
    layer = layer_of(rubric)
    applicable, why_not, precondition = applicability(ctx)
    if not applicable:
        if precondition:
            return MetricResult.not_exercised(
                rubric.key, layer, rubric.stage, precondition, why_not, focus=rubric.focus
            )
        return MetricResult.not_applicable(rubric.key, layer, rubric.stage, why_not, focus=rubric.focus)

    sample = sample if sample is not None else sample_formatter(ctx)
    criteria: list[str] | None = None
    criteria_source = "n/a"

    if rubric.is_adaptive:
        # A pinned set means this sample is scored against the same bar it was
        # scored against last time, which is the only way two adaptive scores
        # are comparable at all.
        pinned = (
            criteria_store.get(sample_id, rubric.key) if criteria_store is not None else None
        )
        if pinned:
            criteria = list(pinned)
            criteria_source = "pinned"
        else:
            generated, _ = judge.invoke(
                f"{rubric.key}:criteria", build_criteria_prompt(rubric, sample), AdaptiveCriteria
            )
            if generated is None:
                return MetricResult.not_applicable(
                    rubric.key,
                    layer,
                    rubric.stage,
                    "criteria generation failed for this sample",
                    focus=rubric.focus,
                )
            if not generated.applicable or not generated.criteria:
                return MetricResult.not_applicable(
                    rubric.key,
                    layer,
                    rubric.stage,
                    "the judge found the rubric inapplicable to this sample",
                    focus=rubric.focus,
                )
            criteria = list(generated.criteria)
            criteria_source = "generated"
            if criteria_store is not None and sample_id:
                criteria_store.put(sample_id, rubric.key, criteria)

    verdict, samples = judge.invoke(rubric.key, build_scoring_prompt(rubric, sample, criteria), RubricVerdict)
    if verdict is None:
        # A judge that failed is a gap in coverage, never an implicit pass.
        return MetricResult.not_applicable(
            rubric.key,
            layer,
            rubric.stage,
            "the judge did not return a verdict for this sample",
            focus=rubric.focus,
        )
    if verdict.score is None:
        return MetricResult.not_applicable(
            rubric.key,
            layer,
            rubric.stage,
            verdict.reason or "the judge scored this rubric as not applicable",
            focus=rubric.focus,
        )

    return MetricResult.from_judge(
        rubric.key,
        layer,
        rubric.stage,
        verdict.score,
        rubric.threshold,
        verdict.reason or f"judge scored {verdict.score}/4",
        repeats=normalized_scores(samples),
        focus=rubric.focus,
        criteria=criteria or [],
        criteria_source=criteria_source,
        n_judge_samples=len(samples),
    )


def run_graded_metrics(
    rubrics: dict[str, Rubric],
    ctx: ValidationContext,
    judge: JudgeClient,
    *,
    layers: frozenset[Layer] | None = None,
    only: set[str] | None = None,
    applicability: dict[str, Applicability] | None = None,
    criteria_store: Any = None,
    sample_id: str = "",
    sample_formatter: Callable[[ValidationContext], str] = default_sample_block,
) -> dict[str, MetricResult]:
    """Score every rubric in ``rubrics`` that applies to this session's pipeline.

    ``applicability`` maps a rubric key to its :data:`Applicability` function;
    a rubric with no entry is treated as always applicable.
    """
    applicability = applicability or {}
    results: dict[str, MetricResult] = {}
    sample: str | None = None
    for key, rubric in rubrics.items():
        if only is not None and key not in only:
            continue
        if not rubric.applies_to(ctx.pipeline):
            continue
        if layers is not None and layer_of(rubric) not in layers:
            continue
        if sample is None:
            sample = sample_formatter(ctx)
        results[key] = score_rubric(
            rubric,
            ctx,
            judge,
            sample,
            applicability=applicability.get(key, always_applicable),
            criteria_store=criteria_store,
            sample_id=sample_id,
            sample_formatter=sample_formatter,
        )
    return results


__all__ = [
    "Applicability",
    "always_applicable",
    "build_criteria_prompt",
    "build_scoring_prompt",
    "default_sample_block",
    "layer_of",
    "run_graded_metrics",
    "score_rubric",
]
