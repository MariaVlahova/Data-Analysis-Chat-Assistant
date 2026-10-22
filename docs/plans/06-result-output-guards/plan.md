# Plan 06: Result and Output Guards
Status: not started
Estimate: about 1 h (our estimate) · Depends on: 01, 02 (`pii.py`), 05 (query tags) · Blocks: 07

**Goal:** Mask PII in result rows before the model sees them, replace real customer IDs with fixed fake IDs per session, hide groups below k = 10 except for a ranked-spend-list exception — still an open rule per decision.md's own verdict (see PRD F15/A3) — that never joins demographic columns, cap rows at 50, and scan the final answer for PII before it is shown.

Tags and `(impl)` marks are explained in plan 01.

## Requirements covered
| ID | Requirement | Source |
|---|---|---|
| F14 | PII is masked in result rows before the model sees them. Each real customer ID becomes a fixed fake ID for the session (`Customer #48213`). | PRD 6.2 |
| F15 | Groups smaller than k = 10 are hidden. Rows sent to the model are capped (50, an example in the source). Ranked spend lists are the open exception. | PRD 6.2 |
| F16 | The final answer is scanned for PII before it is shown | PRD 6.2 |
| NFR11 | PII in the final output: zero | [DECIDED] |
| NFR13, NFR14 | k = 10; 50 rows | [PROPOSED] |
| S7, S9, S15 | Customer-row rule; no streaming before the scan; label customer totals | decision 9.2 |

## Decisions that apply
- 3.4 [DECIDED]: Result Guard "masks PII columns and values. Replaces `users.id` with a fake ID per session. Hides groups below k = 10. Caps rows (example: 50)". Output Guard: "pattern scan in prototype, Cloud Sensitive Data Protection in production". Masking before the model stops it from repeating PII and keeps PII away from the Gemini API.
- Customer IDs (owner decision, 2026-10-22): each `users.id` maps to a fixed fake ID for the session, for example `Customer #48213`. The same customer keeps the same fake ID for the whole session. Real IDs, names, emails and addresses never reach Gemini or the user. Rejected: showing the real `users.id` as "Customer #id".
- A3: a fake ID per session is not PII. The agent can still answer "top customers".
- A2 / Q11 [OPEN]: direct identifiers are blocked at the SQL Guard. Other attributes appear only in groups of 10 or more.
- A21: the dataset is assumed to have no free-text PII, so value scanning is a safety net, not the main defense. If A21 is wrong, value scanning becomes critical.
- 3.10: raw rows never enter the graph state.
- S9 [Medium]: streaming text before the Output Guard could show PII. Stream only progress until the scan passes.
- Q29 default: stream progress only.
- Result Guard note (requirements folder), five parts: A column-level masking, B value-level pattern scan, C pseudonymization, D row cap with a note, E **fail closed** on any internal error.

## Scope
- In: Result Guard (parts A to E, plus group-size suppression), session pseudonymizer, guarded summaries, notices, Output Guard, extra output patterns, alerts, tests.
- Out: SQL checks (plan 05), the numeric grounding check and the Composer (plan 07), Cloud Sensitive Data Protection and Model Armor (production only, plan 11).

## Interfaces
```python
class SessionPseudonymizer:                 # one per CLI session; lives in memory only
    def fake_id(self, real_id) -> str       # "Customer #48213", stable for the session, unique per real ID

class ResultGuard:
    def __init__(self, settings, allowlist, pseudonymizer)
    def apply(self, df: pandas.DataFrame, gq: GuardedQuery, total_rows: int) -> GuardedResult  # raises RESULT_GUARD_FAILED

@dataclass
class GuardedResult:
    columns: list[str]
    rows: list[dict]                 # at most 50, already masked. Safe for the model and the state.
    total_rows: int                  # after suppression, before the cap
    suppressed_groups: int
    truncated: bool
    masked_columns: list[str]
    notices: list[str]               # plain sentences the Composer must pass on
    status: Literal["ok", "empty", "withheld"]
    summary: dict                    # row count, columns, per numeric column min/max/sum, null counts. Never rows.

class OutputGuard:
    def scan(self, text: str) -> list[Finding]
    def check(self, text: str) -> str          # returns text or raises OUTPUT_BLOCKED
```

## Design
### Where the Result Guard runs (3.10, F14)
Plan 07 calls `SafeQueryRunner.run()` and `ResultGuard.apply()` **inside the same node function**. The raw DataFrame is a local variable of that function and is never returned, stored in state or put in a span. Only `GuardedResult` leaves.

The `SessionPseudonymizer` holds real IDs, so it must not be placed in graph state, in LangGraph `config`, or in a trace. Plan 07 injects it into the node through a closure.

### Result Guard steps (in order)
1. **Fail-closed wrapper.** The whole `apply()` runs in a `try`. Any exception, odd data type or timeout drops the payload and raises `RESULT_GUARD_FAILED`. No partial or unverified data goes through. (Result Guard note, part E)
2. **A. Column masking.** Take the output column names (after the user's aliases). A name that matches the sensitive registry (blocked names from the allow-list plus patterns such as `email`, `phone`, `address`, `first_name`, `last_name`, `latitude`, `longitude`, `ssn`) has all its values replaced by `[MASKED]` and is listed in `masked_columns`. With plan 05's column projection this should never fire; it is the safety net for an alias or a schema mistake.
3. **B. Value scan.** For each string cell, run `pii.scan`. Matches are replaced with the typed tokens (`[EMAIL_MASKED]`, `[PHONE_MASKED]`, `[ID_MASKED]`, `[CARD_MASKED]`). Non-scalar cells (arrays, structs) are turned into a JSON string and scanned. A cell type we cannot handle fails closed (step 1).
4. **C. Pseudonymize.** Every column named in `gq.id_columns` (plus any column whose name looks like a customer ID) has each value replaced by `pseudonymizer.fake_id(value)`. The whole cell is replaced, so a derived string such as `CONCAT('c', id)` cannot leak the real ID.
5. **Small groups (S7, F15).** Uses the tags from plan 05:

| `gq` tags | Action |
|---|---|
| `demographic` and `k_column` set | Remove rows where `k_column < guards.k_anonymity`. Count them in `suppressed_groups`. Drop the `k_column`. |
| `demographic` and `k_column` is `None` | Group sizes cannot be verified. **Withhold the result** (`status = "withheld"`, no rows) and add the notice. Fail closed. |
| `per_customer` (ranked list) | Group size does not apply. Add the notice "Customer numbers are labels for this session only." |
| neither | No suppression. Product, brand and time groups are not groups of people. |

6. **D. Cap.** Keep the first `guards.rows_to_model` rows (50) after suppression. If rows were cut, set `truncated` and add the notice "Showing top 50 of N rows." (N is the count after suppression.)
7. **Summary.** Build `summary` from the guarded data only. Plan 07 uses it for the Evidence Check and for replans: "missing list plus guarded summaries, never rows" (diagram, step 4b).
8. **Notices.** Examples: "3 groups with fewer than 10 customers were hidden." "Group sizes could not be checked, so the result was withheld." "Showing top 50 of 1,240 rows." These are rendered into the final answer by code (plan 07), so the answer never implies a complete list when it is not.
9. Emit `guard.decision` (outcome `pass`, `rewrite` or `block`) with counts only: columns masked, cells masked, groups hidden, rows cut. No values.

### Session pseudonymizer (owner decision; A3)
- `fake_id(real)`: if the real ID is known, return its label. Otherwise draw a 5-digit number from a session-seeded random generator, retry on a collision, and store it. Uniqueness is guaranteed by a lookup, not by chance. (impl; a 5-digit space plus a birthday collision is why we check.)
- The label is random, not derived from the real ID, so it cannot be reversed or matched across sessions.
- The map lives in memory for the process. A new CLI run gets new labels. After a restart, "tell me more about Customer #48213" cannot be resolved. The model never had the real ID, so it cannot write that SQL. The prototype says so honestly. (impl)

### Output Guard (F16, NFR11)
- Runs on the final composed text, after the numeric check, before anything is printed.
- Patterns: everything in `pii.py` (email, phone, national or tax ID, card) plus output-only patterns added here: street address (number, street word, suffix such as `St`, `Ave`, `Road`), latitude and longitude pairs, and a postal code next to a city word. The allowed fake-ID format `Customer #ddddd` is never flagged.
- Phone and ID patterns must not fire on money and counts such as `1,234,567`, `$12,345.67`, `2,450` or `10.5%`. Tune against a fixture made of real answer text from the golden cases, because a false positive blocks a good answer. Record false-positive counts.
- On a finding: **block the whole answer**, do not just cut the match (we cannot be sure what surrounds it). The user sees: "I held back this answer because it may contain personal data. Please rephrase, or ask for totals instead. Trace ID: …". Raise `OUTPUT_BLOCKED`, set `pii_at_output` in the metrics row and trigger the alert line (plan 02). A block here means an earlier layer failed, so it is an incident to look at, not a normal event.
- **S9:** nothing from the Composer is printed before this scan. Only progress text streams. The CLI prints the answer once, after `check()` returns.
- Production: Cloud Sensitive Data Protection and Model Armor replace the regex scan (decision §8). Plan 11 describes this.

### Not in this plan
- The numeric grounding check, because it needs the plan and the sub-query results (plan 07).
- The Input Guard (plan 07).

## Files
```text
src/guards/result_guard.py  pseudonymizer.py  output_guard.py
src/pii.py   (extended with output-only patterns)
tests/test_result_guard.py  test_pseudonymizer.py  test_output_guard.py
```

## Tasks
- [ ] `pseudonymizer.py` with the uniqueness lookup.
- [ ] `result_guard.py`: wrapper, column masking, value scan, pseudonymize, suppression table, cap, summary, notices, spans.
- [ ] Extend `pii.py` with address and coordinate patterns. Keep one pattern list.
- [ ] `output_guard.py`: scan, block, message, alert, metrics counter.
- [ ] Build the false-positive fixture of money, count and percent strings.
- [ ] Document the notice texts so plan 07 can render them.

## Tests and acceptance
- Pseudonymizer: same real ID gives the same label; different IDs never collide across 10,000 draws; a new instance gives different labels; labels cannot be turned back into IDs.
- Column masking: a column aliased `email` is `[MASKED]`; a normal column is untouched.
- Value scan: an email inside a free-text cell becomes `[EMAIL_MASKED]`; arrays and structs are scanned; an unknown cell type raises `RESULT_GUARD_FAILED`.
- Customer IDs: `id_columns` values become fake IDs; a derived string column that holds the ID is replaced as a whole cell; the same customer has the same label across two different results in one session.
- Suppression: 9 customers hidden, 10 shown (boundary); mixed result hides only the small rows; `k_column = None` with `demographic` withholds everything; ranked list is not suppressed; product-level groups are not suppressed.
- Cap: 1,240 rows give 50 rows, `truncated`, and the right notice. Suppression happens before the cap.
- Fail closed: a forced exception inside any step returns no rows and the typed error. Nothing partial is returned.
- Output Guard: each pattern blocks; the money and count fixture passes; the fake ID format passes; a blocked answer is never printed (CLI test); the metrics row has `pii_at_output = 1`.
- **Canary test:** plant `canary@example.test` and a fake street address in raw rows. They must not appear in `GuardedResult`, the graph state, the trace file, the log file or the final output.
- Acceptance: the PRD success metrics "PII leaks 0" and "groups below k = 10 suppressed 100%" hold on the attack set in plan 09.

## Open items and defaults used
| Item | Default used | Who answers |
|---|---|---|
| S7 customer-row rule | Ranked spend lists only, no demographic columns, config switch | Owner |
| Q11 PII split | See plan 05 | OpsFleet client |
| Q27 row cap | 50 | Us |
| Q29 streaming and numeric failure | Stream progress only. Block the whole answer on PII. | Us |
| Complementary suppression | Not handled. Known residual risk. | Us |

## Risks and pitfalls
- A false positive in the Output Guard blocks a correct answer. Tune it on real answers early.
- A forgotten `try` around one step breaks the fail-closed promise. The single wrapper in step 1 is there to prevent that.
- Putting the pseudonymizer or a DataFrame into LangGraph state or config would write real IDs into the checkpoint database. Use closures.
- Hiding one small group but showing a total can reveal the hidden group. Documented, not solved.

## Hand-off
- Plan 07 wires `ResultGuard.apply` into the SQL loop node, renders notices into the answer, runs `OutputGuard.check` last, and uses `summary` for the Evidence Check.
- Plan 09 runs the canary and attack tests against all layers together.
- Plan 11 explains the production replacements (Cloud Sensitive Data Protection, Model Armor, BigQuery policy tags).
