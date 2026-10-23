"""Test seams for the model (plan 04): no test or evaluation run needs a network.

- `FakeGateway` replaces the whole gateway. Queue what each step should answer; the graph tests of plan 07 and the
  evaluation runs of plan 09 use it. It still charges the turn budget and counts the call in the turn's metrics, so
  call counts and budgets in those tests are honest.
- `FakeGenaiClient` replaces only the Google SDK client under the real `LLMGateway`, so a test exercises the real
  retry, breaker, rate-limit and fallback code with scripted replies and failures (also with a seeded `Chaos`).
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel, ValidationError

from src.errors import AgentError, ErrorKind
from src.llm.budget import TurnBudget
from src.llm.gateway import LLMResult
from src.llm.routing import Step
from src.observability import span, turn_metrics

FAKE_MODEL = "fake-model"


# FakeGateway ------------------------------------------------------------------------------------

@dataclass(frozen=True)
class FakeCall:
    step: Step
    system: str
    contents: Any
    schema: type[BaseModel] | None
    temperature: float | None


class FakeGateway:
    """Returns queued replies by step. A reply is a pydantic object, a dict or a JSON string (checked against the schema
    the caller passes), plain text, an exception to raise, or a function of the `FakeCall` returning any of those."""

    def __init__(self) -> None:
        self._queues: dict[Step, deque] = {}
        self._always: dict[Step, Any] = {}
        self.calls: list[FakeCall] = []

    def queue(self, step: Step, *replies: Any) -> "FakeGateway":
        self._queues.setdefault(step, deque()).extend(replies)
        return self

    def always(self, step: Step, reply: Any) -> "FakeGateway":
        """Answer every call of `step` that has no queued reply."""
        self._always[step] = reply
        return self

    def calls_for(self, step: Step) -> list[FakeCall]:
        return [call for call in self.calls if call.step is step]

    def parallel_limit(self, requested: int) -> int:
        return max(1, requested)

    def generate(
        self,
        step: Step,
        *,
        system: str,
        contents: "str | list",
        schema: type[BaseModel] | None = None,
        budget: TurnBudget,
        temperature: float | None = None,
        prompt: Mapping[str, Any] | None = None,
    ) -> LLMResult:
        call = FakeCall(Step(step), system, contents, schema, temperature)
        self.calls.append(call)
        budget.charge_call()
        with span("llm.call", "llm", step=call.step.value, model=FAKE_MODEL, attempt=1, fallback_used=False):
            turn_metrics().add_llm_call(FAKE_MODEL, prompt_version=(prompt or {}).get("prompt_version"))
            reply = self._next(call)
            if callable(reply) and not isinstance(reply, (type, BaseException)):
                reply = reply(call)
            if isinstance(reply, type) and issubclass(reply, BaseException):
                reply = reply()
            if isinstance(reply, BaseException):
                raise reply
            return self._result(reply, schema)

    def _next(self, call: FakeCall) -> Any:
        queue = self._queues.get(call.step)
        if queue:
            return queue.popleft()
        if call.step in self._always:
            return self._always[call.step]
        raise AssertionError(f"FakeGateway has no scripted reply for step {call.step.value!r}")

    @staticmethod
    def _result(reply: Any, schema: type[BaseModel] | None) -> LLMResult:
        parsed: BaseModel | None = None
        try:
            if isinstance(reply, BaseModel):
                parsed, text = reply, reply.model_dump_json()
            elif isinstance(reply, dict):
                parsed = schema.model_validate(reply) if schema else None
                text = schema.model_validate(reply).model_dump_json() if schema else repr(reply)
            else:
                text = str(reply)
                parsed = schema.model_validate_json(text) if schema else None
        except ValidationError:
            raise AgentError(ErrorKind.LLM_BAD_OUTPUT, detail="the scripted reply did not match the schema") from None
        return LLMResult(parsed=parsed, text=text, model_used=FAKE_MODEL, tokens_in=0, tokens_out=0, attempts=1, fallback_used=False)


# FakeGenaiClient --------------------------------------------------------------------------------

def sdk_error(code: int, status: str = "", *, retry_delay_s: float | None = None) -> genai_errors.APIError:
    """A real SDK error: `ClientError` below 500, `ServerError` from 500. `retry_delay_s` adds the RetryInfo a 429 can carry."""
    body: dict[str, Any] = {"code": code, "message": "scripted failure", "status": status}
    if retry_delay_s is not None:
        body["details"] = [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": f"{retry_delay_s:g}s"}]
    cls = genai_errors.ServerError if code >= 500 else genai_errors.ClientError
    return cls(code, {"error": body})


def rate_limited(retry_delay_s: float | None = None) -> genai_errors.APIError:
    return sdk_error(429, "RESOURCE_EXHAUSTED", retry_delay_s=retry_delay_s)


def unavailable() -> genai_errors.APIError:
    return sdk_error(503, "UNAVAILABLE")


def reply_for(
    text: str | None, *, tokens_in: int = 10, tokens_out: int = 5, thoughts: int = 0,
    finish: str = "STOP", block_reason: str | None = None,
) -> genai_types.GenerateContentResponse:
    """A response shaped like the SDK's. `text=None` gives a reply with no candidate (a blocked prompt)."""
    usage = genai_types.GenerateContentResponseUsageMetadata(
        prompt_token_count=tokens_in, candidates_token_count=tokens_out, thoughts_token_count=thoughts or None
    )
    feedback = genai_types.GenerateContentResponsePromptFeedback(block_reason=block_reason) if block_reason else None
    candidates = None
    if text is not None:
        content = genai_types.Content(role="model", parts=[genai_types.Part(text=text)]) if text else None
        candidates = [genai_types.Candidate(content=content, finish_reason=finish)]
    return genai_types.GenerateContentResponse(candidates=candidates, usage_metadata=usage, prompt_feedback=feedback)


@dataclass
class _Rule:
    outcome: Any  # a response, or an exception to raise
    model: str | None  # None: any model
    times: int | None  # None: forever


@dataclass(frozen=True)
class FakeRequest:
    model: str
    system: str
    contents: Any
    config: genai_types.GenerateContentConfig


class _FakeModels:
    def __init__(self, owner: "FakeGenaiClient") -> None:
        self._owner = owner

    def generate_content(self, *, model: str, contents: Any, config: genai_types.GenerateContentConfig):
        owner = self._owner
        owner.requests.append(FakeRequest(model, str(config.system_instruction or ""), contents, config))
        for rule in owner._rules:
            if rule.model in (None, model) and (rule.times is None or rule.times > 0):
                if rule.times is not None:
                    rule.times -= 1
                if isinstance(rule.outcome, BaseException):
                    raise rule.outcome
                return rule.outcome
        raise AssertionError(f"FakeGenaiClient has no scripted outcome for model {model!r}")

    def list(self):
        return [genai_types.Model(name=f"models/{name}") for name in self._owner.model_ids]


class FakeGenaiClient:
    """What `LLMGateway` needs from `genai.Client`: `models.generate_content` and `models.list`. Outcomes are matched
    in the order they were added; the first one that applies is used (and counted down when it has `times`)."""

    def __init__(self, model_ids: Iterable[str] = ()) -> None:
        self.model_ids = list(model_ids)
        self.requests: list[FakeRequest] = []
        self._rules: list[_Rule] = []
        self.models = _FakeModels(self)

    def reply(self, text: str, *, model: str | None = None, times: int | None = 1, **options: Any) -> "FakeGenaiClient":
        return self.respond(reply_for(text, **options), model=model, times=times)

    def respond(self, response: Any, *, model: str | None = None, times: int | None = 1) -> "FakeGenaiClient":
        self._rules.append(_Rule(response, model, times))
        return self

    def fail(self, error: BaseException, *, model: str | None = None, times: int | None = 1) -> "FakeGenaiClient":
        self._rules.append(_Rule(error, model, times))
        return self

    def requests_to(self, model: str) -> list[FakeRequest]:
        return [request for request in self.requests if request.model == model]


class FakeClock:
    """A clock whose `sleep` moves it forward, so a test of waits and cooldowns takes no real time."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds
