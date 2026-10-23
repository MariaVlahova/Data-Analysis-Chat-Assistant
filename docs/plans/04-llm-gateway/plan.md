# Plan 04: LLM gateway
Status: built and tested offline against a scripted SDK client (206 new tests, 970 in the offline suite). Four steps need a Gemini API key and are NOT done: the model IDs checked against `models.list()` (A14), a real structured-output call, a live `--chaos harsh` run, and the requests-per-minute numbers from Google AI Studio (see "Still to do with a Gemini key")
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
    def generate(self, step: Step, *, system: str, contents: str | list, schema: type[BaseModel] | None = None,
                 budget: TurnBudget, temperature: float | None = None, prompt: Mapping | None = None) -> LLMResult
    # prompt: the span attributes from observability.prompt_attrs(...); parallel_limit(requested) caps loop.parallel_subqueries (S12)
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
- A limiter per model with at most `llm.rate_limits.<model>.rpm` requests in any 60 seconds (a sliding window; the first design was a token bucket, see "As built"). Defaults are deliberately low (10 requests per minute) and must be raised from the Google AI Studio limits page when the project is set up. The numbers are not published in our sources.
- A request waits for a slot, but never so long that less than a second of the turn is left to make the call. If waiting would do that it raises `LLM_RATE_LIMITED` at once instead of sleeping.
- `loop.parallel_subqueries` (plan 07) is capped by the limiter's capacity: `min(config value, rpm)` (S12). Default 1, which means sequential.

### Retry with jitter (F35)
- Retry on 429, 5xx and timeouts. Do not retry 400-class errors (bad request) or safety blocks.
- Wait = full jitter: random between 0 and `min(cap, base × 2^attempt)`. If the 429 response carries a retry delay, wait at least that long. Defaults: base 1 s, cap 20 s, 3 attempts per model.
- Invalid JSON, a schema mismatch, an empty reply or a reply cut off at the output cap: one re-ask per step (counts as a call). Then `LLM_BAD_OUTPUT`. A cut-off reply is asked again with a bigger cap.
- A refusal the model states (a blocked prompt, or a finish reason such as SAFETY): `LLM_BLOCKED`, no retry with the same prompt. The Input Guard treats it as a block (S10). An empty reply with no stated refusal is not a block (see "As built").

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
- [x] Install `google-genai` (2.32.0 in the venv). Record the exception types for 429 and 5xx. Read from the SDK: a 429 is `google.genai.errors.ClientError` (`code` 429, status `RESOURCE_EXHAUSTED`, the wait in `details.error.details[]` as a `RetryInfo` with `retryDelay: "7s"`), a 5xx is `ServerError`, a timeout is an `httpx.TimeoutException`. **A real 429 or 5xx has not been seen yet**: the live run (below) confirms the shapes.
- [ ] Write a 10-line script that lists models and makes one call. **Needs a Gemini key.** `python -m src.cli.main doctor --live` does both.
- [ ] Check the model IDs against `models.list()`. Update config if needed. **Needs a Gemini key.** `doctor --live` prints the IDs the key can use and fails when a configured one is missing.
- [x] `budget.py` with `upgrade()` and an injectable clock.
- [x] `limiter.py` (sliding one-minute window) and `breaker.py` (three states) with injected clocks.
- [x] `routing.py`: step → chain from config.
- [x] `gateway.py`: the per-call flow, structured output, error mapping to `ErrorKind`, spans, token accounting.
- [x] `chaos.py` and the CLI flags (`--chaos` builds a seeded `Chaos`, the banner prints the seed).
- [x] `fake.py` scripted gateway, plus `FakeGenaiClient` and `FakeClock` for testing the real gateway.
- [x] Add the Gemini ping to `doctor --live`.
- [ ] Run the live acceptance (below). **Needs a Gemini key.**

### As built (where the code differs from the design above, and why)
| Plan said | Built | Why |
|---|---|---|
| Flow: `charge_call` → `limiter.acquire` → `breaker.allow` | `breaker.allow` → `budget.check_calls` → `limiter.acquire` → `budget.charge_call` → send | A model whose breaker is open must be skipped for free, and a call must be charged only when it goes out. A waiting call that the budget would refuse anyway must not hold a rate-limit slot. `check_calls()` checks without charging. |
| A limiter wait past the deadline raises `LLM_RATE_LIMITED` at once | The limiter raises; the gateway then asks the **sibling** model, and raises `LLM_RATE_LIMITED` only when every model in the chain is refused | The sibling has its own bucket. This is the fallback chain doing its job. |
| A retry delay in the 429 is honored | Also honored when it comes as a `Retry-After` header. A wait that would pass the turn's deadline is not slept: that model gives up and the sibling is asked | Never sleep past the deadline. |
| 3 attempts per model; one re-ask on bad JSON | The re-ask is not one of the 3 attempts: it is one per `generate()` call, costs a call, and is not repeated on the sibling. A second bad reply is `LLM_BAD_OUTPUT` at once | A re-ask is an answer problem, not a transport problem. Bounded at `attempts + 1` outbound calls per model. |
| "Do not retry 400-class errors" | A 404 (model ID unknown to the key) goes to the sibling and never opens the breaker. 400, 401, 403 raise `LLM_UNAVAILABLE` with a setup message, no retry, no sibling | The sibling would fail the same way, except for a wrong model ID. The detail is only `HTTP <code> <status word> from <model>`. |
| `generate(step, system, contents, schema, budget, temperature)` | `schema` defaults to `None`; one extra optional argument `prompt`, the span attributes from `observability.prompt_attrs(...)` | Plan 07 passes its template ID and version. Without it the span carries a hash of `system` as `step@unversioned+hash`. |
| `cost_hook(tokens, model)` | `cost_hook(model, tokens_in, tokens_out)`, called after every attempt; an error in it is logged (class and call sites only) and ignored | Input and output tokens are priced differently. |
| `TurnBudget(calls, seconds, bytes_, clock)` | Same, plus `TurnBudget.from_settings(settings.budget, clock)` (which `upgrade()` needs), `check_calls()`, `calls_used`, `calls_remaining`. `charge_bytes()` records the bytes, then raises `BUDGET_EXHAUSTED` when over | `SafeQueryRunner` already catches that error where it charges abandoned jobs. |
| Config: per-step `max_output_tokens`, temperature | New keys `llm.steps.<step>.{temperature, max_output_tokens}` (a step left out keeps its default; an unknown step stops startup) and `llm.request_timeout_s` | Everything is config. `LLM_STEP_NAMES` in `config.py` repeats the `Step` values because `config.py` cannot import `src.llm`; a test keeps them equal. |
| `Chaos.maybe_fail_bq()` | `SafeQueryRunner(chaos=...)` calls it before every dry run and job (never for the startup schema read) and classifies the exception with the same code as a real one | The only way a BigQuery fault goes through the real retry code. |
| `--chaos` and `--chaos-seed` flags | `--chaos-seed` without `--chaos` is a startup error. Without a seed one is drawn, kept in `session.chaos_seed` and printed in the banner as a replay command. `ChatSession.chaos_engine` holds the `Chaos` | A run that cannot be replayed proves little. |
| (not in the plan) | `Chaos.begin_turn()`; `fake.FakeGenaiClient` (SDK-shaped replies and errors under the real gateway) and `fake.FakeClock`; `tests/test_no_direct_gemini.py` | `begin_turn()` makes "bad SQL at most once per turn" and the cap on a run of faults per turn. The fake client tests the real retry, breaker and fallback code. The guard test keeps any file outside `src/llm/` from reaching the SDK, like `test_no_direct_execute.py` does for BigQuery. |

Changed after the code review (15 findings, all fixed):
| Was | Now | Why |
|---|---|---|
| Rate limit: a token bucket as big as a minute's allowance | A sliding 60-second window: at most `rpm` requests in any minute. A burst of `rpm` goes through at once, then the next waits until the oldest is a minute old | The bucket let about `2 x rpm` through in the first minute, so the limit in NFR9 did not hold and 429s would follow. |
| A wait was slept if it was shorter than the time left in the turn | A retry wait or a rate-limit wait must leave at least 1 second (`MIN_USEFUL_CALL_S`) for the call; otherwise that model gives up and the sibling is asked now | Sleeping 9.9 s of 10 s left a call with 0.1 s while the sibling was free. |
| Empty reply or no candidate with no stated refusal: `LLM_BLOCKED` (plan text: "empty candidate") | Bad output: asked again once, then `LLM_BAD_OUTPUT` (retryable). Only a stated refusal is `LLM_BLOCKED`: a prompt block reason, or a finish reason SAFETY, BLOCKLIST, PROHIBITED_CONTENT, SPII, RECITATION, LANGUAGE or an image one | A transient glitch must not reject a legitimate question as unsafe (S10). **This departs from the plan's wording**; the Input Guard still blocks the request on either error (`INPUT_GUARD_FAILED`, Q13). |
| A reply that hit `MAX_TOKENS` was asked again with the same cap; free text that hit it was returned as an answer | It is bad output in both cases (schema or not) and is asked again with twice the cap, up to 16 384 (never lowering a higher cap you set) | The same cap ends the same way, and half a sentence is no answer. Thinking models spend the cap on thoughts. |
| Chaos `BAD_JSON` on a step with no schema returned the garbage as the answer | On a step with no schema it comes back as an empty reply, which takes the same re-ask path | Nothing could reject it, so the demo showed garbage and no recovery. |
| Chaos faults on BigQuery: withheld only for the startup schema read (phase `metadata`) | Withheld for every `SystemQuery` (the brand list, the latest date, the A23 count, `doctor`); a guarded query is faulted. BigQuery faults in a row are capped at 2, below the runner's 3 attempts | The chat must always be able to start, and BigQuery has no sibling to fall back to. |
| The SDK client had no timeout of its own; `retry_options` always set | `LLMGateway.http_options()` sets the client timeout (`models.list()` cannot hang `doctor --live`) and, where the SDK has `HttpRetryOptions`, one attempt. Tested only on google-genai 2.32.0 | Older SDKs never retried, so leaving the option out is the same. |
| `llm.steps`: a listed step with one setting left out got the generic defaults | Each setting left out keeps that step's own default. Names and defaults live in `src/llm_steps.py` (no copy in `config.py`); `config.yaml` shows an example only | `composer: {temperature: 0.5}` silently cut the Composer's cap from 4096 to 2048. |
| `src/llm/__init__.py` imported the gateway at once | The names load on first use. `import src.llm.chaos` / `.budget` / `.limiter` / `.routing` do not load the Google SDK; `src.llm.gateway`, `src.llm.fake` and `from src.llm import LLMGateway` do | The CLI builds a `Chaos` for a flag, and `SafeQueryRunner` clamps a timeout, without paying for the SDK. |
| The call-timeout rule and its 0.1 s floor existed in `safe_runner.py` and `gateway.py` | One `call_timeout()` in `src/llm/budget.py`, used by both | One definition. (The two retry waits keep different jitter on purpose: plan 03 scales by 50% to 100%, this plan says full jitter.) |
| The chaos banner said faults are injected | It says the agent that receives them is not connected yet (plan 07) and nothing is injected in this version | The turn only echoes. Plan 07 changes the text. The metrics row is still stamped `chaos: true` whenever the flag is on (plan 02); filter on it knowing that. |

Things the tests showed, now written down:
- **At the default 10 requests a minute a 20-call "why" turn cannot finish.** The limiter correctly holds the 11th call to the same model for up to a minute, and the turn has 60 seconds. Raise `llm.default_rpm` or `llm.rate_limits` from the AI Studio limits page before judging the budget numbers (NFR9, Q3). Different models have separate windows, so a mix of Flash and Flash-Lite calls is less affected.
- A model that "thinks" spends output tokens on the thoughts. They are added to `tokens_out` (they are billed as output). If a live call returns an empty reply that ended at `MAX_TOKENS`, the gateway asks again with a bigger cap; if that fails too it reports `LLM_BAD_OUTPUT` with that reason, and the fix is `llm.steps.<step>.max_output_tokens`. No `thinking_config` is set: the Gemini 3 setting names are not in our sources, so check them with the live key.
- Falling back to Flash-Lite is visible: `fallback_used` is on the `llm.call` span and in the metrics row.

### Still to do with a Gemini key
1. Put `GEMINI_API_KEY` in `.env`.
2. `python -m src.cli.main doctor --live`. The "Gemini models" line closes A14: if an ID in `llm.models` is not listed, copy the right ones from the fix line into `config.yaml` and write the IDs in `docs/decision.md` (A14, Q10). The "Gemini call" line proves a structured-output call returns parsed JSON; a warning means the sibling answered.
3. Open the AI Studio limits page and set `llm.default_rpm` / `llm.rate_limits` from it (NFR9). Note the numbers in the config comment.
4. Check the pydantic schema is accepted by the live API (some pydantic features are not supported in Gemini's response schema). If one is refused, the call fails as `LLM_UNAVAILABLE` (HTTP 400): simplify the schema in plan 07 or switch `_send` to `response_json_schema`.
5. A short live chaos run once plan 07 is wired: 10 calls with `--chaos harsh`, no unhandled exception. Until then `tests/test_gateway.py::test_a_harsh_run_of_many_calls_ends_every_call_with_a_result_or_a_typed_error` is the offline version.
6. Mark Status as done when 2 to 5 pass.

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
- Plan 07 builds one `TurnBudget` per turn with `TurnBudget.from_settings(settings.budget)`, calls `upgrade()` after the Input Guard, and passes it to every `generate()` call and to `SafeQueryRunner`. It builds one `LLMGateway(settings, chaos=session.chaos_engine)` and one `SafeQueryRunner(settings, chaos=session.chaos_engine)` per chat, calls `chaos.begin_turn()` when it makes a turn's runtime, calls `chaos.maybe_corrupt_sql(sql)` on the SQL writer's output (before the SQL Guard, which still checks it), and passes `prompt=prompt_attrs(template_id, version, text)` to `generate()`. It caps `loop.parallel_subqueries` with `gateway.parallel_limit(...)`. It changes the chaos banner in `cli/main.py` (`banner()`) from "not connected yet" to saying faults are injected, and drops the line in the README that says they only start once the agent is connected.
- Plan 03's `SafeQueryRunner` reads `budget.bytes_remaining` (a property) and calls `charge_bytes(n)` and `check_time()`. `TurnBudget` has them (done).
- Plan 09 uses `fake.py` and the chaos seeds for the resilience tests: `FakeGateway` for graph tests, the real `LLMGateway` over `FakeGenaiClient` plus a seeded `Chaos` for retry, breaker and fallback tests.
- Plan 11 documents the full production chain including Vertex AI.
