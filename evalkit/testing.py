"""Test doubles for running the framework hermetically -- no model, no network.

Use these in your own test suite to exercise metrics, the runner, and the
gate without paying for a judge call or depending on one being reachable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import BaseModel

from evalkit.judge import JudgeClient


class NullCriteriaStore:
    """A criteria store that pins nothing -- every adaptive rubric regenerates every run."""

    corpus_hash: str = ""

    def get(self, sample_id: str, rubric_key: str) -> list[str] | None:
        return None

    def put(self, sample_id: str, rubric_key: str, criteria: list[str]) -> None:
        return None

    def manifest(self) -> dict[str, Any]:
        return {"criteria_pinned": False}

    def save(self) -> None:
        return None


@dataclass
class InMemoryCriteriaStore:
    """Pins generated adaptive criteria for the lifetime of one process/test."""

    corpus_hash: str = ""
    _store: dict[tuple[str, str], list[str]] = field(default_factory=dict)

    def get(self, sample_id: str, rubric_key: str) -> list[str] | None:
        return self._store.get((sample_id, rubric_key))

    def put(self, sample_id: str, rubric_key: str, criteria: list[str]) -> None:
        self._store[(sample_id, rubric_key)] = list(criteria)

    def manifest(self) -> dict[str, Any]:
        return {"criteria_pinned": True, "n_pinned": len(self._store)}

    def save(self) -> None:
        return None


def scripted_judge_factory(
    script: dict[str, BaseModel] | Callable[[str], BaseModel],
) -> Callable[[type[BaseModel]], Any]:
    """Build a ``judge_factory`` for :class:`~evalkit.judge.JudgeClient` that
    returns pre-written verdicts instead of calling a model.

    ``script`` is either a ``{prompt: verdict}`` dict for exact-match fixtures,
    or a callable ``prompt -> verdict`` for programmatic scripting (e.g.
    "score 4 unless the prompt mentions X"). Every schema shares the same
    script, matching how the real judge is schema-agnostic at the transport
    level.

    Example::

        from evalkit.judge import JudgeClient, RubricVerdict
        from evalkit.testing import scripted_judge_factory

        def always_pass(prompt: str) -> RubricVerdict:
            return RubricVerdict(score=4, reason="scripted pass")

        judge = JudgeClient(judge_factory=scripted_judge_factory(always_pass))
    """

    class _Runnable:
        def invoke(self, prompt: str) -> BaseModel:
            if callable(script):
                return script(prompt)
            if prompt not in script:
                raise KeyError(f"scripted judge has no fixture for prompt: {prompt[:200]!r}...")
            return script[prompt]

    def factory(_schema: type[BaseModel]) -> Any:
        return _Runnable()

    return factory


def scripted_judge(script: dict[str, BaseModel] | Callable[[str], BaseModel], **kwargs: Any) -> JudgeClient:
    """Convenience: a ready-to-use :class:`JudgeClient` backed by ``script``."""
    return JudgeClient(judge_factory=scripted_judge_factory(script), **kwargs)


__all__ = ["InMemoryCriteriaStore", "NullCriteriaStore", "scripted_judge", "scripted_judge_factory"]
