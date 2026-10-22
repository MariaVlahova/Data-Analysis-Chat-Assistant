# Plan 06: Result and Output Guards
Status: not started

**Goal:** Mask PII in result rows before the model sees them, replace real customer IDs with fixed fake IDs per session, hide groups below k = 10 except for a ranked-spend-list exception — still an open rule per decision.md's own verdict (see PRD F15/A3) — that never joins demographic columns, cap rows at 50, and scan the final answer for PII before it is shown.
