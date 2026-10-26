"""Tracing, metrics and log masking (plan 02).

Plans 03 to 08 import from here and use the span names in docs/plans/02-observability/plan.md:

    from src.observability import span, event, guard_decision, turn_metrics, prompt_attrs
"""
from src.observability.logging_setup import PIIMaskingFilter
from src.observability.metrics import TurnMetrics
from src.observability.tracing import (
    Tracer,
    TurnTrace,
    current_trace_id,
    debug_prompts_enabled,
    event,
    guard_decision,
    prompt_attrs,
    purge_old_traces,
    span,
    turn_metrics,
)

__all__ = [
    "PIIMaskingFilter",
    "Tracer",
    "TurnMetrics",
    "TurnTrace",
    "current_trace_id",
    "debug_prompts_enabled",
    "event",
    "guard_decision",
    "prompt_attrs",
    "purge_old_traces",
    "span",
    "turn_metrics",
]
