"""Uncertainty for the aggregate numbers.

A mean over twenty samples is a point estimate. Printed to three decimals with
nothing beside it, it invites a reader to treat a 0.02 move as a regression --
which, on a corpus this size, it usually is not. Every published mean and pass
rate therefore carries a bootstrap interval and the ``n`` it was computed from.

The percentile bootstrap is used rather than a normal approximation because
neither statistic is normally distributed here: scores pile up at 1.0 and pass
rates are proportions, so a symmetric interval would run past the ends of the
scale. Resampling makes no distributional assumption and handles both.

The resampler is seeded from a constant, so two runs over the same data report
the same interval. An interval that moved on its own would be one more thing a
reader has to discount.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Sequence

DEFAULT_RESAMPLES = 10_000
DEFAULT_CONFIDENCE = 0.95

#: Fixed so an interval is a property of the data, not of when it was computed.
BOOTSTRAP_SEED = 20260101

#: Below this many observations a bootstrap re-draws the same handful of values
#: and reports an interval far narrower than the uncertainty actually present.
MIN_BOOTSTRAP_N = 3


@dataclass(frozen=True)
class Interval:
    """A confidence interval, with everything needed to read it honestly."""

    low: float
    high: float
    confidence: float
    n: int
    resamples: int
    method: str

    @property
    def width(self) -> float:
        return self.high - self.low

    def overlaps(self, other: Interval | None) -> bool:
        """True when the two intervals share any ground.

        Overlap is the conservative test: two overlapping intervals are not
        evidence of a difference, so a regression call built on non-overlap
        under-reports rather than over-reports.
        """
        if other is None:
            return True
        return self.low <= other.high and other.low <= self.high

    def to_dict(self) -> dict[str, Any]:
        return {
            "low": round(self.low, 6),
            "high": round(self.high, 6),
            "confidence": self.confidence,
            "n": self.n,
            "resamples": self.resamples,
            "method": self.method,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> Interval | None:
        """Rebuild an interval from a summary file, or None if it has none.

        Summary files written before intervals existed simply lack the key, and
        a comparison against one of those must degrade to the delta rule rather
        than fail.
        """
        if not isinstance(payload, dict) or payload.get("low") is None:
            return None
        try:
            return cls(
                low=float(payload["low"]),
                high=float(payload["high"]),
                confidence=float(payload.get("confidence", DEFAULT_CONFIDENCE)),
                n=int(payload.get("n", 0)),
                resamples=int(payload.get("resamples", 0)),
                method=str(payload.get("method", "unknown")),
            )
        except (TypeError, ValueError, KeyError):
            return None


def _percentile(ordered: list[float], q: float) -> float:
    """Linear-interpolated percentile of an already-sorted list."""
    if len(ordered) == 1:
        return ordered[0]
    position = q * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[int(position)]
    weight = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


@lru_cache(maxsize=512)
def _resampled_interval(
    ordered: tuple[float, ...], resamples: int, confidence: float, seed: int
) -> Interval:
    """The expensive part, memoized on the sorted value vector.

    The bootstrap distribution of a mean does not depend on input order, so the
    sorted tuple is a sound cache key -- and across a full run most metrics
    share a vector (every sample passing is ``(1.0,) * n``), which is where the
    reuse comes from.
    """
    n = len(ordered)
    rng = random.Random(seed)
    numbers = list(ordered)
    means: list[float] = []
    for _ in range(resamples):
        # random.choices is the C-level path; this is the hot loop of a full run
        # and a Python-level draw per observation is several times slower.
        draw = rng.choices(numbers, k=n)
        means.append(sum(draw) / n)
    means.sort()

    tail = (1.0 - confidence) / 2.0
    return Interval(
        low=_percentile(means, tail),
        high=_percentile(means, 1.0 - tail),
        confidence=confidence,
        n=n,
        resamples=resamples,
        method="percentile bootstrap",
    )


def bootstrap_ci(
    values: Sequence[float],
    *,
    resamples: int = DEFAULT_RESAMPLES,
    confidence: float = DEFAULT_CONFIDENCE,
    seed: int = BOOTSTRAP_SEED,
) -> Interval | None:
    """Percentile bootstrap interval for the mean of `values`.

    Returns ``None`` for an empty input -- a metric that scored nothing has no
    interval, and inventing one would be worse than reporting the gap.

    Below :data:`MIN_BOOTSTRAP_N` observations the interval is reported as the
    observed range with the method named ``"range (n<3)"``. Resampling three
    values ten thousand times produces a tight interval that describes the
    three values rather than the population, and a reader who sees
    ``bootstrap`` on it would over-trust it.
    """
    numbers = [float(v) for v in values if v is not None]
    if not numbers:
        return None

    n = len(numbers)
    if n < MIN_BOOTSTRAP_N:
        return Interval(
            low=min(numbers),
            high=max(numbers),
            confidence=confidence,
            n=n,
            resamples=0,
            method=f"range (n<{MIN_BOOTSTRAP_N})",
        )

    low, high = min(numbers), max(numbers)
    if low == high:
        # Every observation identical: resampling can only ever draw that value,
        # so ten thousand rounds would compute a degenerate interval the long way.
        return Interval(
            low=low,
            high=high,
            confidence=confidence,
            n=n,
            resamples=0,
            method="degenerate (all observations equal)",
        )

    if resamples <= 0:
        return Interval(
            low=low, high=high, confidence=confidence, n=n, resamples=0, method="range (bootstrap off)"
        )

    return _resampled_interval(tuple(sorted(numbers)), resamples, confidence, seed)


def proportion_ci(
    n_success: int,
    n_total: int,
    *,
    resamples: int = DEFAULT_RESAMPLES,
    confidence: float = DEFAULT_CONFIDENCE,
    seed: int = BOOTSTRAP_SEED,
) -> Interval | None:
    """Bootstrap interval for a pass rate.

    The same resampler as the mean, over the 0/1 vector the rate came from, so
    a pass rate and a mean score are never compared across two different
    notions of uncertainty.
    """
    if n_total <= 0:
        return None
    n_success = max(0, min(int(n_success), int(n_total)))
    vector = [1.0] * n_success + [0.0] * (n_total - n_success)
    return bootstrap_ci(vector, resamples=resamples, confidence=confidence, seed=seed)


def separated(before: Interval | None, after: Interval | None) -> bool:
    """True when two intervals do not overlap at all."""
    if before is None or after is None:
        return False
    return not before.overlaps(after)


# --------------------------------------------------------------------------- #
# Agreement
# --------------------------------------------------------------------------- #
def cohens_kappa(a: Sequence[Any], b: Sequence[Any]) -> float | None:
    """Chance-corrected agreement between two raters over paired labels.

    Returns ``None`` when the two sequences differ in length or are empty. When
    both raters used exactly one label the observed and expected agreement are
    both 1.0 and kappa is undefined; that is reported as ``None`` too rather
    than as perfect agreement, which is the reading a 1.0 would invite.
    """
    if len(a) != len(b) or not a:
        return None

    pairs = list(zip(a, b))
    n = len(pairs)
    observed = sum(1 for x, y in pairs if x == y) / n

    labels = {*a, *b}
    expected = 0.0
    for label in labels:
        expected += (sum(1 for x in a if x == label) / n) * (sum(1 for y in b if y == label) / n)

    if math.isclose(expected, 1.0):
        return None
    return (observed - expected) / (1.0 - expected)


def agreement_rate(a: Sequence[Any], b: Sequence[Any]) -> float | None:
    """Raw proportion of matching labels, reported next to kappa.

    Kappa alone is hard to read when one label dominates -- a rater pair can
    agree on 95% of samples and score near zero. Both numbers travel together.
    """
    if len(a) != len(b) or not a:
        return None
    return sum(1 for x, y in zip(a, b) if x == y) / len(a)


__all__ = [
    "BOOTSTRAP_SEED",
    "DEFAULT_CONFIDENCE",
    "DEFAULT_RESAMPLES",
    "Interval",
    "agreement_rate",
    "bootstrap_ci",
    "cohens_kappa",
    "proportion_ci",
    "separated",
]
