"""Gemini checks for `doctor --live` (plan 04): the configured model IDs exist for this key (A14), and one small
structured call works.

They run only with `--live`, because a call uses a little of the free-tier quota. Without the flag each shows as
skipped, so the output says what a plain `doctor` did not look at.

The Google SDK is imported inside the checks, not here: `doctor` must still start on a broken install so that its
Packages check can name what is missing.
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from src.cli.doctor_core import Check, DoctorContext
from src.errors import AgentError, crash_summary

if TYPE_CHECKING:
    from src.llm.gateway import LLMGateway

log = logging.getLogger(__name__)

LIVE_HINT = "run `doctor --live` to call Gemini (a call uses a little of the free-tier quota)"
MODELS_NAME = "Gemini models"
CALL_NAME = "Gemini call"
SHOWN_MODELS = 12


def _gateway(ctx: DoctorContext) -> "LLMGateway":
    if "gateway" not in ctx.shared:
        from src.llm.gateway import LLMGateway

        ctx.shared["gateway"] = LLMGateway(ctx.settings)
    return ctx.shared["gateway"]


def _not_ready(ctx: DoctorContext, name: str) -> Check | None:
    """The finished `Check` when Gemini cannot be asked, else None."""
    if ctx.settings is None:
        return Check(name, "skip", "skipped because the config file did not load")
    if not ctx.live:
        return Check(name, "skip", LIVE_HINT)
    if "gateway" not in ctx.shared and ctx.settings.secrets.gemini_api_key is None:
        return Check(name, "skip", "skipped because GEMINI_API_KEY is not set")
    return None


def _why(exc: BaseException) -> str:
    """What to print about a failure: the kind or the HTTP code, never the library's message."""
    if isinstance(exc, AgentError):
        return exc.kind.value
    code = getattr(exc, "code", None)
    return f"HTTP {code}" if isinstance(code, int) else type(exc).__name__


def check_gemini_models(ctx: DoctorContext) -> Check:
    not_ready = _not_ready(ctx, MODELS_NAME)
    if not_ready is not None:
        return not_ready
    gateway = _gateway(ctx)
    try:
        available = gateway.available_models()
    except Exception as exc:  # noqa: BLE001 - a diagnostic reports a failure, it does not crash
        log.warning("listing the Gemini models failed: %s", crash_summary(exc))
        return Check(MODELS_NAME, "fail", f"could not list the models ({_why(exc)})",
                     "Check GEMINI_API_KEY in .env and your connection, then run doctor --live again.")
    needed = gateway.router.models_in_use()
    missing = [model for model in needed if model not in available]
    if missing:
        ctx.shared["models_missing"] = True
        offered = sorted(name for name in available if name.startswith("gemini") and "embedding" not in name)
        shown = ", ".join(offered[:SHOWN_MODELS]) + (f", and {len(offered) - SHOWN_MODELS} more" if len(offered) > SHOWN_MODELS else "")
        return Check(MODELS_NAME, "fail", f"not available to this key: {', '.join(missing)}",
                     f"Set llm.models in config.yaml to IDs this key has. Gemini models it lists: {shown or 'none'}.")
    return Check(MODELS_NAME, "ok", f"all {len(needed)} configured model(s) exist: {', '.join(needed)}")


def check_gemini_call(ctx: DoctorContext) -> Check:
    not_ready = _not_ready(ctx, CALL_NAME)
    if not_ready is not None:
        return not_ready
    if ctx.shared.get("models_missing"):
        return Check(CALL_NAME, "skip", "skipped because a model ID is missing")
    from pydantic import BaseModel

    from src.llm.budget import TurnBudget
    from src.llm.routing import Step

    class Ping(BaseModel):
        ok: bool

    gateway = _gateway(ctx)
    # Enough calls to walk the whole chain (every attempt on both models) plus one re-ask, so a failing first model
    # shows up as a warning about the sibling instead of the ping running out of budget.
    budget = TurnBudget(calls=2 * ctx.settings.llm.retry.attempts + 1, seconds=2 * ctx.settings.llm.request_timeout_s, bytes_=0)
    started = time.perf_counter()
    try:
        result = gateway.generate(
            Step.INPUT_GUARD, system='This is a connectivity test. Reply with the JSON {"ok": true}.',
            contents="ping", schema=Ping, budget=budget,
        )
    except AgentError as exc:
        return Check(CALL_NAME, "fail", f"{exc.kind.value}: {exc.detail or exc.user_message}",
                     "Check GEMINI_API_KEY and the free-tier limits in Google AI Studio, then run doctor --live again.")
    except Exception as exc:  # noqa: BLE001
        log.error("the Gemini check crashed: %s", crash_summary(exc))
        return Check(CALL_NAME, "fail", f"the call crashed ({type(exc).__name__})")
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    detail = (f"{result.model_used} answered with structured JSON in {elapsed_ms} ms "
              f"({result.tokens_in} tokens in, {result.tokens_out} out, {result.attempts} attempt(s))")
    if result.fallback_used:
        return Check(CALL_NAME, "warn", detail + "; the sibling model answered because the first one failed",
                     "Look at the Gemini rate limits in Google AI Studio and raise llm.rate_limits in config.yaml.")
    return Check(CALL_NAME, "ok", detail)


# In the order they run. `doctor.py` registers them after the BigQuery checks.
GEMINI_CHECKS = [check_gemini_models, check_gemini_call]
