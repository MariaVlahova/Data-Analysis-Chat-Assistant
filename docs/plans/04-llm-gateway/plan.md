# Plan 04: LLM gateway
Status: not started

**Goal:** Build one Gemini gateway that routes each step to its model and handles rate limits, retries with jitter, a circuit breaker, Flash to Flash-Lite fallback, a per-turn call budget (config-driven and larger for "why" and report turns than for a simple turn) and a --chaos flag for fault injection.
