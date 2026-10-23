"""Which model answers which step, and what to try next (plan 04, decision 3.2).

Model IDs are config values (`llm.models`), never hard-coded here. Each step has a chain: the model it prefers, then
its sibling. Production would put Vertex AI Gemini after the sibling; the chain is a list, so that is a config change
(design-only, not built).
"""
from __future__ import annotations

from src.config import LlmConfig, StepSettings
from src.llm_steps import Step

__all__ = ["Router", "Step"]


# Tiers are the `llm.models` keys. Cheap classification steps start on Flash-Lite, the steps that write start on Flash,
# and each falls back to the other one (prototype, section 8).
_LIGHT_FIRST = ("light", "main")
_MAIN_FIRST = ("main", "light")
_PRO_ONLY = ("pro",)  # no fallback: the preview Pro model is pinned for the last repair and the offline judge (S13)

_TIERS: dict[Step, tuple[str, ...]] = {
    Step.INPUT_GUARD: _LIGHT_FIRST,
    Step.EVIDENCE_CHECK: _LIGHT_FIRST,
    Step.PLANNER: _MAIN_FIRST,
    Step.SQL_WRITE: _MAIN_FIRST,
    Step.SQL_REPAIR: _MAIN_FIRST,
    Step.COMPOSER: _MAIN_FIRST,
    Step.LAST_REPAIR: _PRO_ONLY,
    Step.JUDGE: _PRO_ONLY,
}


class Router:
    def __init__(self, llm: LlmConfig) -> None:
        self._llm = llm

    def chain(self, step: Step) -> tuple[str, ...]:
        """Model IDs to try, in order. With `llm.pro_enabled` off the Pro steps use the Flash chain, so Pro is skipped
        and never called (decision 3.2: Pro off in the prototype)."""
        tiers = _TIERS[step]
        if "pro" in tiers and not self._llm.pro_enabled:
            tiers = _MAIN_FIRST
        models: list[str] = []
        for tier in tiers:
            model = getattr(self._llm.models, tier)
            if model not in models:  # light and main may be set to the same ID
                models.append(model)
        return tuple(models)

    def settings(self, step: Step) -> StepSettings:
        return self._llm.steps[step.value]

    def models_in_use(self) -> list[str]:
        """Every model ID some step can call with this config, for the `doctor --live` check against `models.list()`."""
        seen: list[str] = []
        for step in Step:
            for model in self.chain(step):
                if model not in seen:
                    seen.append(model)
        return seen
