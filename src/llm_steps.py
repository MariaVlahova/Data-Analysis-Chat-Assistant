"""The model steps and their default settings, in one place (plan 04).

Both `src/config.py` (to validate the `llm.steps` keys and fill in defaults) and `src/llm` (to route) need them.
`config.py` cannot import the `src.llm` package, so they live here, and this module imports nothing from the project.
"""
from __future__ import annotations

from enum import Enum


class Step(str, Enum):
    INPUT_GUARD = "input_guard"
    PLANNER = "planner"
    SQL_WRITE = "sql_write"
    SQL_REPAIR = "sql_repair"
    EVIDENCE_CHECK = "evidence_check"
    COMPOSER = "composer"
    LAST_REPAIR = "last_repair"
    JUDGE = "judge"


LLM_STEP_NAMES = tuple(step.value for step in Step)

# (temperature, max_output_tokens). Temperature 0 for every step that must be repeatable, about 0.3 for the Composer
# so the tone can vary (impl). A thinking model spends output tokens on its thoughts: raise a cap that proves too small.
STEP_DEFAULTS: dict[Step, tuple[float, int]] = {
    Step.INPUT_GUARD: (0.0, 512),
    Step.PLANNER: (0.0, 1024),
    Step.SQL_WRITE: (0.0, 2048),
    Step.SQL_REPAIR: (0.0, 2048),
    Step.EVIDENCE_CHECK: (0.0, 512),
    Step.COMPOSER: (0.3, 4096),
    Step.LAST_REPAIR: (0.0, 2048),
    Step.JUDGE: (0.0, 2048),
}
