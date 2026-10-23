"""The one gateway to Gemini (plan 04, F35, NFR4, NFR9, S3, S12).

Every model call in the agent goes through `LLMGateway.generate()`. It routes the step to a chain of models and, per
attempt: checks the breaker, checks and charges the turn budget, waits for a rate-limit token, sends, reads the reply
and records tokens. A 429, a 5xx or a timeout is retried with a growing, jittered wait; when a model gives up the next
one in the chain is asked. All retrying happens here (the SDK's own retries are off), so the budget counts every
outbound call and a retry can never hide a cost (brief R5).

Errors leave as typed `AgentError`s: LLM_RATE_LIMITED, LLM_UNAVAILABLE, LLM_BAD_OUTPUT, LLM_BLOCKED, CIRCUIT_OPEN,
BUDGET_EXHAUSTED, TURN_TIMEOUT. Nothing here logs or stores an exception's text, a prompt or a reply: a model error
can quote the request. The details are our own words plus the HTTP code and Google's status word.
"""
from __future__ import annotations

import logging
import random
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

import httpx
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel, ValidationError

from src.config import Settings
from src.errors import AgentError, ErrorKind, crash_summary
from src.llm.breaker import Breakers, State
from src.llm.budget import TurnBudget, call_timeout, fits_in_turn
from src.llm.chaos import Chaos, MalformedReply
from src.llm.limiter import RateLimiter
from src.llm.routing import Router, Step
from src.observability import event, prompt_attrs, span, turn_metrics

log = logging.getLogger(__name__)

SETUP_MESSAGE = (
    "I can't use the language model because of a setup problem on our side. "
    "Please tell an administrator and share the trace ID."
)
MAX_RETRY_AFTER_S = 3600.0
TRUNCATION_REASK_FACTOR = 2  # a reply that hit the output cap is asked again with this many times the cap ...
MAX_OUTPUT_TOKENS_CEILING = 16_384  # ... up to this
REASK_NOTE = (
    "Your previous reply could not be read. Reply again with only the JSON object that matches the required schema."
)
CostHook = Callable[[str, int, int], None]  # (model, tokens in, tokens out): production would add up a daily budget (NFR8)

# A reply that ended for one of these reasons was refused by the model's safety filters (the Input Guard treats it as a block, S10).
_BLOCKING_FINISH = frozenset({
    "SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "RECITATION", "LANGUAGE",
    "IMAGE_SAFETY", "IMAGE_PROHIBITED_CONTENT", "IMAGE_RECITATION",
})
_STATUS_WORD = re.compile(r"[A-Z_]{1,40}")  # Google's status words (RESOURCE_EXHAUSTED); anything else is not quoted
_DURATION = re.compile(r"(\d+(?:\.\d+)?)s")
_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True)
class LLMResult:
    parsed: BaseModel | None
    text: str
    model_used: str
    tokens_in: int  # added up over every attempt of the call, so a re-ask is paid for here too
    tokens_out: int  # includes the model's thinking tokens: they are billed as output
    attempts: int  # outbound attempts, failed ones included
    fallback_used: bool


class Gateway(Protocol):
    """What the agent needs. `LLMGateway` and `fake.FakeGateway` both fit."""

    def generate(
        self, step: Step, *, system: str, contents: "str | list", schema: type[BaseModel] | None = None,
        budget: TurnBudget, temperature: float | None = None, prompt: Mapping[str, Any] | None = None,
    ) -> LLMResult: ...


@dataclass
class _Reply:
    text: str
    tokens_in: int = 0
    tokens_out: int = 0
    finish: str | None = None
    blocked: str | None = None  # why the model refused, when it did


@dataclass
class _Failure:
    """A failed attempt, in the terms the retry loop needs."""

    model: str
    reason: str  # short, for the span and the final detail: "rate_limited", "server_error", "timeout", ...
    kind: ErrorKind  # what the caller sees if every model fails like this
    detail: str
    retryable: bool = True  # ask the same model again (else: go to the sibling)
    counts: bool = True  # counts against the breaker
    terminal: bool = False  # raise now: asking another model would only repeat it
    message: str | None = None  # a setup problem has its own words and is not worth retrying
    retry_after: float | None = None

    def to_error(self, detail: str | None = None) -> AgentError:
        return AgentError(
            self.kind, self.message, detail=detail or self.detail, retryable=False if self.message else None
        )


_REASK = object()


@dataclass
class _Run:
    """Everything one `generate()` call tracks across its attempts."""

    step: Step
    system: str
    contents: Any
    schema: type[BaseModel] | None
    budget: TurnBudget
    temperature: float
    max_output_tokens: int
    prompt: dict[str, Any]
    outbound: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    reasked: bool = False
    waited: float = 0.0  # the wait before the attempt that is about to start
    failures: list[_Failure] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


class LLMGateway:
    def __init__(
        self,
        settings: Settings,
        *,
        client: Any = None,
        chaos: Chaos | None = None,
        limiter: RateLimiter | None = None,
        breakers: Breakers | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
        cost_hook: CostHook | None = None,
    ) -> None:
        """`client` is a `google.genai.Client` (or a stand-in with `models.generate_content` and `models.list`). Without
        one, the client is made on first use from `GEMINI_API_KEY`, so building a gateway never needs the key."""
        self._settings = settings
        self._llm = settings.llm
        self._router = Router(self._llm)
        self._sdk = client
        self._chaos = chaos
        self._limiter = limiter or RateLimiter.from_config(self._llm, clock=clock, sleep=sleep)
        self._breakers = breakers or Breakers(self._llm.breaker, clock)
        self._clock = clock
        self._sleep = sleep
        self._jitter = jitter
        self._cost_hook = cost_hook
        self._lock = threading.Lock()

    @property
    def router(self) -> Router:
        return self._router

    @property
    def client(self) -> Any:
        """The SDK client, made on first use. The SDK's own retries are off: all retrying is in this class."""
        if self._sdk is None:
            with self._lock:
                if self._sdk is None:
                    key = self._settings.secrets.gemini_api_key
                    if key is None:
                        raise AgentError(
                            ErrorKind.LLM_UNAVAILABLE, SETUP_MESSAGE, detail="GEMINI_API_KEY is not set", retryable=False
                        )
                    from google import genai

                    self._sdk = genai.Client(api_key=key.get_secret_value(), http_options=self.http_options())
        return self._sdk

    def http_options(self, timeout_s: float | None = None) -> Any:
        """HTTP options for the SDK: a timeout (so a call that has none of its own, such as `models.list()`, cannot hang)
        and no SDK retries. `retry_options` only exists in newer SDK versions, where it must be set to one attempt; in
        older ones the SDK never retried."""
        options: dict[str, Any] = {"timeout": int((timeout_s or self._llm.request_timeout_s) * 1000)}
        retry_options = getattr(genai_types, "HttpRetryOptions", None)
        if retry_options is not None:
            options["retry_options"] = retry_options(attempts=1)
        return genai_types.HttpOptions(**options)

    def available_models(self) -> set[str]:
        """Model IDs the key can use, from `models.list()` (A14: checked by `doctor --live`)."""
        return {(getattr(model, "name", "") or "").removeprefix("models/") for model in self.client.models.list()}

    def parallel_limit(self, requested: int) -> int:
        """`loop.parallel_subqueries` capped by the rate limiter's burst size (S12). Always at least 1."""
        capacity = min(self._limiter.capacity(model) for model in self._router.chain(Step.SQL_WRITE))
        return max(1, min(requested, capacity))

    # the call ------------------------------------------------------------------------------------

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
        """One model answer for `step`. `prompt` is the span attributes of the prompt (`prompt_attrs(...)`); without
        it the span carries a hash of `system` only. Raises the typed errors listed in the module docstring."""
        chain = self._router.chain(step)
        settings = self._router.settings(step)
        run = _Run(
            step=step, system=system, contents=contents, schema=schema, budget=budget,
            temperature=settings.temperature if temperature is None else temperature,
            max_output_tokens=settings.max_output_tokens,
            prompt=dict(prompt) if prompt else prompt_attrs(step.value, "unversioned", system),
        )
        for rank, model in enumerate(chain):
            result = self._ask_model(run, model, rank)
            if result is not None:
                return result
        raise self._chain_error(run, chain)

    def _ask_model(self, run: _Run, model: str, rank: int) -> LLMResult | None:
        """Ask one model, retrying transient failures. None means this model gave up and the next one is asked."""
        breaker = self._breakers.get(model)
        retry = self._llm.retry
        tries = 0
        while True:
            if not breaker.allow():
                event("llm.skipped", model=model, reason="circuit_open")
                run.skipped.append(model)
                return None
            try:
                run.budget.check_calls()  # first: a call that cannot be paid for must not wait for a rate-limit slot
                self._limiter.acquire(model, run.budget)
                run.budget.charge_call()
            except AgentError as exc:
                breaker.release()
                if exc.kind is ErrorKind.LLM_RATE_LIMITED:  # this model is saturated and the turn cannot wait: try the sibling
                    event("llm.skipped", model=model, reason="rate_limited")
                    run.failures.append(_Failure(model, "rate_limited_locally", exc.kind, exc.detail, retryable=False, counts=False))
                    return None
                raise  # BUDGET_EXHAUSTED and TURN_TIMEOUT end the whole turn
            except BaseException:
                breaker.release()
                raise

            outcome = self._attempt(run, model, rank, breaker)
            if isinstance(outcome, LLMResult):
                return outcome
            if outcome is _REASK:
                continue
            run.failures.append(outcome)
            if not outcome.retryable:
                return None
            tries += 1
            if tries >= retry.attempts or breaker.state is State.OPEN:
                return None
            wait = self._backoff(tries, outcome.retry_after)
            if not fits_in_turn(wait, run.budget):  # too little of the turn would be left for the call: let the sibling try now
                return None
            run.waited = wait
            self._sleep(wait)

    def _backoff(self, tries: int, retry_after: float | None) -> float:
        """Full jitter: a random wait between 0 and min(cap, base x 2^(tries-1)). A delay the server asked for is a floor."""
        retry = self._llm.retry
        wait = self._jitter() * min(retry.cap_s, retry.base_s * 2 ** (tries - 1))
        return max(wait, retry_after or 0.0)

    def _attempt(self, run: _Run, model: str, rank: int, breaker) -> "LLMResult | _Failure | object":
        """One outbound call, as one `llm.call` span. Returns the result, a retryable `_Failure` or `_REASK`."""
        run.outbound += 1
        fallback = rank > 0
        with span(
            "llm.call", "llm", step=run.step.value, model=model, attempt=run.outbound, fallback_used=fallback,
            temperature=run.temperature, max_output_tokens=run.max_output_tokens, **run.prompt,
        ) as sp:
            if run.waited:
                sp.set(waited_before_s=round(run.waited, 2))
                run.waited = 0.0
            try:
                reply = self._send(run, model, sp)
            except Exception as exc:  # noqa: BLE001 - classified below; anything that is not a Gemini or network problem is re-raised
                failure = self._classify(exc, model)
                if failure is None:
                    breaker.release()
                    raise
                self._count(run, model, 0, 0, fallback)
                if failure.counts:
                    was_open = breaker.state is State.OPEN
                    breaker.record_failure()
                    if not was_open and breaker.state is State.OPEN:
                        sp.set(breaker_opened=True)
                        log.warning("circuit opened for model %s after %s", model, failure.reason)
                else:
                    breaker.record_success()  # the service answered, so it is up
                sp.set(retry_reason=failure.reason)
                sp.finish("error", failure.kind.value)
                if failure.terminal:
                    raise failure.to_error() from None
                return failure

            breaker.record_success()  # a reply came back: the service is up, whatever the reply says
            run.tokens_in += reply.tokens_in
            run.tokens_out += reply.tokens_out
            self._count(run, model, reply.tokens_in, reply.tokens_out, fallback)
            sp.set(tokens_in=reply.tokens_in, tokens_out=reply.tokens_out, finish_reason=reply.finish)

            if reply.blocked is not None:  # the model's safety filters refused: asking again would only be refused again
                sp.set(blocked=reply.blocked)
                sp.finish("error", ErrorKind.LLM_BLOCKED.value)
                raise AgentError(ErrorKind.LLM_BLOCKED, detail=reply.blocked)

            truncated = reply.finish == "MAX_TOKENS"
            if not reply.text.strip():
                # No text and no refusal: a glitch, or the output cap spent on thinking. Not a safety block, so ask again.
                return self._bad_output(
                    run, sp, "the reply was empty" + (" and ended at the output token limit" if truncated else ""), truncated
                )
            parsed, why = _parse(run.schema, reply.text)
            if why is not None and reply.finish not in (None, "STOP"):
                why += f" (finish reason {reply.finish})"
            elif why is None and truncated and run.schema is None:
                why = "the reply was cut off at the output token limit"  # free text has no schema to fail, but half an answer is no answer
            if why is not None:
                return self._bad_output(run, sp, why, truncated)
            return LLMResult(
                parsed=parsed, text=reply.text, model_used=model, tokens_in=run.tokens_in, tokens_out=run.tokens_out,
                attempts=run.outbound, fallback_used=fallback,
            )

    @staticmethod
    def _bad_output(run: _Run, sp, why: str, truncated: bool = False) -> object:
        """Unusable output: one re-ask per call (it costs a call), then LLM_BAD_OUTPUT. A reply that hit the output cap is
        asked again with a bigger one: the same cap would end the same way."""
        sp.set(retry_reason="bad_output")
        sp.finish("error", ErrorKind.LLM_BAD_OUTPUT.value)
        if run.reasked:
            raise AgentError(ErrorKind.LLM_BAD_OUTPUT, detail=f"{why}, again after one re-ask")
        run.reasked = True
        if truncated:
            run.max_output_tokens = max(
                run.max_output_tokens, min(run.max_output_tokens * TRUNCATION_REASK_FACTOR, MAX_OUTPUT_TOKENS_CEILING)
            )
        return _REASK

    def _send(self, run: _Run, model: str, sp) -> _Reply:
        if self._chaos is not None:
            fault = self._chaos.maybe_fail_llm(run.step)
            if fault is not None:
                sp.set(chaos_fault=getattr(fault, "chaos_fault", "unknown"))
                if isinstance(fault, MalformedReply):  # free text has no JSON to break: it comes back empty instead
                    return _Reply(text=MalformedReply.text if run.schema is not None else "", finish="STOP")
                raise fault
        config = genai_types.GenerateContentConfig(
            system_instruction=run.system + ("\n\n" + REASK_NOTE if run.reasked and run.schema else ""),
            temperature=run.temperature,
            max_output_tokens=run.max_output_tokens,
            http_options=self.http_options(call_timeout(self._llm.request_timeout_s, run.budget)),
        )
        if run.schema is not None:
            config.response_mime_type = "application/json"
            config.response_schema = run.schema
        return _read(self.client.models.generate_content(model=model, contents=run.contents, config=config))

    # accounting and errors -----------------------------------------------------------------------

    def _count(self, run: _Run, model: str, tokens_in: int, tokens_out: int, fallback: bool) -> None:
        turn_metrics().add_llm_call(
            model, tokens_in=tokens_in, tokens_out=tokens_out, fallback_used=fallback,
            prompt_version=run.prompt.get("prompt_version"),
        )
        if self._cost_hook is not None:
            try:
                self._cost_hook(model, tokens_in, tokens_out)
            except Exception as exc:  # noqa: BLE001 - a bookkeeping hook must never fail the call it measures
                log.warning("the cost hook failed: %s", crash_summary(exc))

    def _classify(self, exc: Exception, model: str) -> _Failure | None:
        """A Gemini or network failure as a `_Failure`, or None when it is not one (a bug: the turn boundary reports it)."""
        if isinstance(exc, genai_errors.APIError):
            code = int(exc.code or 0)
            if not code:
                return None
            status = exc.status if isinstance(exc.status, str) and _STATUS_WORD.fullmatch(exc.status) else ""
            where = f"HTTP {code}{' ' + status if status else ''} from {model}"
            if code == 429:
                return _Failure(model, "rate_limited", ErrorKind.LLM_RATE_LIMITED, where, retry_after=_retry_after(exc))
            if code == 408 or code >= 500:
                return _Failure(model, "timeout" if code == 408 else "server_error", ErrorKind.LLM_UNAVAILABLE, where)
            if code == 404:  # the model ID does not exist for this key: the sibling may still work
                return _Failure(
                    model, "model_not_found", ErrorKind.LLM_UNAVAILABLE, where + ": check llm.models in config.yaml",
                    retryable=False, counts=False, message=SETUP_MESSAGE,
                )
            return _Failure(  # 400, 401, 403 ...: our request or key is wrong, and the sibling would fail the same way
                model, "bad_request", ErrorKind.LLM_UNAVAILABLE, where, retryable=False, counts=False, terminal=True,
                message=SETUP_MESSAGE,
            )
        if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
            return _Failure(model, "timeout", ErrorKind.LLM_UNAVAILABLE, f"no answer from {model} in time")
        if isinstance(exc, (httpx.TransportError, ConnectionError)):
            return _Failure(model, "network", ErrorKind.LLM_UNAVAILABLE, f"connection to {model} failed")
        return None

    def _chain_error(self, run: _Run, chain: tuple[str, ...]) -> AgentError:
        paused = f"; paused: {', '.join(run.skipped)}" if run.skipped else ""
        if not run.failures:
            wait = min(self._breakers.get(model).retry_in() for model in run.skipped)
            return AgentError(
                ErrorKind.CIRCUIT_OPEN, detail=f"{', '.join(chain)} paused after repeated failures; the next probe is in {wait:.0f} s"
            )
        tried = "; ".join(f"{f.model}: {f.reason}" for f in run.failures)
        return run.failures[-1].to_error(f"every model failed ({tried}{paused})")


# reading the reply ------------------------------------------------------------------------------

def _count_of(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _read(response: Any) -> _Reply:
    usage = getattr(response, "usage_metadata", None)
    tokens_in = _count_of(getattr(usage, "prompt_token_count", 0))
    tokens_out = _count_of(getattr(usage, "candidates_token_count", 0)) + _count_of(getattr(usage, "thoughts_token_count", 0))

    # Only a refusal the model states is a block. A reply with no candidate or no text and no stated reason is a glitch.
    blocked: str | None = None
    feedback = getattr(response, "prompt_feedback", None)
    reason = _name(feedback.block_reason) if feedback is not None and getattr(feedback, "block_reason", None) else None
    if reason and reason != "BLOCKED_REASON_UNSPECIFIED":
        blocked = f"the prompt was blocked ({reason})"
    candidates = getattr(response, "candidates", None) or []
    finish = _name(candidates[0].finish_reason) if candidates and candidates[0].finish_reason else None
    if blocked is None and finish in _BLOCKING_FINISH:
        blocked = f"the reply was blocked ({finish})"
    try:
        text = response.text or ""  # joins the text parts and leaves out the model's thoughts
    except Exception:  # noqa: BLE001 - an odd part layout must read as an empty reply, not crash the turn
        text = ""
    return _Reply(text=text, tokens_in=tokens_in, tokens_out=tokens_out, finish=finish, blocked=blocked)


def _name(value: Any) -> str:
    return str(getattr(value, "name", value))


def _parse(schema: type[BaseModel] | None, text: str) -> tuple[BaseModel | None, str | None]:
    """(parsed, None) or (None, why). `why` names the schema fields at fault, never the model's text."""
    if schema is None:
        return None, None
    stripped = text.strip()
    fenced = _FENCE.match(stripped)
    try:
        return schema.model_validate_json(fenced.group(1) if fenced else stripped), None
    except ValidationError as exc:
        problems = [f"{'.'.join(str(part) for part in err['loc']) or 'reply'}: {err['type']}" for err in exc.errors()[:3]]
        return None, "the reply did not match the schema (" + "; ".join(problems) + ")"


def _retry_after(exc: genai_errors.APIError) -> float | None:
    """The wait a 429 asks for: the RetryInfo detail, else the Retry-After header. None when there is none."""
    try:
        details = exc.details
        error = details.get("error", details) if isinstance(details, dict) else {}
        for item in error.get("details") or []:
            if isinstance(item, dict) and str(item.get("@type", "")).endswith("RetryInfo"):
                match = _DURATION.fullmatch(str(item.get("retryDelay", "")).strip())
                if match:
                    return min(float(match.group(1)), MAX_RETRY_AFTER_S)
        header = getattr(getattr(exc, "response", None), "headers", None)
        value = header.get("retry-after") if header is not None else None
        if value is not None and re.fullmatch(r"\d+(?:\.\d+)?", str(value).strip()):
            return min(float(value), MAX_RETRY_AFTER_S)
    except Exception:  # noqa: BLE001 - a hint we cannot read is no hint
        return None
    return None
