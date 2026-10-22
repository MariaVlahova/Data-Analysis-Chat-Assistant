# Plan 02: Observability
Status: not started

**Goal:** Give every turn a short trace ID with spans for each step, model call, tool call and query, mask PII out of every span and trace (not only the metrics row), write one PII-free metrics row per turn (task success, guard blocks, PII at output, latency, cost) to runs.jsonl, and expose them through the /trace command and a metrics summary command.
