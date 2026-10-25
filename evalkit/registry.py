"""The plugin interface: how you add a metric to this framework.

A metric is a plain function ``(ValidationContext) -> MetricResult | None``.
Register it against a ``MetricRegistry`` with a decorator that declares its
name, stage, focus and which pipelines it applies to, and the runner picks it
up automatically. That is the entire extension mechanism -- there is nothing
else to subclass or configure.

Two registries cover the two kinds of function-based metric:

* **static** (``Layer.STATIC``) -- deterministic, no ground truth needed. Pure
  functions over a session: schema checks, invariants, conservation laws,
  access-control checks. Free and exact.
* **computation** (``Layer.COMPUTATION``) -- deterministic but needs a
  reference: error rates, token-F1, recall@k, numeric-value comparison. Same
  cost profile as static, but a sample with no reference is skipped rather
  than scored.

Judge-graded metrics (Layer.STANDARD / JUDGE / ADAPTIVE) are not function
registries -- they are rubric *text* plus one shared scoring routine, because
"call a judge with this prompt" needs no per-rubric code. See
``evalkit.rubrics`` and ``evalkit.graded`` for that half of the interface.

A registry returns ``None`` from a metric function to mean "does not apply to
this session's pipeline at all" -- pure silence, nothing reported. Returning
``MetricResult.not_applicable(...)`` means "applies in principle, but this
sample's inputs were not available" -- a reported gap. The distinction matters
for coverage reporting: see ``evalkit.runner.coverage_gaps``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable

from evalkit.context import ValidationContext
from evalkit.schema import Focus, Layer, MetricResult, Stage

#: The shape every registered metric function must have.
MetricFn = Callable[[ValidationContext], "MetricResult | None"]


@dataclass
class _Registration:
    fn: MetricFn
    stage: Stage
    pipelines: tuple[str, ...]
    focus: Focus


class MetricRegistry:
    """A pluggable collection of metric functions for one evaluation layer.

    Create one per layer per project (or import the framework's default
    ``STATIC_METRICS`` / ``COMPUTATION_METRICS`` instances and register onto
    those) -- each instance is independent, so two projects, or two unrelated
    metric suites in the same project, never collide over function names.

    Example::

        from evalkit.registry import MetricRegistry
        from evalkit.schema import Focus, Layer

        STATIC_METRICS = MetricRegistry(Layer.STATIC)

        @STATIC_METRICS.register(
            "no_empty_answer", stage="generation", pipelines=("chat",), focus=Focus.CORRECTNESS
        )
        def validate_answer_not_empty(ctx):
            answer = str(ctx.state.get("final_answer") or "")
            return MetricResult.from_bool(
                "no_empty_answer", Layer.STATIC, "generation",
                bool(answer.strip()), "answer is non-empty" if answer.strip() else "answer is empty",
                focus=Focus.CORRECTNESS,
            )
    """

    def __init__(self, layer: Layer) -> None:
        self.layer = layer
        self._metrics: dict[str, _Registration] = {}

    def register(
        self,
        name: str,
        *,
        stage: Stage,
        pipelines: tuple[str, ...],
        focus: Focus,
    ) -> Callable[[MetricFn], MetricFn]:
        """Decorator: register ``fn`` as a metric named ``name``.

        ``stage`` is any string you choose -- see ``evalkit.schema.Stage``.
        ``pipelines`` restricts the metric to sessions whose ``pipeline``
        field matches one of these values; pass every pipeline name your
        project uses if the metric applies everywhere.
        """

        def decorate(fn: MetricFn) -> MetricFn:
            if name in self._metrics:
                raise ValueError(f"metric {name!r} is already registered on this registry")
            self._metrics[name] = _Registration(fn=fn, stage=stage, pipelines=tuple(pipelines), focus=focus)
            fn.metric_name = name  # type: ignore[attr-defined]
            fn.stage = stage  # type: ignore[attr-defined]
            return fn

        return decorate

    def __contains__(self, name: str) -> bool:
        return name in self._metrics

    def __iter__(self):
        return iter(self._metrics)

    def names(self) -> list[str]:
        return list(self._metrics)

    def stage_of(self, name: str) -> Stage:
        return self._metrics[name].stage

    def focus_of(self, name: str) -> Focus:
        return self._metrics[name].focus

    def applicable(self, pipeline: str) -> list[str]:
        """Names registered for ``pipeline``, without running them.

        A PASS_BY_PROXY skip needs a metric's identity precisely because it is
        not going to invoke it.
        """
        return [name for name, reg in self._metrics.items() if pipeline in reg.pipelines]

    def run(
        self, ctx: ValidationContext, only: Iterable[str] | None = None
    ) -> dict[str, MetricResult]:
        """Run every metric that applies to ``ctx.pipeline``.

        A metric function returning ``None`` is silently skipped -- see the
        module docstring for the distinction between that and an explicit
        ``not_applicable()`` result.
        """
        wanted = set(only) if only is not None else None
        results: dict[str, MetricResult] = {}
        for name, reg in self._metrics.items():
            if wanted is not None and name not in wanted:
                continue
            if ctx.pipeline not in reg.pipelines:
                continue
            result = reg.fn(ctx)
            if result is not None:
                results[name] = result
        return results


def result_helpers(
    name: str, layer: Layer, stage: Stage, focus: Focus
) -> tuple[Callable, Callable, Callable]:
    """Three small constructors pre-bound to one metric's name/layer/stage/focus.

    Cuts the boilerplate in a metric body down to the interesting part.
    Purely a convenience -- you can always call ``MetricResult.from_bool`` etc.
    directly, and nothing here reads from a registry, so it works regardless
    of whether you call it before or after ``@registry.register(...)``.

    Example::

        ok, na, not_exercised = result_helpers(
            "no_empty_answer", Layer.STATIC, "generation", Focus.CORRECTNESS
        )

        @STATIC_METRICS.register("no_empty_answer", stage="generation",
                                  pipelines=("chat",), focus=Focus.CORRECTNESS)
        def validate(ctx):
            answer = str(ctx.state.get("final_answer") or "")
            if not answer.strip():
                return ok(False, "answer is empty")
            return ok(True, "answer is non-empty")
    """

    def ok(passed: bool, reason: str, **details) -> MetricResult:
        return MetricResult.from_bool(name, layer, stage, passed, reason, focus=focus, **details)

    def na(reason: str, **details) -> MetricResult:
        return MetricResult.not_applicable(name, layer, stage, reason, focus=focus, **details)

    def not_exercised(precondition: str, reason: str, **details) -> MetricResult:
        return MetricResult.not_exercised(name, layer, stage, precondition, reason, focus=focus, **details)

    return ok, na, not_exercised


#: Default registries, ready to import and register onto. Using your own
#: ``MetricRegistry()`` instances instead is equally supported -- these exist
#: purely so a small project can skip the one extra line of setup.
STATIC_METRICS = MetricRegistry(Layer.STATIC)
COMPUTATION_METRICS = MetricRegistry(Layer.COMPUTATION)


__all__ = [
    "COMPUTATION_METRICS",
    "STATIC_METRICS",
    "MetricFn",
    "MetricRegistry",
    "result_helpers",
]
