"""Local stand-in for the PII alert (NFR20).

Any PII the Output Guard saw in a Composer draft means an earlier layer failed, so it is an incident.
The prototype logs CRITICAL (done where the row is written), prints one warning line after the turn,
and adds an ALERT line to the metrics summary. Production sends a Pub/Sub event and a Cloud Monitoring
alert (design-only, plan 11).
"""
from __future__ import annotations

from typing import Any, Mapping

from src.observability.metrics import MetricsSummary

SHOWN_TRACE_IDS = 5


def turn_alert(row: Mapping[str, Any] | None) -> str | None:
    """The one-line warning for a finished turn, or None when its row is clean."""
    if not row:
        return None
    count = row.get("pii_at_output")
    if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
        return None
    return (
        f"ALERT: personal data was found in a draft answer and blocked ({count} finding(s)). "
        f"An earlier safety layer missed it. Trace ID: {row.get('trace_id', '?')}"
    )


def summary_alert(summary: MetricsSummary) -> str | None:
    """The ALERT line for the metrics summary, or None when no turn had PII at output."""
    if summary.pii_at_output_total <= 0:
        return None
    ids = summary.pii_alert_trace_ids
    shown = ", ".join(ids[:SHOWN_TRACE_IDS]) + (f" and {len(ids) - SHOWN_TRACE_IDS} more" if len(ids) > SHOWN_TRACE_IDS else "")
    return (
        f"ALERT: {summary.pii_at_output_total} PII finding(s) at output in {len(ids)} turn(s). "
        f"This must be 0. Check /trace for: {shown}"
    )
