"""The evaluation runner: sessions in, a scored report out.

Layer order is a cost decision, not a stylistic one. The deterministic layers
(STATIC, COMPUTATION) run first and, when a sample already passes all of them,
its judge calls are skipped and the judge-graded metrics are recorded as
PASS_BY_PROXY -- the cheap layers standing in for the expensive ones instead
of paying to re-confirm what they already showed. A sample with any
deterministic failure gets the judge anyway, since that is exactly the case
worth paying to diagnose. The skip is counted so the report says how much was
inferred rather than implying full coverage.

Nothing in this module is specific to any one project. Wire in your own
``MetricRegistry`` instances, your own rubric dict, and your own reference
lookup, and the runner does the same layering, gating, aggregation and gate
logic regardless of what those metrics measure.
"""

from __future__ import annotations

import logging
import platform
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Sequence

import evalkit
from evalkit.config import DEFAULT_MIN_REGRESSION_DELTA, EvaluationConfig
from evalkit.context import ValidationContext
from evalkit.graded import Applicability, default_sample_block, run_graded_metrics
from evalkit.judge import JudgeClient
from evalkit.registry import MetricRegistry
from evalkit.rubrics import Rubric
from evalkit.schema import (
    END_TO_END,
    FREE_LAYERS,
    Layer,
    MetricResult,
    MetricSummary,
    SampleResult,
    Stage,
    Verdict,
    rollup_by_focus,
    rollup_by_stage,
    summarize_metric,
)
from evalkit.session import EvalSession, EvalTurn
from evalkit.statistics import DEFAULT_CONFIDENCE, DEFAULT_RESAMPLES, Interval, separated

logger = logging.getLogger(__name__)


@dataclass
class EvaluationReport:
    """A complete run: manifest, per-sample results, and aggregates."""

    run_manifest: dict[str, Any]
    samples: list[SampleResult]
    summaries: dict[str, MetricSummary] = field(default_factory=dict)
    counts: dict[str, Any] = field(default_factory=dict)
    gate: dict[str, Any] = field(default_factory=dict)
    coverage_gaps: dict[str, Any] = field(default_factory=dict)
    #: Optional: capability name -> metrics that would cover it, so a report
    #: can say which declared capabilities this run actually measured. Pass
    #: your own mapping to ``EvaluationRunner(capability_coverage=...)``.
    capability_coverage_map: dict[str, tuple[str, ...]] = field(default_factory=dict, repr=False)

    @property
    def summary(self) -> dict[str, Any]:
        by_stage = {stage: rollup.to_dict() for stage, rollup in rollup_by_stage(self.summaries).items()}
        by_focus = {focus: rollup.to_dict() for focus, rollup in rollup_by_focus(self.summaries).items()}
        payload: dict[str, Any] = {
            **self.counts,
            "metrics": {name: summary.to_dict() for name, summary in sorted(self.summaries.items())},
            "by_stage": by_stage,
            "by_focus": by_focus,
            "coverage_gaps": self.coverage_gaps,
            "gate": self.gate,
        }
        if self.capability_coverage_map:
            payload["capability_coverage"] = self.capability_coverage()
        return payload

    def capability_coverage(self) -> dict[str, dict[str, Any]]:
        """Which declared capabilities this run actually measured."""
        coverage: dict[str, dict[str, Any]] = {}
        for capability, metrics in self.capability_coverage_map.items():
            scoring = [
                name for name in metrics if name in self.summaries and self.summaries[name].n_scored > 0
            ]
            coverage[capability] = {
                "covered": bool(scoring),
                "scoring_metrics": scoring,
                "declared_metrics": list(metrics),
            }
        return coverage

    def to_summary_dict(self) -> dict[str, Any]:
        return {"run_manifest": self.run_manifest, "summary": self.summary}

    def metric(self, name: str) -> MetricSummary | None:
        return self.summaries.get(name)


def _git_revision() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - git may be absent
        return ""


def build_manifest(
    config: EvaluationConfig,
    *,
    run_id: str,
    timestamp: str,
    corpus_hash: str = "",
    duration_s: float = 0.0,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Everything needed to reproduce, or to distrust, a published number.

    ``extra`` is where project-specific identity goes -- the model under test,
    its provider, an app version -- so the manifest can name what was measured
    without this module needing to know your settings module exists.
    """
    return {
        "run_id": run_id,
        "timestamp": timestamp,
        "corpus_hash": corpus_hash,
        "git_rev": _git_revision(),
        "python": platform.python_version(),
        "framework_version": evalkit.__version__,
        "duration_s": round(duration_s, 3),
        **config.manifest_fields(),
        **(extra or {}),
    }


@dataclass
class EvaluationRunner:
    """Scores captured sessions against your registered metrics and rubrics.

    Every project-specific piece is a constructor argument rather than an
    import: your two ``MetricRegistry`` instances, your rubric dict, your
    applicability functions, and (optionally) how to render a sample for a
    judge and how to look up ground truth for a session. Live execution --
    actually driving your pipeline to produce sessions -- is your own thin
    wrapper; this runner only turns already-captured ``EvalSession`` objects
    into a scored report.
    """

    config: EvaluationConfig = field(default_factory=EvaluationConfig)
    judge: JudgeClient | None = None

    #: Your static/computation metric registries. Both optional -- a project
    #: using only judge-graded rubrics can leave them empty.
    static_metrics: MetricRegistry | None = None
    computation_metrics: MetricRegistry | None = None

    #: Judge-graded metrics written as *functions* rather than rubric rows --
    #: e.g. a third-party metric library wrapped to return a MetricResult.
    #: Runs in the same branch as the rubric-graded layers, so it is gated by
    #: ``gate_judge_on_static`` and skipped as PASS_BY_PROXY on the same
    #: samples. Its registry layer must be STANDARD, JUDGE or ADAPTIVE.
    graded_metrics: MetricRegistry | None = None

    #: Judge-graded rubrics (from ``evalkit.rubrics.load_rubrics``) and their
    #: applicability functions (see ``evalkit.graded``).
    rubrics: dict[str, Rubric] = field(default_factory=dict)
    applicability: dict[str, Applicability] = field(default_factory=dict)
    sample_formatter: Callable[[ValidationContext], str] = default_sample_block

    #: Looks up ground truth for a session -- typically a dict keyed by
    #: ``session.session_id`` from your golden set, or a callable if the
    #: lookup needs computing. Only used when ``run()`` is not given
    #: ``references`` directly.
    reference_lookup: dict[str, dict[str, Any] | None] | Callable[[EvalSession], dict[str, Any] | None] | None = None

    #: Anything a metric needs beyond the session and reference -- an external
    #: store snapshot, model names, a price table -- exposed as
    #: ``ctx.extras``. See ``evalkit.context.ValidationContext``.
    extras: dict[str, Any] = field(default_factory=dict)

    #: Optional persistence for generated L3-adaptive criteria; see
    #: ``evalkit.testing`` for an in-memory stand-in and roll your own for
    #: on-disk pinning if you need cross-run stability.
    criteria_store: Any = None

    #: capability name -> metric names that would demonstrate it. Purely for
    #: reporting; see ``EvaluationReport.capability_coverage``.
    capability_coverage: dict[str, tuple[str, ...]] = field(default_factory=dict)

    #: Extra fields folded into the run manifest (model name, app version...).
    manifest_extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.config.validate()
        if self.judge is None and self.config.uses_judge:
            self.judge = JudgeClient(repeats=self.config.judge_repeats)
        if self.graded_metrics is not None and self.graded_metrics.layer in FREE_LAYERS:
            raise ValueError(
                f"graded_metrics holds {self.graded_metrics.layer.value}, a free layer -- "
                "paid metrics registered there would be priced as free and would drive "
                "PASS_BY_PROXY. Use static_metrics or computation_metrics instead."
            )

    # -- scoring ------------------------------------------------------------- #

    def _lookup_reference(self, session: EvalSession) -> dict[str, Any] | None:
        lookup = self.reference_lookup
        if lookup is None:
            return None
        if callable(lookup):
            return lookup(session)
        return lookup.get(session.session_id)

    def score_sessions(
        self, session: EvalSession, reference: dict[str, Any] | None = None
    ) -> list[SampleResult]:
        """Every scorable sample in a session: one per turn.

        A single-turn session yields exactly one sample and behaves as it
        always did. A conversation yields one per turn, each scored against
        that turn's own ground truth (``reference["turn_references"][i]``), so
        a report says which turn regressed rather than that the conversation
        did.
        """
        reference = reference if reference is not None else self._lookup_reference(session)
        turn_references = (reference or {}).get("turn_references")
        if not turn_references:
            return [self.score_session(session, reference)]

        samples: list[SampleResult] = []
        for turn in session.turns:
            if turn.index >= len(turn_references):
                # More turns than ground truth: scoring them against the last
                # reference would invent results, so they are left unscored.
                logger.warning(
                    "session %s turn %d has no reference and was not scored",
                    session.session_id,
                    turn.index,
                )
                break
            merged = {
                **{k: v for k, v in (reference or {}).items() if k != "turn_references"},
                **turn_references[turn.index],
            }
            samples.append(self.score_session(session, merged, turn=turn))
        return samples

    def score_session(
        self,
        session: EvalSession,
        reference: dict[str, Any] | None = None,
        *,
        turn: EvalTurn | None = None,
    ) -> SampleResult:
        ctx = ValidationContext(session=session, reference=reference, turn=turn, extras=self.extras)
        suffix = f":t{turn.index}" if turn is not None else ""
        sample = SampleResult(
            sample_id=f"{session.pipeline}:{session.session_id}{suffix}",
            session_id=session.session_id,
            pipeline=session.pipeline,
            turn_index=turn.index if turn is not None else 0,
            prompt=(
                turn.prompt
                if turn is not None
                else next((e.content for e in session.events if e.type == "user"), "")
            ),
            response=str(ctx.state.get("final_answer") or "")[:4000],
            reference=reference or None,
            trace=session.trace() if turn is None else {**session.trace(), "turn": turn.to_dict()},
        )

        results: dict[str, MetricResult] = {}
        if self.config.enabled(Layer.STATIC) and self.static_metrics is not None:
            results.update(self.static_metrics.run(ctx))
        if self.config.enabled(Layer.COMPUTATION) and self.computation_metrics is not None:
            results.update(self.computation_metrics.run(ctx))

        graded_layers = frozenset(
            layer for layer in (Layer.STANDARD, Layer.JUDGE, Layer.ADAPTIVE) if self.config.enabled(layer)
        )
        graded_registry = (
            self.graded_metrics
            if self.graded_metrics is not None and self.config.enabled(self.graded_metrics.layer)
            else None
        )
        if graded_layers and (self.rubrics or graded_registry is not None):
            deterministic = self._deterministic_verdicts(results)
            all_passed = bool(deterministic) and all(
                verdict is Verdict.PASS for verdict in deterministic.values()
            )
            if all_passed and self.config.gate_judge_on_static:
                # The deterministic layers already vouch for this sample;
                # paying a judge to re-confirm it buys nothing. Record the
                # inference instead of implying it was judged.
                reason = (
                    f"skipped: deterministic layers already passed "
                    f"({', '.join(sorted(deterministic)[:3])})"
                )
                for key, rubric in _rubrics_for(self.rubrics, session.pipeline, graded_layers):
                    from evalkit.graded import layer_of

                    results[key] = MetricResult.pass_by_proxy(
                        key, layer_of(rubric), rubric.stage, reason, focus=rubric.focus
                    )
                if graded_registry is not None:
                    for name in graded_registry.applicable(session.pipeline):
                        results[name] = MetricResult.pass_by_proxy(
                            name,
                            graded_registry.layer,
                            graded_registry.stage_of(name),
                            reason,
                            focus=graded_registry.focus_of(name),
                        )
            else:
                if self.rubrics:
                    assert self.judge is not None
                    results.update(
                        run_graded_metrics(
                            self.rubrics,
                            ctx,
                            self.judge,
                            layers=graded_layers,
                            applicability=self.applicability,
                            criteria_store=self.criteria_store,
                            sample_id=sample.sample_id,
                            sample_formatter=self.sample_formatter,
                        )
                    )
                if graded_registry is not None:
                    results.update(graded_registry.run(ctx))

        for result in results.values():
            sample.add(result)
        sample.stages_covered = sorted({result.stage for result in results.values()})
        return sample

    @staticmethod
    def _deterministic_verdicts(results: dict[str, MetricResult]) -> dict[str, Verdict]:
        """Every STATIC/COMPUTATION verdict that actually decided something.

        N/A and NOT_EXERCISED entries carry no evidence either way, so they
        are left out -- a sample with no decided deterministic metric at all
        has nothing to infer a proxy pass from.
        """
        return {
            name: result.verdict
            for name, result in results.items()
            if result.layer in {Layer.STATIC, Layer.COMPUTATION} and result.decided
        }

    # -- runs ---------------------------------------------------------------- #

    def run(
        self,
        sessions: Iterable[EvalSession],
        *,
        references: dict[str, dict[str, Any] | None] | None = None,
        run_id: str = "run",
        timestamp: str | None = None,
        duration_s: float = 0.0,
        corpus_hash: str = "",
    ) -> EvaluationReport:
        sessions = list(sessions)
        samples = [
            sample
            for session in sessions
            for sample in self.score_sessions(
                session, (references.get(session.session_id) if references is not None else None)
            )
        ]
        report = EvaluationReport(
            run_manifest=build_manifest(
                self.config,
                run_id=run_id,
                timestamp=timestamp or datetime.now(timezone.utc).isoformat(),
                corpus_hash=corpus_hash,
                duration_s=duration_s,
                extra=self.manifest_extra,
            ),
            samples=samples,
            capability_coverage_map=self.capability_coverage,
        )
        report.run_manifest["judge_calls"] = self.judge.call_count if self.judge else 0
        report.run_manifest.update(self.economics(sessions))
        report.summaries = aggregate(
            samples,
            resamples=self.config.bootstrap_resamples,
            confidence=self.config.ci_confidence,
        )
        report.counts = sample_counts(samples)
        # The denominators travel with the manifest as well as the summary: a
        # score quoted from the manifest alone must still say what n it is over.
        report.run_manifest.update(
            {key: report.counts[key] for key in ("n_samples", "n_per_pipeline")}
        )
        report.gate = evaluate_gate(report.summaries, self.config)
        report.coverage_gaps = coverage_gaps(report.summaries, samples, gate_floors=self.config.gate_floors)
        return report

    # -- economics ----------------------------------------------------------- #

    def economics(self, sessions: list[EvalSession]) -> dict[str, Any]:
        """What the run consumed: tokens spent running the system vs. measuring it.

        Tokens are the primary unit because they are what was actually spent
        and every backend reports them. Cost-in-money is deliberately not
        computed here -- price lists change without the system changing at
        all; convert ``tokens_app`` / ``tokens_judge_estimated`` yourself from
        whatever pricing table your project maintains, and put the result in
        ``manifest_extra`` if you want it published alongside this.
        """
        app_usage: dict[str, int] = {}
        for session in sessions:
            for key, value in session.token_usage.items():
                app_usage[key] = app_usage.get(key, 0) + int(value)
        app_basis = "provider-reported token usage"
        if not app_usage:
            estimated = sum(session.estimated_tokens for session in sessions)
            if estimated:
                app_usage = {"total_tokens": estimated}
                app_basis = "estimated from message text (lower bound)"

        judge_tokens = self.judge.estimated_tokens() if self.judge else {}
        return {
            "tokens_app": app_usage,
            "tokens_judge_estimated": judge_tokens,
            "token_accounting": token_accounting(app_usage, app_basis, judge_tokens),
        }


def token_accounting(main: dict[str, int], main_basis: str, judge: dict[str, int]) -> dict[str, Any]:
    """Tokens spent running the system, against tokens spent measuring it.

    The ratio is the framework's own cost of ownership: the deterministic
    layers are free, so an evaluation that spends several times what the
    system itself spent is spending it all on the judge layers. Asserting
    that is cheap; measuring it is the point.
    """
    main_total = int(main.get("total_tokens", 0) or 0) or sum(
        int(main.get(key, 0) or 0) for key in ("input_tokens", "output_tokens")
    )
    judge_total = int(judge.get("total_tokens", 0) or 0)
    total = main_total + judge_total

    return {
        "main": {"total_tokens": main_total, "basis": main_basis},
        "judge": {"total_tokens": judge_total, "basis": "estimated from prompt/response length"},
        "total_tokens": total,
        "judge_share": round(judge_total / total, 4) if total else None,
        # How many times the system's own spend the measurement cost.
        "evaluation_multiple": round(judge_total / main_total, 2) if main_total else None,
    }


def _rubrics_for(rubrics: dict[str, Rubric], pipeline: str, layers: frozenset[Layer]):
    from evalkit.graded import layer_of

    return [
        (key, rubric)
        for key, rubric in rubrics.items()
        if rubric.applies_to(pipeline) and layer_of(rubric) in layers
    ]


def sample_counts(samples: list[SampleResult]) -> dict[str, Any]:
    """The denominators behind every number in the report.

    A mean with no ``n`` beside it is not quotable, and neither is a pass rate
    over an unstated mix of pipelines.
    """
    conversations = {s.session_id for s in samples if s.turn_index > 0}

    per_pipeline: dict[str, int] = {}
    for sample in samples:
        per_pipeline[sample.pipeline] = per_pipeline.get(sample.pipeline, 0) + 1

    return {
        "total_samples": len(samples),
        # The same number under the name the manifest and any header use.
        "n_samples": len(samples),
        "n_sessions": len({s.session_id for s in samples}),
        "n_conversations": len(conversations),
        "n_per_pipeline": dict(sorted(per_pipeline.items())),
    }


def aggregate(
    samples: list[SampleResult],
    *,
    resamples: int = DEFAULT_RESAMPLES,
    confidence: float = DEFAULT_CONFIDENCE,
) -> dict[str, MetricSummary]:
    grouped: dict[str, list[MetricResult]] = {}
    ids: dict[str, list[str]] = {}
    for sample in samples:
        for name, result in sample.metrics.items():
            grouped.setdefault(name, []).append(result)
            ids.setdefault(name, []).append(sample.sample_id)
    return {
        name: summarize_metric(
            name,
            results,
            sample_ids=ids.get(name),
            resamples=resamples,
            confidence=confidence,
        )
        for name, results in sorted(grouped.items())
    }


def evaluate_gate(summaries: dict[str, MetricSummary], config: EvaluationConfig) -> dict[str, Any]:
    """Gate verdict: per-metric floors first, then the composite.

    The floors exist because a composite average is the wrong instrument for a
    safety property. Averaged against seven healthy format checks, one sample
    that crossed a tenant boundary moves the composite by an eighth of its own
    failure and ships. A floor is checked on its own, and a metric that
    breaches one fails the run whatever the average says.

    A floor metric that scored nothing does *not* pass by default. It is
    reported as unmeasured, both here and in ``coverage_gaps``, because "we
    did not look" and "we looked and it was clean" are the two readings a
    silent pass conflates.

    ``config.gate_metrics`` empty means the composite is computed over every
    metric that scored at least one sample -- set it explicitly once you know
    which metrics matter most for your change surface.
    """
    keys = config.gate_metrics or tuple(
        name for name, s in summaries.items() if s.n_scored > 0
    )
    present = [summaries[key] for key in keys if key in summaries and summaries[key].n_scored > 0]

    floor_breaches: list[dict[str, Any]] = []
    floors_unmeasured: list[str] = []
    for name, floor in sorted(config.gate_floors.items()):
        summary = summaries.get(name)
        if summary is None or summary.n_scored == 0 or summary.mean is None:
            floors_unmeasured.append(name)
            continue
        if summary.mean < float(floor):
            floor_breaches.append(
                {
                    "metric": name,
                    "mean": round(summary.mean, 6),
                    "floor": float(floor),
                    "n_scored": summary.n_scored,
                    "n_fail": summary.n_fail,
                    "stage": summary.stage,
                    "focus": summary.focus.value,
                }
            )

    floor_note = (
        f"; floors not measured in this run: {floors_unmeasured}" if floors_unmeasured else ""
    )

    if not present:
        return {
            "threshold": config.gate_threshold,
            "composite_score": None,
            # A floor breach is a verdict even when no composite metric
            # scored: a run that measured only one floor metric and found a
            # breach has failed.
            "verdict": "FAIL" if floor_breaches else "N/A",
            "blocking_metrics": [b["metric"] for b in floor_breaches],
            "floor_breaches": floor_breaches,
            "floors": dict(sorted(config.gate_floors.items())),
            "floors_unmeasured": floors_unmeasured,
            "reason": (
                f"{len(floor_breaches)} metric(s) below their hard floor: "
                f"{[b['metric'] for b in floor_breaches]}"
                if floor_breaches
                else f"no gate metric scored in this run{floor_note}"
            ),
            "metrics": list(keys),
        }

    composite = sum(s.mean for s in present if s.mean is not None) / len(present)
    below_threshold = sorted(
        s.name for s in present if s.mean is not None and s.mean < config.gate_threshold
    )
    blocking = sorted({*below_threshold, *(b["metric"] for b in floor_breaches)})
    passed = composite >= config.gate_threshold and not blocking

    if passed:
        reason = f"composite {composite:.3f} at or above the {config.gate_threshold} threshold{floor_note}"
    elif floor_breaches:
        # Named first: a floor breach is the more serious finding, and burying
        # it behind a composite number is exactly the failure the floors
        # exist for.
        breached = ", ".join(f"{b['metric']} {b['mean']:.3f} < {b['floor']}" for b in floor_breaches)
        reason = f"hard floor breached: {breached}; composite {composite:.3f}{floor_note}"
    else:
        reason = f"composite {composite:.3f}; below threshold: {below_threshold or 'none'}{floor_note}"

    return {
        "threshold": config.gate_threshold,
        "composite_score": composite,
        "verdict": "PASS" if passed else "FAIL",
        "blocking_metrics": blocking,
        "below_threshold": below_threshold,
        "floor_breaches": floor_breaches,
        "floors": dict(sorted(config.gate_floors.items())),
        "floors_unmeasured": floors_unmeasured,
        "reason": reason,
        "metrics": [s.name for s in present],
    }


# --------------------------------------------------------------------------- #
# Coverage gaps
# --------------------------------------------------------------------------- #
def coverage_gaps(
    summaries: dict[str, MetricSummary],
    samples: list[SampleResult],
    *,
    gate_floors: dict[str, float] | None = None,
) -> dict[str, Any]:
    """What this run did not measure, stated rather than left to inference.

    Every entry here is a place a reader could otherwise mistake silence for a
    clean result: a precondition the corpus promised and the pipeline never
    met, a backend that was not installed, a floor metric nothing checked.
    """
    gaps: dict[str, Any] = {}

    not_exercised = {
        name: {"n": summary.n_not_exercised, "samples": summary.not_exercised_samples[:10]}
        for name, summary in sorted(summaries.items())
        if summary.n_not_exercised
    }
    if not_exercised:
        gaps["not_exercised"] = not_exercised

    unmeasured_floors = sorted(
        name
        for name in (gate_floors or {})
        if name not in summaries or summaries[name].n_scored == 0
    )
    if unmeasured_floors:
        gaps["unmeasured_floors"] = unmeasured_floors

    # A metric present on every sample and N/A on every one of them is a
    # backend or a reference that was missing, not a property of the system.
    always_undecided = sorted(
        name
        for name, summary in summaries.items()
        if summary.n_scored == 0 and (summary.n_na + summary.n_not_exercised) > 0
    )
    if always_undecided:
        gaps["never_scored"] = {
            name: summaries[name].n_na + summaries[name].n_not_exercised
            for name in always_undecided
        }

    # Distinct from the above: this judge rubric produced no verdict of its
    # own not because a backend or reference was missing, but because every
    # sample that reached it already passed deterministically. A high count
    # here reads as coverage unless it is named -- the rubric was never
    # actually judged.
    always_proxy = sorted(
        name
        for name, summary in summaries.items()
        if summary.n_scored == 0
        and summary.n_pass_by_proxy > 0
        and (summary.n_na + summary.n_not_exercised) == 0
    )
    if always_proxy:
        gaps["always_pass_by_proxy"] = {name: summaries[name].n_pass_by_proxy for name in always_proxy}

    unscored_samples = sorted(
        sample.sample_id for sample in samples if not any(result.decided for result in sample.metrics.values())
    )
    if unscored_samples:
        gaps["samples_with_no_verdict"] = unscored_samples[:20]

    return gaps


# --------------------------------------------------------------------------- #
# Comparison / regression attribution
# --------------------------------------------------------------------------- #
#: A drop smaller than this is treated as noise rather than a regression.
MATERIAL_DELTA = 0.02

#: Coverage loss needs no noise band. Over the same corpus, how many samples a
#: metric could score is deterministic: if it scored eight before and seven
#: now, something stopped being measurable, and there is no sampling error to
#: discount.
MIN_COVERAGE_LOSS = 1


def _score_regression(
    delta: float | None,
    before_ci: Interval | None,
    after_ci: Interval | None,
    *,
    material: float,
    min_delta: float,
) -> tuple[bool, str]:
    """Is this drop a regression, or is it noise dressed as one?

    Two ways to qualify, and a drop needs only one of them:

    * the two confidence intervals do not overlap -- the corpus is large
      enough, or the move decisive enough, that resampling separates them; or
    * the drop is at least ``min_delta``, which is how a regression can still
      be called when a baseline predates intervals or the sample is too small
      to produce one.
    """
    if delta is None or delta > -material:
        return False, "within the material-delta band"
    if separated(before_ci, after_ci):
        return True, "confidence intervals do not overlap"
    if -delta >= min_delta:
        return True, f"drop of {-delta:.3f} at or above the {min_delta} minimum delta"
    if before_ci is None or after_ci is None:
        return False, f"no confidence interval, and the drop is under the {min_delta} minimum delta"
    return False, "confidence intervals overlap and the drop is under the minimum delta"


def _coverage_regression(
    before_n: int | None, after_n: int | None, *, comparable: bool
) -> tuple[bool, str]:
    """Did this metric stop being able to score samples it used to score?

    This is the regression a mean cannot show, and it can point the wrong way
    entirely: a fault that makes the hardest sample fail outright removes it
    from the metric, and the surviving mean goes *up*.
    """
    if not comparable or before_n is None or after_n is None or before_n <= 0:
        return False, ""
    lost = before_n - after_n
    if lost < MIN_COVERAGE_LOSS:
        return False, ""
    return True, f"coverage: scored {after_n} sample(s), down from {before_n}"


def compare_metric_rows(
    before_metrics: dict[str, Any],
    after_metrics: dict[str, Any],
    *,
    material: float = MATERIAL_DELTA,
    min_delta: float = DEFAULT_MIN_REGRESSION_DELTA,
    coverage_comparable: bool = True,
    stage_order: Sequence[Stage] = (),
) -> dict[str, Any]:
    """Compare two ``summary["metrics"]`` mappings, in dict form.

    ``stage_order`` names your pipeline stages from earliest to latest (plus
    ``evalkit.schema.END_TO_END`` wherever cross-cutting stages should sort).
    When given, a regression is attributed to the *earliest* stage in that
    order with a material drop -- correctness is usually compositional, so
    the biggest number is often the last stage to notice rather than the one
    at fault. Leave it empty to skip localization and just report every
    regressed stage.
    """
    rows: dict[str, dict[str, Any]] = {}
    for name in sorted(set(before_metrics) | set(after_metrics)):
        before = before_metrics.get(name) or {}
        after = after_metrics.get(name) or {}
        b_mean, a_mean = before.get("mean"), after.get("mean")
        b_n, a_n = before.get("n_scored"), after.get("n_scored")
        delta = a_mean - b_mean if b_mean is not None and a_mean is not None else None
        before_ci = Interval.from_dict(before.get("mean_ci"))
        after_ci = Interval.from_dict(after.get("mean_ci"))

        score_hit, score_why = _score_regression(
            delta, before_ci, after_ci, material=material, min_delta=min_delta
        )
        coverage_hit, coverage_why = _coverage_regression(b_n, a_n, comparable=coverage_comparable)
        basis = "; ".join(part for part in (score_why if score_hit else "", coverage_why) if part)

        rows[name] = {
            "stage": (after or before).get("stage"),
            "layer": (after or before).get("layer"),
            "baseline": b_mean,
            "candidate": a_mean,
            "delta": delta,
            "baseline_n": b_n,
            "candidate_n": a_n,
            "coverage_delta": (a_n - b_n) if b_n is not None and a_n is not None else None,
            "baseline_ci": before_ci.to_dict() if before_ci else None,
            "candidate_ci": after_ci.to_dict() if after_ci else None,
            "regression": score_hit or coverage_hit,
            "score_regression": score_hit,
            "coverage_regression": coverage_hit,
            "regression_basis": basis or score_why,
        }

    regressions = {k: v for k, v in rows.items() if v["regression"]}
    # Drops that moved the wrong way but did not clear the bar. Named rather
    # than dropped: a silent filter reads as "nothing regressed".
    suppressed = sorted(
        name for name, row in rows.items() if not row["regression"] and (row["delta"] or 0) <= -material
    )
    scored_regressions = {k: v for k, v in regressions.items() if v["delta"] is not None}
    worst = min(scored_regressions, key=lambda k: scored_regressions[k]["delta"], default=None)

    # A coverage-only regression has no score delta to rank by. It is ranked
    # by the share of coverage lost instead, on the same negative scale, so
    # the two kinds sort together rather than one silently outranking the other.
    def _severity(row: dict[str, Any]) -> float:
        parts = []
        if row["score_regression"] and row["delta"] is not None:
            parts.append(row["delta"])
        if row["coverage_regression"] and row["baseline_n"]:
            parts.append(-(row["baseline_n"] - row["candidate_n"]) / row["baseline_n"])
        return min(parts) if parts else 0.0

    by_stage: dict[str, float] = {}
    for row in regressions.values():
        stage = str(row["stage"])
        by_stage[stage] = min(by_stage.get(stage, 0.0), _severity(row))

    material_stages = set(by_stage)
    if stage_order:
        localized = next((stage for stage in stage_order if stage in material_stages), None)
    else:
        localized = min(material_stages, key=lambda s: by_stage[s], default=None)
    culprits = sorted(
        (name for name, row in regressions.items() if row["stage"] == localized),
        key=lambda name: _severity(regressions[name]),
    )

    return {
        "metrics": rows,
        "worst_metric": worst,
        "localized_stage": localized,
        "localized_metrics": culprits,
        "downstream_stages": sorted(material_stages - {localized} if localized else material_stages),
        "stage_deltas": dict(sorted(by_stage.items(), key=lambda item: item[1])),
        "n_regressions": len(regressions),
        "coverage_regressions": sorted(k for k, v in regressions.items() if v["coverage_regression"]),
        "suppressed_as_noise": suppressed,
        "material_delta": material,
        "min_regression_delta": min_delta,
        "coverage_comparable": coverage_comparable,
        "stage_order_supplied": bool(stage_order),
    }


def compare(
    baseline: EvaluationReport,
    candidate: EvaluationReport,
    material: float = MATERIAL_DELTA,
    *,
    min_delta: float = DEFAULT_MIN_REGRESSION_DELTA,
    stage_order: Sequence[Stage] = (),
) -> dict[str, Any]:
    """Per-metric deltas, and (with ``stage_order``) the stage a regression attributes to.

    A drop only counts once it clears :func:`_score_regression` -- overlapping
    confidence intervals are not evidence of a change -- and a metric that
    lost coverage regresses whether or not its surviving mean fell.
    """
    return compare_metric_rows(
        {name: summary.to_dict() for name, summary in baseline.summaries.items()},
        {name: summary.to_dict() for name, summary in candidate.summaries.items()},
        material=material,
        min_delta=min_delta,
        coverage_comparable=(baseline.counts.get("n_samples") == candidate.counts.get("n_samples")),
        stage_order=stage_order,
    )


__all__ = [
    "MATERIAL_DELTA",
    "MIN_COVERAGE_LOSS",
    "EvaluationReport",
    "EvaluationRunner",
    "aggregate",
    "build_manifest",
    "compare",
    "compare_metric_rows",
    "coverage_gaps",
    "evaluate_gate",
    "sample_counts",
    "token_accounting",
]
