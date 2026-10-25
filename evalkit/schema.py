"""Result schema and the normalization rules every metric must go through.

Scores are always "higher is better" on 0.0-1.0, but the constructors keep the
raw measurement in ``details`` -- an error rate reported only as ``1 - err`` is
unreportable, and the raw number is the one worth quoting.

Verdicts are deliberately four-valued. ``N/A`` is not a failure and not a pass:
it is excluded from both the mean and the pass rate and surfaces separately as
an applicability rate, so a check that never applied cannot read as a high
score. ``NOT_EXERCISED`` is the same arithmetic with a different meaning -- the
sample's own spec said this metric would be exercised and it was not, which is
a defect in the corpus or the pipeline rather than a property of the system's
answer.

Everything here is domain-agnostic on purpose: nothing in this module knows
about any particular product, pipeline or set of metrics. ``stage`` is a plain
string you define per project (e.g. ``"retrieval"``, ``"generation"``, or your
own ``S0``..``S5`` scheme) -- see ``evalkit.registry`` for how to attach
metrics to it.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Iterable

from evalkit.statistics import (
    DEFAULT_CONFIDENCE,
    DEFAULT_RESAMPLES,
    Interval,
    bootstrap_ci,
    proportion_ci,
)

#: A stage is just a string. Define your own set of stage names per project --
#: there is no fixed enum to subclass. A conventional end-to-end/cross-cutting
#: stage name is provided for convenience.
Stage = str
END_TO_END: Stage = "E2E"


class Layer(str, Enum):
    """The five evaluation layers, ordered by cost and exactness.

    STATIC and COMPUTATION need no model in the loop and are free; STANDARD,
    JUDGE and ADAPTIVE call a judge model and cost money. See ``evalkit.config``
    for how a run gates the paid layers behind the free ones.
    """

    STATIC = "L1a"
    COMPUTATION = "L1b"
    STANDARD = "L2"
    JUDGE = "L3"
    ADAPTIVE = "L3-adaptive"


#: Layers runnable without any model in the loop.
FREE_LAYERS = frozenset({Layer.STATIC, Layer.COMPUTATION})
#: Layers that call a judge model.
JUDGE_LAYERS = frozenset({Layer.STANDARD, Layer.JUDGE, Layer.ADAPTIVE})


class Focus(str, Enum):
    """What concern a metric protects, independent of how or where it is measured.

    Orthogonal to both ``Layer`` (how a verdict is produced) and ``stage``
    (which pipeline component it blames). A tenant-boundary check and an
    LLM-judged injection rubric can sit in different layers and stages but the
    same focus -- this is what lets a report answer "how is security doing" as
    one question instead of a hand-picked list of metric names.

    This taxonomy is deliberately generic and reusable across projects. Add
    your own values only if none of these fit -- most systems' concerns map
    onto one of these six.
    """

    #: Is the output right, faithful, and complete -- the default concern.
    CORRECTNESS = "correctness"
    #: Adversarial and access-control concerns: surviving a deliberate attempt
    #: to manipulate or breach the system.
    SECURITY = "security"
    #: Responsible-behaviour and trust concerns that are not staged by an
    #: attacker -- harmful content, fabricated citations, unauthorized advice.
    SAFETY = "safety"
    #: Schema, format and instruction-following conformance.
    COMPLIANCE = "compliance"
    #: Graceful degradation, termination backstops, recovery from bad input.
    ROBUSTNESS = "robustness"
    #: Cost: steps, tokens, latency.
    EFFICIENCY = "efficiency"


class Verdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    NA = "N/A"
    #: The sample declared a precondition that never fired. Scored like N/A,
    #: reported separately: a metric that legitimately does not apply and one
    #: the corpus promised would apply are not the same finding.
    NOT_EXERCISED = "NOT_EXERCISED"
    #: A deterministic layer already vouched for this sample, so the judge was
    #: never called -- the cheap layers standing in for the expensive one
    #: instead of paying to confirm what they already showed. Excluded from
    #: the mean and the pass rate like N/A: an inferred pass must never read
    #: as if it were a judged one.
    PASS_BY_PROXY = "PASS_BY_PROXY"


#: Verdicts excluded from the mean and the pass rate.
UNDECIDED = frozenset({Verdict.NA, Verdict.NOT_EXERCISED, Verdict.PASS_BY_PROXY})


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _finite(value: float | None) -> float | None:
    """NaN and infinities must never reach a JSON report."""
    if value is None:
        return None
    value = float(value)
    return None if math.isnan(value) or math.isinf(value) else value


@dataclass
class MetricResult:
    """One metric's verdict on one sample.

    Never construct this directly outside of the classmethod constructors --
    each one enforces exactly one normalization rule, which is what keeps
    every metric in a project comparable to every other.
    """

    name: str
    layer: Layer
    stage: Stage
    focus: Focus
    score: float | None
    verdict: Verdict
    reason: str
    details: dict[str, Any] = field(default_factory=dict)
    repeats: list[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.score = _finite(self.score)
        if self.score is not None:
            self.score = _clamp01(self.score)
        if self.verdict is not Verdict.NA and not self.reason.strip():
            raise ValueError(f"metric {self.name!r}: a {self.verdict.value} verdict needs a reason")

    @property
    def decided(self) -> bool:
        """True when this result carries a verdict about the system's behaviour."""
        return self.verdict not in UNDECIDED

    # -- constructors, one per normalization rule ---------------------------- #

    @classmethod
    def from_bool(
        cls,
        name: str,
        layer: Layer,
        stage: Stage,
        ok: bool,
        reason: str,
        *,
        focus: Focus,
        **details: Any,
    ) -> MetricResult:
        """Boolean check: score is 0.0 or 1.0."""
        return cls(
            name=name,
            layer=layer,
            stage=stage,
            focus=focus,
            score=1.0 if ok else 0.0,
            verdict=Verdict.PASS if ok else Verdict.FAIL,
            reason=reason,
            details=details,
        )

    @classmethod
    def from_ratio(
        cls,
        name: str,
        layer: Layer,
        stage: Stage,
        ratio: float,
        threshold: float,
        reason: str,
        *,
        focus: Focus,
        **details: Any,
    ) -> MetricResult:
        """Continuous 0-1 measurement with a declared passing threshold."""
        ratio = _clamp01(ratio)
        return cls(
            name=name,
            layer=layer,
            stage=stage,
            focus=focus,
            score=ratio,
            verdict=Verdict.PASS if ratio >= threshold else Verdict.FAIL,
            reason=reason,
            details={"threshold": threshold, **details},
        )

    @classmethod
    def from_error_rate(
        cls,
        name: str,
        layer: Layer,
        stage: Stage,
        error_rate: float,
        max_error: float,
        reason: str,
        *,
        focus: Focus,
        **details: Any,
    ) -> MetricResult:
        """Lower-is-better measurement (e.g. CER/WER) inverted for reporting.

        The raw rate stays in ``details`` -- it is the number worth quoting.
        """
        error_rate = max(0.0, float(error_rate))
        return cls(
            name=name,
            layer=layer,
            stage=stage,
            focus=focus,
            score=1.0 - min(error_rate, 1.0),
            verdict=Verdict.PASS if error_rate <= max_error else Verdict.FAIL,
            reason=reason,
            details={"raw_error_rate": error_rate, "max_error": max_error, **details},
        )

    @classmethod
    def from_judge(
        cls,
        name: str,
        layer: Layer,
        stage: Stage,
        judge_score: float,
        threshold: float,
        reason: str,
        repeats: Iterable[float] = (),
        *,
        focus: Focus,
        **details: Any,
    ) -> MetricResult:
        """Judge rating on the 1-4 scale, mapped onto 0-1."""
        normalized = _clamp01((float(judge_score) - 1.0) / 3.0)
        return cls(
            name=name,
            layer=layer,
            stage=stage,
            focus=focus,
            score=normalized,
            verdict=Verdict.PASS if normalized >= threshold else Verdict.FAIL,
            reason=reason,
            details={"judge_score_1_4": float(judge_score), "threshold": threshold, **details},
            repeats=[float(r) for r in repeats],
        )

    @classmethod
    def not_applicable(
        cls,
        name: str,
        layer: Layer,
        stage: Stage,
        reason: str = "",
        *,
        focus: Focus,
        **details: Any,
    ) -> MetricResult:
        return cls(
            name=name,
            layer=layer,
            stage=stage,
            focus=focus,
            score=None,
            verdict=Verdict.NA,
            reason=reason,
            details=details,
        )

    @classmethod
    def not_exercised(
        cls,
        name: str,
        layer: Layer,
        stage: Stage,
        precondition: str,
        reason: str,
        *,
        focus: Focus,
        **details: Any,
    ) -> MetricResult:
        """The sample declared this metric would apply, and it did not fire.

        Unlike :meth:`not_applicable` the reason is mandatory and the declared
        precondition is recorded alongside it, because the finding is always
        about a specific promise the corpus made that the run did not keep.
        """
        if not reason.strip():
            raise ValueError(f"metric {name!r}: a NOT_EXERCISED verdict needs a reason")
        return cls(
            name=name,
            layer=layer,
            stage=stage,
            focus=focus,
            score=None,
            verdict=Verdict.NOT_EXERCISED,
            reason=reason,
            details={"declared_precondition": precondition, **details},
        )

    @classmethod
    def pass_by_proxy(
        cls,
        name: str,
        layer: Layer,
        stage: Stage,
        reason: str,
        *,
        focus: Focus,
        **details: Any,
    ) -> MetricResult:
        """The judge was never called: a deterministic layer already vouched for this sample.

        Carries no score, like :meth:`not_applicable` -- the inference is about
        cost, not quality, and must not be averaged in as if a judge had agreed.
        """
        return cls(
            name=name,
            layer=layer,
            stage=stage,
            focus=focus,
            score=None,
            verdict=Verdict.PASS_BY_PROXY,
            reason=reason,
            details=details,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer.value,
            "stage": self.stage,
            "focus": self.focus.value,
            "score": self.score,
            "verdict": self.verdict.value,
            "reason": self.reason,
            "details": self.details,
            "repeats": self.repeats,
        }


@dataclass
class SampleResult:
    """Every metric's verdict on one evaluation row, plus its trace."""

    sample_id: str
    session_id: str
    pipeline: str
    turn_index: int = 0
    prompt: str = ""
    response: str = ""
    reference: dict[str, Any] | None = None
    stages_covered: list[Stage] = field(default_factory=list)
    trace: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, MetricResult] = field(default_factory=dict)

    def add(self, result: MetricResult) -> None:
        self.metrics[result.name] = result

    def to_dict(self, *, include_trace: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "sample_id": self.sample_id,
            "session_id": self.session_id,
            "pipeline": self.pipeline,
            "turn_index": self.turn_index,
            "prompt": self.prompt,
            "response": self.response,
            "reference": self.reference,
            "stages_covered": list(self.stages_covered),
            "metrics": {name: m.to_dict() for name, m in self.metrics.items()},
        }
        if include_trace:
            payload["trace"] = self.trace
        return payload


@dataclass
class MetricSummary:
    """Aggregate statistics for one metric across all samples."""

    name: str
    layer: Layer
    stage: Stage
    focus: Focus
    n_scored: int = 0
    n_skipped: int = 0
    skip_reason: str = ""
    mean: float | None = None
    std: float | None = None
    min: float | None = None
    max: float | None = None
    pass_rate: float | None = None
    n_pass: int = 0
    n_fail: int = 0
    n_na: int = 0
    n_not_exercised: int = 0
    #: Verdict.PASS_BY_PROXY: the judge was never called because a
    #: deterministic layer already passed. Counted like N/A, not like PASS --
    #: an inferred pass must stay visibly distinct from a judged one.
    n_pass_by_proxy: int = 0
    proxy_reason: str = ""
    applicability_rate: float | None = None
    repeat_variance: float | None = None
    #: Bootstrap intervals. ``None`` when nothing scored, never a fabricated range.
    mean_ci: Interval | None = None
    pass_rate_ci: Interval | None = None
    #: Samples whose spec declared this metric would apply and where it did not fire.
    not_exercised_samples: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["layer"] = self.layer.value
        payload["stage"] = self.stage
        payload["focus"] = self.focus.value
        payload.pop("name")
        payload["mean_ci"] = self.mean_ci.to_dict() if self.mean_ci else None
        payload["pass_rate_ci"] = self.pass_rate_ci.to_dict() if self.pass_rate_ci else None
        return payload


def _skip_reason_summary(
    results: list[MetricResult], verdict: Verdict = Verdict.NA, max_reasons: int = 2
) -> str:
    """Why a verdict happened across samples, as a reader-facing string.

    Most causes are one reason repeated across samples, so the common case
    reads as one clear sentence; a metric with several distinct causes lists
    the top few by count rather than picking one arbitrarily.
    """
    reasons = Counter(r.reason for r in results if r.verdict is verdict and r.reason)
    if not reasons:
        return ""
    top = reasons.most_common(max_reasons)
    parts = [f"{reason} ({count})" for reason, count in top]
    remaining = len(reasons) - len(top)
    if remaining > 0:
        parts.append(f"+{remaining} more reason(s)")
    return "; ".join(parts)


def summarize_metric(
    name: str,
    results: list[MetricResult],
    skipped: int = 0,
    *,
    sample_ids: list[str] | None = None,
    resamples: int = DEFAULT_RESAMPLES,
    confidence: float = DEFAULT_CONFIDENCE,
) -> MetricSummary:
    """Aggregate one metric, applying the undecided-verdict exclusion rules.

    Every reported number carries the ``n`` behind it and, where there is one,
    a bootstrap interval: a mean of 0.91 over four samples and the same mean
    over ninety are different claims, and only the interval says which one is
    being made.
    """
    if not results and not skipped:
        raise ValueError(f"no results to summarize for metric {name!r}")

    layer = results[0].layer if results else Layer.STATIC
    stage = results[0].stage if results else END_TO_END
    focus = results[0].focus if results else Focus.CORRECTNESS

    scored = [r.score for r in results if r.score is not None and r.decided]
    n_pass = sum(1 for r in results if r.verdict is Verdict.PASS)
    n_fail = sum(1 for r in results if r.verdict is Verdict.FAIL)
    n_na = sum(1 for r in results if r.verdict is Verdict.NA)
    n_not_exercised = sum(1 for r in results if r.verdict is Verdict.NOT_EXERCISED)
    n_pass_by_proxy = sum(1 for r in results if r.verdict is Verdict.PASS_BY_PROXY)

    ids = sample_ids or []
    summary = MetricSummary(
        name=name,
        layer=layer,
        stage=stage,
        focus=focus,
        n_scored=len(scored),
        n_skipped=skipped,
        n_pass=n_pass,
        n_fail=n_fail,
        n_na=n_na,
        n_not_exercised=n_not_exercised,
        n_pass_by_proxy=n_pass_by_proxy,
        skip_reason=_skip_reason_summary(results, Verdict.NA),
        proxy_reason=_skip_reason_summary(results, Verdict.PASS_BY_PROXY),
        not_exercised_samples=[
            ids[i]
            for i, r in enumerate(results)
            if r.verdict is Verdict.NOT_EXERCISED and i < len(ids)
        ],
    )

    decided = n_pass + n_fail
    if decided:
        summary.pass_rate = n_pass / decided
        summary.pass_rate_ci = proportion_ci(
            n_pass, decided, resamples=resamples, confidence=confidence
        )
    if results:
        # NOT_EXERCISED sits in the denominator exactly as N/A does: both are a
        # sample this metric produced no verdict about.
        summary.applicability_rate = decided / len(results)

    if scored:
        mean = sum(scored) / len(scored)
        summary.mean = mean
        summary.min = min(scored)
        summary.max = max(scored)
        # Population std: the samples are the whole evaluated set, not a draw from it.
        summary.std = math.sqrt(sum((s - mean) ** 2 for s in scored) / len(scored))
        summary.mean_ci = bootstrap_ci(scored, resamples=resamples, confidence=confidence)

    variances = [
        sum((r - sum(m.repeats) / len(m.repeats)) ** 2 for r in m.repeats) / len(m.repeats)
        for m in results
        if len(m.repeats) > 1
    ]
    if variances:
        summary.repeat_variance = sum(variances) / len(variances)

    return summary


@dataclass
class GroupSummary:
    """Rollup of several metrics that share a stage or a focus."""

    key: str
    mean_score: float | None
    pass_rate: float | None
    metrics: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {"mean_score": self.mean_score, "pass_rate": self.pass_rate, "metrics": self.metrics}


def _rollup(summaries: dict[str, MetricSummary], key_of) -> dict[str, GroupSummary]:
    groups: dict[str, list[MetricSummary]] = {}
    for summary in summaries.values():
        groups.setdefault(key_of(summary), []).append(summary)
    rollup: dict[str, GroupSummary] = {}
    for key, members in groups.items():
        means = [s.mean for s in members if s.mean is not None]
        rates = [s.pass_rate for s in members if s.pass_rate is not None]
        rollup[key] = GroupSummary(
            key=key,
            mean_score=sum(means) / len(means) if means else None,
            pass_rate=sum(rates) / len(rates) if rates else None,
            metrics=sorted(s.name for s in members),
        )
    return rollup


def rollup_by_stage(summaries: dict[str, MetricSummary]) -> dict[str, GroupSummary]:
    """Per-stage rollup, computed from metric attribution rather than hand-assigned."""
    return _rollup(summaries, lambda s: s.stage)


def rollup_by_focus(summaries: dict[str, MetricSummary]) -> dict[str, GroupSummary]:
    """Per-focus rollup -- "how is safety doing" as one number instead of a hand-picked list."""
    return _rollup(summaries, lambda s: s.focus.value)


__all__ = [
    "END_TO_END",
    "FREE_LAYERS",
    "JUDGE_LAYERS",
    "Focus",
    "GroupSummary",
    "Layer",
    "MetricResult",
    "MetricSummary",
    "SampleResult",
    "Stage",
    "UNDECIDED",
    "Verdict",
    "rollup_by_focus",
    "rollup_by_stage",
    "summarize_metric",
]
