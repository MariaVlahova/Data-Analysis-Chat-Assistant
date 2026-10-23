# Plan 04: LLM gateway
Status: not started
Estimate: about 1 h (our estimate) · Depends on: 01, 02 · Blocks: 07

**Goal:** Build one Gemini gateway that routes each step to its model and handles rate limits, retries with jitter, a circuit breaker, Flash to Flash-Lite fallback, a per-turn call budget (config-driven and larger for "why" and report turns than for a simple turn) and a --chaos flag for fault injection.

Tags and `(impl)` marks are explained in plan 01.

## Requirements covered
| ID | Requirement | Source |
|---|---|---|
| F35 | Gemini 429 and 5xx errors: retry with growing waits, pause a failing provider, fall back to a sibling model | PRD 6.4 |
| F37 | A `--chaos` flag injects 429s, timeouts and bad SQL to show the resilience (P1) | PRD 6.4 |
| F34 (part) | Loops stop on max tries, **call budget**, timeout | PRD 6.4 |
| NFR4 | AI calls per turn: about 8 for a simple turn. "Why" and report turns need their own number. [OPEN] | decision §4 |
| NFR9 | Gemini rate limits are read from config. They are not published. [PROPOSED] | decision §4 |
| S3 [High] | About 8 calls cannot cover up to 12 SQL calls plus other steps. Set a separate number for "why" and report turns. | decision 9.2 |
| S12 [Medium] | Parallel sub-queries plus retries can cause a burst of 429s. Tie the parallel limit to the rate limiter. | decision 9.2 |

## Decisions that apply
- 3.2 [PROPOSED]: models by step. Fallback chain is primary, sibling model, Vertex AI Gemini, degraded mode. The prototype uses Flash with Pro off. Model IDs need a check at build time (A14, Q10).
- 3.11 [PROPOSED]: error 429 or 5xx means wait longer before each retry, pause a failing provider, use the fallback chain. A call budget per turn: about 8 for a simple turn, about 60 s.
- 3.10: the Budget plus tries, timeout, same SQL twice and same error twice stop each loop. Code stops loops, never the model.
- §8: prototype fallback is Flash to Flash-Lite, plus `--chaos` (429s, timeouts, bad SQL). Vertex AI fallback is design-only.
- 3.2 Rejected: Pro everywhere (too much quota and money).
- S13: the AI judge uses a preview Pro model (offline only; pin the ID).
- Brief R5: resilient to API and third-party downtime, without inflating costs.

## Scope
- In: model routing, rate limiter, retry with jitter, circuit breaker, fallback chain, structured output, token accounting, the per-turn `TurnBudget`, chaos injection, spans.
- Out: prompts (plan 07), Input Guard logic (plan 07), embeddings (plan 10 uses lexical search in the prototype), Vertex AI client (design-only), daily per-user budget (NFR8 [OPEN]; the gateway exposes a cost hook, nothing enforces it).

## Interfaces this plan provides
```python
class Step(str, Enum):
    INPUT_GUARD; PLANNER; SQL_WRITE; SQL_REPAIR; EVIDENCE_CHECK; COMPOSER; LAST_REPAIR; JUDGE

class LLMGateway:
    def generate(self, step: Step, *, system: str, contents: str | list, schema: type[BaseModel] | None,
                 budget: TurnBudget, temperature: float | None = None) -> LLMResult
    # raises AgentError: LLM_RATE_LIMITED, LLM_UNAVAILABLE, LLM_BAD_OUTPUT, LLM_BLOCKED, CIRCUIT_OPEN, BUDGET_EXHAUSTED, TURN_TIMEOUT

@dataclass
class LLMResult: parsed: BaseModel | None; text: str; model_used: str; tokens_in: int; tokens_out: int; attempts: int; fallback_used: bool

class TurnBudget:                       # one per turn, created by plan 07
    def __init__(self, calls: int, seconds: float, bytes_: int, clock=time.monotonic)
    def charge_call(self); def charge_bytes(self, n); def check_time(); def remaining_seconds()
    def upgrade(self, kind: Literal["simple","why","report"])   # raise limits after the Input Guard classifies the turn

class Chaos:                            # built from --chaos / --chaos-seed
    def maybe_fail_llm(self, step) -> None | Exception
    def maybe_corrupt_sql(self, sql: str) -> str
    def maybe_fail_bq(self) -> None | Exception
```

## Design
### Model routing (3.2)
Model IDs are config values, never hard-coded. The names below are the ones decision.md lists and they still need the build-time check (A14).

| Step | Primary model | Fallback (prototype) |
|---|---|---|
| INPUT_GUARD, EVIDENCE_CHECK | `gemini-3.5-flash-lite` | `gemini-3.8-flash` |
| PLANNER, SQL_WRITE, SQL_REPAIR, COMPOSER | `gemini-3.8-flash` | `gemini-3.5-flash-lite` |
| LAST_REPAIR, full reports (off in the prototype), offline JUDGE | `gemini-3.1-pro-preview` (preview only, pin the ID, S13) | none |
| Embeddings (not used in the prototype) | `gemini-embedding-001` | none |

- `llm.pro_enabled: false` in the prototype. With Pro off, LAST_REPAIR uses Flash.
- First task: call `models.list()` and compare against these IDs. If an ID is missing, stop with a clear message and fix the config. This closes A14 for the build.
- Production chain: primary → sibling → **Vertex AI Gemini** → degraded mode (honest message, non-LLM commands still work). The gateway has a `providers` list so adding Vertex later is a config change. Not built in the prototype.
- SDK: the `google-genai` client. Structured output by passing a pydantic schema (JSON response). We turn off the SDK's own retries and do all retrying here, so there is one place that counts calls. (impl; verify option names on the installed version)

### Per-call flow
```text
budget.charge_call() → limiter.acquire(model) → breaker.allow(model)
 → send (timeout) → parse + validate schema → record tokens
 on 429/5xx/timeout: breaker.record_failure, wait (backoff + jitter), retry
 on retries exhausted or breaker open: next model in the chain
 chain exhausted: raise typed error
```
- **Every outbound attempt costs one call** from the budget, including retries and fallbacks. Free-tier quota is per request, so this keeps the "calls" number honest and stops retries from inflating cost (brief R5). (impl)
- Temperature: 0 for Input Guard, Planner, SQL, repair and Evidence Check. About 0.3 for the Composer so tone can vary. `max_output_tokens` per step in config. (impl)

### Rate limiter (NFR9, S12)
- Token bucket per model, filled from config `llm.rate_limits.<model>.rpm`. Defaults are deliberately low (10 requests per minute) and must be raised from the Google AI Studio limits page when the project is set up. The numbers are not published in our sources.
- A request waits for a token, but never longer than the turn's remaining seconds. If waiting would pass the deadline it raises `LLM_RATE_LIMITED` at once instead of sleeping.
- `loop.parallel_subqueries` (plan 07) is capped by the limiter's capacity: `min(config value, bucket size)` (S12). Default 1, which means sequential.

### Retry with jitter (F35)
- Retry on 429, 5xx and timeouts. Do not retry 400-class errors (bad request) or safety blocks.
- Wait = full jitter: random between 0 and `min(cap, base × 2^attempt)`. If the 429 response carries a retry delay, wait at least that long. Defaults: base 1 s, cap 20 s, 3 attempts per model.
- Invalid JSON or schema mismatch: one re-ask per step (counts as a call). Then `LLM_BAD_OUTPUT`.
- Safety block or empty candidate: `LLM_BLOCKED`, no retry with the same prompt. The Input Guard treats it as a block (S10).

### Circuit breaker
- One breaker per model. Closed → open after `failure_threshold` consecutive failures (default 3). Open for `cooldown_s` (default 30 s). Then half-open: one probe request; success closes it, failure reopens it.
- Only 429, 5xx and timeouts count. Bad requests do not.
- While open, the chain skips that model at once. If every model in the chain is open → `CIRCUIT_OPEN`.
- The clock is injected so tests never sleep.

### Turn budget (S3, NFR4, Q3)
Derive the numbers instead of guessing:
- Simple turn: Input Guard 1 + SQL 1 + up to 2 repairs + 1 broaden + Composer 1 = 6, so 8 leaves two attempts for retries. **8 calls, 60 s.** (decision 3.11)
- Why or report turn with the cap of 4 sub-questions: Input Guard 1 + Planner 1 + 4 × (1 SQL + 2 repairs + 1 broaden) + Evidence Check 1 + Composer 1 = 20. With `max_replans = 0` this is the worst case. **20 calls.** Seconds: 60 for "why" (NFR3), 90 for a report in the prototype because background workers are design-only (NFR2, §8). Every replan adds one Planner call plus its sub-query calls; if `max_replans` is raised, raise `calls` with it.
- Bytes per turn: `budget.bytes_per_turn` (plan 01).
- All numbers are config and tagged [OPEN] until the client answers Q3. Plan 07 creates the budget with the simple limits, then calls `upgrade()` after the Input Guard classifies the turn, because the classification call itself happens before the kind is known.
- When the budget runs out: raise `BUDGET_EXHAUSTED` (calls or bytes) or `TURN_TIMEOUT`. The graph stops the loop and the Composer says what was done and what is missing.
- Not built: daily budget per user with an 80% alert (NFR8 [OPEN]). The gateway calls an optional `cost_hook(tokens, model)` after each call, which production would use.

### Chaos (`--chaos`, F37, §8)
- `--chaos mild|harsh` and `--chaos-seed N`. Seeded random, so a run can be replayed.
- Faults: 429 (with a retry delay), 503, timeout, malformed JSON from the model, a BigQuery timeout, and **bad SQL** (the SQL writer's output is replaced once by a query with a syntax error or an unknown column, to drive the repair loop).
- `mild`: about 15% of calls. `harsh`: about 40%. Both capped so a turn can still finish. (impl)
- Chaos faults go through the same retry, breaker and fallback code as real faults. They never skip a guard. A chaos turn sets `chaos: true` in the metrics row.
- Tests use a scripted mode: "fail the next N calls with X" instead of random.

### Spans and metrics
Each attempt is an `llm.call` span: step, model, attempt number, tokens, latency, retry reason, fallback used, prompt version. The gateway adds to `turn_metrics`: calls, tokens, fallback used.

## Files
```text
src/llm/__init__.py  gateway.py  routing.py  limiter.py  breaker.py  budget.py  chaos.py  fake.py
tests/test_gateway.py  test_limiter.py  test_breaker.py  test_budget.py  test_chaos.py
```
`fake.py` is a scripted gateway for tests in plans 07 and 09: it returns queued responses by step, so no test needs a network.

## Tasks
- [ ] Install `google-genai`. Write a 10-line script that lists models and makes one call. Record the exact exception types for 429 and 5xx.
- [ ] Check the model IDs against `models.list()`. Update config if needed.
- [ ] `budget.py` with `upgrade()` and an injectable clock.
- [ ] `limiter.py` (token bucket) and `breaker.py` (three states) with injected clocks.
- [ ] `routing.py`: step → chain from config.
- [ ] `gateway.py`: the per-call flow, structured output, error mapping to `ErrorKind`, spans, token accounting.
- [ ] `chaos.py` and the CLI flags.
- [ ] `fake.py` scripted gateway.
- [ ] Add the Gemini ping to `doctor --live`.

## Tests and acceptance
- Router: each step gets the right chain; Pro is skipped when disabled.
- Limiter: a burst waits; the wait is refused when it would pass the deadline.
- Retry: 429 then success → one retry, jittered wait within bounds, **two calls charged**. Retry delay from the response is honored.
- Breaker: three failures open it; the next call skips to the sibling; after the cooldown one probe closes it.
- Fallback: Flash fails all attempts → Flash-Lite answers → `fallback_used` is true. Both fail → typed error, no crash.
- Budget: the 9th call in a simple turn raises `BUDGET_EXHAUSTED`; `upgrade("why")` raises the limit to 20; the clock past 60 s raises `TURN_TIMEOUT`.
- Bad JSON: one re-ask, then `LLM_BAD_OUTPUT`.
- Chaos with a fixed seed gives the same fault sequence twice.
- Acceptance (live): a real structured-output call returns parsed JSON; with `--chaos harsh` a short script of 10 calls finishes without an unhandled exception.

## Open items and defaults used
| Item | Default used | Who answers |
|---|---|---|
| Q3, NFR4 budgets | 8 / 20 / 20 calls; 60 / 60 / 90 s | Us, then client |
| Q10 models, Pro in routing | Flash-family only, Pro off, Pro only for the offline judge | OpsFleet client |
| Q28 retry limits and 429 rules | In config; longer wait per retry; Flash to Flash-Lite | Us |
| NFR9 rate limits | 10 rpm per model until set from AI Studio | Us |
| NFR8 daily budget | Not enforced. Hook only. | OpsFleet client |

## Risks and pitfalls
- Model IDs in decision.md may not exist at build time. Check first.
- The free tier may block the demo (decision 9.1). The limiter and fallback reduce the risk but do not remove it. Keep `--chaos` runs offline with `fake.py` where possible and use live calls sparingly.
- Letting the SDK retry on its own hides calls from the budget. Turn it off.
- Falling back to Flash-Lite for SQL writing can lower quality. The guards still hold; the metrics row shows `fallback_used` so the effect is visible.

## Hand-off
- Plan 07 builds one `TurnBudget` per turn, calls `upgrade()` after the Input Guard, and passes it to every `generate()` call and to `SafeQueryRunner`.
- Plan 03's `SafeQueryRunner` reads `budget.bytes_remaining` (a property) and calls `charge_bytes(n)` and `check_time()`. Add `bytes_remaining` to `TurnBudget`.
- Plan 09 uses `fake.py` and the chaos seeds for the resilience tests.
- Plan 11 documents the full production chain including Vertex AI.
