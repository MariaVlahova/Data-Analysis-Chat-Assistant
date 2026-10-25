"""Configuration for an evaluation run.

Defaults are read from the environment so a run is reproducible from a shell,
but every field can be set explicitly (tests should always do this rather than
relying on ambient env vars). Whatever is used ends up in the run manifest --
a published score that cannot name its judge model is not a result.

Nothing here names a project-specific metric. ``gate_floors`` and
``gate_metrics`` start empty; wire in the metrics that matter for your system
when you build your ``EvaluationConfig``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from evalkit.schema import FREE_LAYERS, Layer
from evalkit.statistics import DEFAULT_CONFIDENCE, DEFAULT_RESAMPLES

#: A drop at least this large is called a regression even when the confidence
#: intervals still overlap. Without it, a small corpus could never report one.
DEFAULT_MIN_REGRESSION_DELTA = 0.10

_LAYER_ALIASES: dict[str, frozenset[Layer]] = {
    "all": frozenset(Layer),
    "free": FREE_LAYERS,
    "static": frozenset({Layer.STATIC}),
    "deterministic": FREE_LAYERS,
    "judge": frozenset({Layer.STANDARD, Layer.JUDGE, Layer.ADAPTIVE}),
}


#: How the corpus is partitioned. ``dev`` is what CI and local iteration run
#: against; ``holdout`` is reserved so a score can be checked against samples
#: nobody has been tuning towards. ``all`` runs the union.
CORPUS_SPLITS = frozenset({"all", "dev", "holdout"})


def _parse_floors(raw: str) -> dict[str, float]:
    """``EVAL_GATE_FLOORS`` as a JSON object of metric -> floor."""
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        overrides = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"EVAL_GATE_FLOORS is not valid JSON: {exc}") from exc
    if not isinstance(overrides, dict):
        raise ValueError("EVAL_GATE_FLOORS must be a JSON object of metric -> floor")
    return {str(k): float(v) for k, v in overrides.items()}


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _parse_layers(raw: str) -> frozenset[Layer]:
    raw = (raw or "all").strip().lower()
    if raw in _LAYER_ALIASES:
        return _LAYER_ALIASES[raw]
    wanted = {part.strip() for part in raw.split(",") if part.strip()}
    by_value = {layer.value.lower(): layer for layer in Layer}
    by_name = {layer.name.lower(): layer for layer in Layer}
    resolved: set[Layer] = set()
    for part in wanted:
        layer = by_value.get(part) or by_name.get(part)
        if layer is None:
            raise ValueError(
                f"unknown layer {part!r}; use one of {sorted(_LAYER_ALIASES)} "
                f"or a comma-separated list of {sorted(by_value)}"
            )
        resolved.add(layer)
    if not resolved:
        raise ValueError("EVAL_LAYERS resolved to an empty set")
    return frozenset(resolved)


@dataclass
class EvaluationConfig:
    """Everything that changes what a run measures or where it writes."""

    # -- what to run --
    layers: frozenset[Layer] = field(default_factory=lambda: frozenset(Layer))
    #: Skip judge layers (recorded as PASS_BY_PROXY) for a sample whose
    #: deterministic layers all passed; a sample with any deterministic
    #: failure still gets the judge, since that is the case worth diagnosing.
    gate_judge_on_static: bool = True

    # -- judge --
    judge_provider: str = ""
    judge_model: str = ""
    judge_temperature: float = 0.0
    judge_repeats: int = 1

    #: Independent repeats of the whole system under test, for measuring
    #: pipeline (not judge) variance -- how much the trajectory itself moves
    #: from one run to the next on the same input.
    system_repeats: int = 1

    # -- output --
    output_uri: str = "file://./eval_output"
    #: Replace document/user text in written traces with a salted hash.
    redact_traces: bool = False

    # -- run identity --
    mode: str = "offline"
    fault_id: str = ""
    seed: int = 0
    #: ``all`` | ``dev`` | ``holdout`` -- which slice of the corpus to run.
    corpus_split: str = "all"

    # -- gate --
    gate_threshold: float = 0.85
    #: Metrics the composite gate score is computed over. Empty means "every
    #: metric that scored" -- set this to the deterministic checks that most
    #: directly reflect your change surface.
    gate_metrics: tuple[str, ...] = ()
    #: Per-metric floors that fail the gate on their own, regardless of the
    #: composite. Empty by default -- add the metrics where *any* failure
    #: should block a run (access control, safety, provenance) with floor 1.0.
    gate_floors: dict[str, float] = field(default_factory=dict)

    # -- uncertainty --
    bootstrap_resamples: int = DEFAULT_RESAMPLES
    ci_confidence: float = DEFAULT_CONFIDENCE
    min_regression_delta: float = DEFAULT_MIN_REGRESSION_DELTA

    # -- adaptive criteria --
    #: Where generated L3-adaptive criteria are pinned between runs.
    criteria_path: Path | None = None
    #: Regenerate pinned criteria instead of reusing them. Recorded in the manifest.
    regenerate_criteria: bool = False

    @classmethod
    def from_env(cls, prefix: str = "EVAL_", **overrides) -> EvaluationConfig:
        """Build a config from ``{prefix}LAYERS`` etc., then apply ``overrides``.

        ``prefix`` lets two frameworks sharing one process (or one project
        with two evaluated systems) use disjoint env-var namespaces.
        """
        env = os.environ
        config = cls(
            layers=_parse_layers(env.get(f"{prefix}LAYERS", "all")),
            gate_judge_on_static=_env_flag(f"{prefix}GATE_JUDGE_ON_STATIC", True),
            judge_provider=env.get(f"{prefix}JUDGE_PROVIDER", ""),
            judge_model=env.get(f"{prefix}JUDGE_MODEL", ""),
            judge_temperature=float(env.get(f"{prefix}JUDGE_TEMPERATURE", "0.0")),
            judge_repeats=int(env.get(f"{prefix}JUDGE_REPEATS", "1")),
            system_repeats=int(env.get(f"{prefix}SYSTEM_REPEATS", "1")),
            output_uri=env.get(f"{prefix}OUTPUT_URI", "file://./eval_output"),
            redact_traces=_env_flag(f"{prefix}REDACT_TRACES", False),
            corpus_split=env.get(f"{prefix}CORPUS_SPLIT", "all"),
            gate_threshold=float(env.get(f"{prefix}GATE_THRESHOLD", "0.85")),
            gate_floors=_parse_floors(env.get(f"{prefix}GATE_FLOORS", "")),
            bootstrap_resamples=int(env.get(f"{prefix}BOOTSTRAP_RESAMPLES", str(DEFAULT_RESAMPLES))),
            ci_confidence=float(env.get(f"{prefix}CI_CONFIDENCE", str(DEFAULT_CONFIDENCE))),
            min_regression_delta=float(
                env.get(f"{prefix}MIN_REGRESSION_DELTA", str(DEFAULT_MIN_REGRESSION_DELTA))
            ),
        )
        for key, value in overrides.items():
            if not hasattr(config, key):
                raise TypeError(f"unknown EvaluationConfig field {key!r}")
            setattr(config, key, value)
        config.validate()
        return config

    def validate(self) -> None:
        if self.judge_repeats < 1:
            raise ValueError("judge_repeats must be >= 1")
        if self.system_repeats < 1:
            raise ValueError("system_repeats must be >= 1")
        if not 0.0 <= self.gate_threshold <= 1.0:
            raise ValueError("gate_threshold must be within [0, 1]")
        if not 0.0 <= self.judge_temperature <= 2.0:
            raise ValueError("judge_temperature must be within [0, 2]")
        if not self.layers:
            raise ValueError("at least one layer must be enabled")
        if self.corpus_split not in CORPUS_SPLITS:
            raise ValueError(f"corpus_split must be one of {sorted(CORPUS_SPLITS)}")
        for name, floor in self.gate_floors.items():
            if not 0.0 <= float(floor) <= 1.0:
                raise ValueError(f"gate floor for {name!r} must be within [0, 1]")
        if self.bootstrap_resamples < 0:
            raise ValueError("bootstrap_resamples must be >= 0")
        if not 0.0 < self.ci_confidence < 1.0:
            raise ValueError("ci_confidence must be within (0, 1)")

    @property
    def uses_judge(self) -> bool:
        return bool(self.layers - FREE_LAYERS)

    def enabled(self, layer: Layer) -> bool:
        return layer in self.layers

    def manifest_fields(self) -> dict[str, object]:
        """The subset of config that belongs in a published run manifest."""
        return {
            "mode": self.mode,
            "fault_id": self.fault_id or None,
            "seed": self.seed,
            "corpus_split": self.corpus_split,
            "layers_enabled": sorted(layer.value for layer in self.layers),
            "judge_provider": self.judge_provider or None,
            "judge_model": self.judge_model or None,
            "judge_temperature": self.judge_temperature,
            "judge_repeats": self.judge_repeats,
            "system_repeats": self.system_repeats,
            "gate_threshold": self.gate_threshold,
            "gate_floors": dict(sorted(self.gate_floors.items())),
            "ci_confidence": self.ci_confidence,
            "bootstrap_resamples": self.bootstrap_resamples,
            "min_regression_delta": self.min_regression_delta,
            "redact_traces": self.redact_traces,
            "criteria_pinned": self.criteria_path is not None,
            "criteria_regenerated": self.regenerate_criteria,
        }


__all__ = [
    "CORPUS_SPLITS",
    "DEFAULT_MIN_REGRESSION_DELTA",
    "EvaluationConfig",
]
