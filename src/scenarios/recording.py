"""A pass-through around the model gateway that keeps what each call was asked and how it ended.

The traces keep a hash of the system prompt and the counts of a call, not the text. With `--debug-prompts` the exchange file beside the trace
(`traces/<id>.exchange.jsonl`, plan 12) keeps each step's system text, its contents with every guarded result replaced by a line that names it, and
its reply, masked, with no result row and no answer the Output Guard held back. To say that no personal data reached a prompt, the scenario
runner needs the text of every prompt as it was sent, rows included, so it wraps the gateway with this. The graph sees a gateway that behaves
exactly like the one inside it. This record is the runner's own, for its checks: it is not part of the traces or of the exchange file.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Mapping

from pydantic import BaseModel

from src.errors import AgentError
from src.llm.budget import TurnBudget
from src.llm.routing import Step


@dataclass(frozen=True)
class RecordedCall:
    step: str
    system: str
    contents: str
    seconds: float
    error_kind: str | None = None  # the AgentError kind when the call failed
    tokens_in: int = 0
    tokens_out: int = 0

    @property
    def text(self) -> str:
        return f"{self.system}\n{self.contents}"


class RecordingGateway:
    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls: list[RecordedCall] = []

    def __getattr__(self, name: str) -> Any:  # `router`, `parallel_limit` ...: whatever the inner gateway offers
        return getattr(self._inner, name)

    def generate(
        self, step: Step, *, system: str, contents: "str | list", schema: type[BaseModel] | None = None, budget: TurnBudget,
        temperature: float | None = None, prompt: Mapping[str, Any] | None = None,
    ):
        started = time.perf_counter()
        text = contents if isinstance(contents, str) else "\n\n".join(str(part) for part in contents)
        try:
            result = self._inner.generate(
                step, system=system, contents=contents, schema=schema, budget=budget, temperature=temperature, prompt=prompt,
            )
        except AgentError as exc:
            self.calls.append(RecordedCall(Step(step).value, system, text, time.perf_counter() - started, exc.kind.value))
            raise
        self.calls.append(RecordedCall(
            Step(step).value, system, text, time.perf_counter() - started, None,
            getattr(result, "tokens_in", 0), getattr(result, "tokens_out", 0),
        ))
        return result
