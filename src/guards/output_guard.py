"""The Output Guard: the last check on the final answer before anything is printed (plan 06, F16, NFR11, S9).

It runs on the composed text, after the numeric check. It scans for everything `src/pii.py` knows (e-mail, phone,
national or tax ID, card) and for the output-only patterns (street address, coordinates, postal code). On a finding it
blocks the whole answer: we cannot be sure what surrounds a match, so we do not cut it out and show the rest.

A block here means an earlier layer failed (the SQL Guard, the Result Guard or the pseudonymizer), so it is an incident,
not a normal event: the turn's metrics row gets `pii_at_output`, which raises the alert line (plan 02).

S9: nothing from the Composer is printed before `check()` returns. Only progress text streams. The CLI prints the answer
once, after this call.

Production replaces the regex scan with Cloud Sensitive Data Protection and Model Armor (decision section 8, plan 11).
"""
from __future__ import annotations

import bisect
import logging
import re
from typing import Any

from src import pii
from src.errors import AgentError, ErrorKind, crash_summary
from src.guards.pseudonymizer import FIRST_DIGITS, LABEL_PREFIX, MAX_DIGITS
from src.observability import current_trace_id, guard_decision, span, turn_metrics
from src.pii import Finding

log = logging.getLogger(__name__)

BLOCKED_MESSAGE = (
    "I held back this answer because it may contain personal data. Please rephrase, or ask for totals instead."
)  # `friendly()` adds the line "Trace ID: ..."

# The fake ID of the session pseudonymizer, with the digit counts it can issue and no more: `Customer #4111111111111111`
# is a card number with a prefix, not a label. A finding that sits wholly inside a label is not personal data (A3). A finding
# that only starts or ends inside one (a card number that begins with the label) is still reported.
_FAKE_ID = re.compile(rf"{re.escape(LABEL_PREFIX)}\d{{{FIRST_DIGITS},{MAX_DIGITS}}}(?!\d)")


class OutputGuard:
    def scan(self, text: str) -> list[Finding]:
        """Every personal-data finding in `text`, in order of position. Raises TypeError if `text` is not text."""
        if not isinstance(text, str):
            raise TypeError("the answer is not text")
        label_starts: list[int] = []
        label_ends: list[int] = []
        for match in _FAKE_ID.finditer(text):  # in order of position, never overlapping
            label_starts.append(match.start())
            label_ends.append(match.end())

        def inside_a_label(finding: Finding) -> bool:
            index = bisect.bisect_right(label_starts, finding.start) - 1  # the last label that starts at or before the finding
            return index >= 0 and finding.end <= label_ends[index]

        return [finding for finding in pii.scan(text, extended=True) if not inside_a_label(finding)]

    def check(self, text: str) -> str:
        """The text itself when it is clean. Raises OUTPUT_BLOCKED otherwise.

        One wrapper covers the scan and the recording of its result: whatever fails, the caller gets the typed error and the
        answer is not shown, never an arbitrary exception that plan 07 would report as INTERNAL."""
        with span("guard.output", "guard", chars=len(text) if isinstance(text, str) else None) as sp:
            findings: list[Finding] = []
            try:
                findings = self.scan(text)
                if not findings:
                    guard_decision("output", "pass")
                    return text
                self._record_block(sp, findings)
            except Exception as exc:  # noqa: BLE001 - fail closed: an answer that cannot be checked is not shown
                try:
                    guard_decision("output", "block", rule="PII_AT_OUTPUT" if findings else "GUARD_ERROR")
                except Exception as recording_exc:  # noqa: BLE001 - the typed error below is what the caller must get
                    log.error("could not record the Output Guard failure: %s", crash_summary(recording_exc))
                if findings:  # the answer has personal data and the recording of it failed: the user still gets the usual message
                    raise self._blocked(findings, crash_summary(exc)) from None
                raise AgentError(ErrorKind.OUTPUT_BLOCKED, detail=crash_summary(exc), trace_id=current_trace_id()) from None
            raise self._blocked(findings)

    @staticmethod
    def _record_block(sp: Any, findings: list[Finding]) -> None:
        kinds = sorted({finding.type for finding in findings})
        turn_metrics().add_pii_at_output(len(findings))
        # The held-back answer is NOT stored, not even masked: a pattern cannot mask a name or an unlabelled real ID, and
        # `/trace` shows the user their own turns, so the answer the guard held back would be one command away. The
        # incident review gets what it needs without the text: how many findings, of which kind, where in the answer.
        places = [f"{finding.type}@{finding.start}-{finding.end}" for finding in findings]
        sp.set(findings=len(findings), kinds=kinds, places=places)
        guard_decision("output", "block", rule="PII_AT_OUTPUT", findings=len(findings), kinds=kinds)

    @staticmethod
    def _blocked(findings: list[Finding], also: str = "") -> AgentError:
        kinds = ", ".join(sorted({finding.type for finding in findings}))
        detail = f"{len(findings)} finding(s) in the answer: {kinds}" + (f"; recording failed: {also}" if also else "")
        return AgentError(ErrorKind.OUTPUT_BLOCKED, BLOCKED_MESSAGE, detail=detail, trace_id=current_trace_id())
