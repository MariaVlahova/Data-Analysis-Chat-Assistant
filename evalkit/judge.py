"""Judge client for the model-graded layers.

Provider-neutral by construction: this module never imports a specific
LLM SDK or app. You supply a ``judge_factory`` -- a callable that takes a
Pydantic schema and returns something with an ``.invoke(prompt) -> schema
instance`` method (exactly what LangChain's ``with_structured_output``, or a
thin wrapper around the OpenAI/Anthropic SDKs, gives you).

Judge scoring is stochastic and costly, which drives three behaviours here:
repeated sampling so the variance is measured rather than assumed, a cache so
an identical prompt is not paid for twice, and a call counter so a run that
claims to be free can be shown to be.
"""

from __future__ import annotations

import hashlib
import logging
import statistics
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

#: Judges rate 1 (poor) to 4 (excellent); null means the rubric did not apply.
SCORE_MIN, SCORE_MAX = 1, 4


class RubricVerdict(BaseModel):
    """A judge's rating against one rubric."""

    score: int | None = Field(
        default=None,
        description=(
            "1 (poor) to 4 (excellent), or null when the rubric does not apply to this sample. "
            "Do not guess a middle score to avoid deciding -- null is the correct answer when "
            "the behaviour being judged never had a chance to occur."
        ),
    )
    reason: str = Field(description="One or two sentences justifying the score, citing the evidence.")


class AdaptiveCriteria(BaseModel):
    """Per-sample criteria the judge generates before scoring (Layer.ADAPTIVE)."""

    criteria: list[str] = Field(
        default_factory=list,
        description=(
            "The specific, checkable expectations this sample should meet. Empty when the "
            "rubric does not apply."
        ),
    )
    applicable: bool = Field(default=True, description="False when the rubric does not apply here.")


@dataclass
class JudgeCall:
    """One judge invocation, retained so a report can show its own cost."""

    rubric: str
    schema: str
    repeats: int
    cached: bool
    prompt_chars: int = 0
    response_chars: int = 0


class JudgeNotConfigured(RuntimeError):
    """``JudgeClient.invoke`` was called with no ``judge_factory`` supplied."""


@dataclass
class JudgeClient:
    """Structured judge with repeats, caching and accounting.

    ``judge_factory(schema) -> Runnable`` is required for any run that scores
    a judge-graded layer; pass a scripted stand-in (see ``evalkit.testing``)
    for hermetic tests.
    """

    repeats: int = 1
    judge_factory: Callable[[type[BaseModel]], Any] | None = None
    calls: list[JudgeCall] = field(default_factory=list)
    _cache: dict[str, Any] = field(default_factory=dict, repr=False)
    _runnables: dict[type, Any] = field(default_factory=dict, repr=False)

    @property
    def call_count(self) -> int:
        """Model invocations actually made -- cache hits and skips excluded."""
        return sum(call.repeats for call in self.calls if not call.cached)

    def estimated_tokens(self, chars_per_token: int = 4) -> dict[str, int]:
        """Tokens the judge layers consumed, estimated from prompt/response length.

        A structured-output call typically returns a parsed model, not a raw
        message, so provider usage rarely survives to be counted. Length is
        the honest fallback and every number derived from it is labelled an
        estimate -- an unlabelled estimate in a cost table is worse than none.
        """
        input_tokens = sum(
            max(0, call.prompt_chars) // chars_per_token * call.repeats
            for call in self.calls
            if not call.cached
        )
        output_tokens = sum(
            max(0, call.response_chars) // chars_per_token * call.repeats
            for call in self.calls
            if not call.cached
        )
        if not input_tokens and not output_tokens:
            return {}
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }

    def reset(self) -> None:
        self.calls.clear()
        self._cache.clear()

    def _runnable(self, schema: type[BaseModel]):
        if schema not in self._runnables:
            if self.judge_factory is None:
                raise JudgeNotConfigured(
                    "JudgeClient.judge_factory is not set; pass one that builds a structured "
                    "runnable for `schema`, e.g. a LangChain "
                    "`llm.with_structured_output(schema)`, or use a scripted judge from "
                    "evalkit.testing for tests that need no model."
                )
            self._runnables[schema] = self.judge_factory(schema)
        return self._runnables[schema]

    @staticmethod
    def _key(rubric: str, schema: type[BaseModel], prompt: str) -> str:
        digest = hashlib.sha256(f"{rubric}\x00{schema.__name__}\x00{prompt}".encode()).hexdigest()
        return digest[:32]

    def invoke(
        self, rubric: str, prompt: str, schema: type[BaseModel] = RubricVerdict, repeats: int | None = None
    ) -> tuple[Any, list[Any]]:
        """Score `prompt`, returning the representative verdict and every sample.

        With repeats > 1 the median score wins, so a single outlier cannot
        decide a verdict, and the individual samples travel with the result so
        a report can show how unstable the judge was.
        """
        key = self._key(rubric, schema, prompt)
        if key in self._cache:
            self.calls.append(JudgeCall(rubric, schema.__name__, 0, cached=True))
            return self._cache[key]

        n = repeats if repeats is not None else self.repeats
        runnable = self._runnable(schema)
        samples: list[Any] = []
        for attempt in range(max(n, 1)):
            try:
                samples.append(runnable.invoke(prompt))
            except Exception:
                # A judge that dies must not be silently read as approval: the
                # sample is dropped and the caller sees a short sample list.
                logger.exception("Judge %r failed on attempt %d", rubric, attempt + 1)

        self.calls.append(
            JudgeCall(
                rubric,
                schema.__name__,
                max(n, 1),
                cached=False,
                prompt_chars=len(prompt),
                response_chars=(
                    sum(len(s.model_dump_json()) for s in samples) // len(samples) if samples else 0
                ),
            )
        )
        if not samples:
            result = (None, [])
        else:
            result = (_representative(samples), samples)
        self._cache[key] = result
        return result


def _representative(samples: list[Any]) -> Any:
    """The median-scoring sample; ties resolve to the earlier one."""
    scored = [s for s in samples if getattr(s, "score", None) is not None]
    if not scored:
        return samples[0]
    median = statistics.median(sorted(s.score for s in scored))
    return min(scored, key=lambda s: (abs(s.score - median), scored.index(s)))


def score_variance(samples: list[Any]) -> float | None:
    """Population variance of the numeric scores, or None below two samples."""
    scores = [float(s.score) for s in samples if getattr(s, "score", None) is not None]
    if len(scores) < 2:
        return None
    return statistics.pvariance(scores)


def normalized_scores(samples: list[Any]) -> list[float]:
    """Every sample's score mapped onto 0-1, for the repeat-variance report."""
    return [
        (float(s.score) - SCORE_MIN) / (SCORE_MAX - SCORE_MIN)
        for s in samples
        if getattr(s, "score", None) is not None
    ]


__all__ = [
    "SCORE_MAX",
    "SCORE_MIN",
    "AdaptiveCriteria",
    "JudgeCall",
    "JudgeClient",
    "JudgeNotConfigured",
    "RubricVerdict",
    "normalized_scores",
    "score_variance",
]
