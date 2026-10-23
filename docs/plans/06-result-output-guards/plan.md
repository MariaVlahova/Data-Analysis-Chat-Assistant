# Plan 06: Result and Output Guards
Status: built and tested offline. Nothing calls the guards yet: plan 07 wires them in. No live step is left. The Output Guard's false-positive fixture is hand-written text, not real answers (see "Still to do").
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
- [x] `pseudonymizer.py` with the uniqueness lookup.
- [x] `result_guard.py`: wrapper, column masking, value scan, pseudonymize, suppression table, cap, summary, notices, spans.
- [x] Extend `pii.py` with address and coordinate patterns. Keep one pattern list.
- [x] `output_guard.py`: scan, block, message, alert, metrics counter.
- [x] Build the false-positive fixture of money, count and percent strings. (Hand-written: 43 strings, 0 false positives, including report headings such as `3 Recommended Actions To Drive Revenue`. Not real answers. The test keeps a floor of 30 strings, not an exact count, so adding real answers never needs an edit of the assertion.)
- [x] Document the notice texts so plan 07 can render them. (Next section.)

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

## Notice texts (for plan 07)
`GuardedResult.notices` holds finished sentences. Plan 07 renders them into the answer as they are. The texts live in `src/guards/result_guard.py`, so a test can import them.

| When | Notice |
|---|---|
| a column was masked | `1 column was hidden because it held personal data.` / `N columns were hidden because they held personal data.` |
| a pattern changed a text value | `Some personal details inside text values were hidden.` |
| groups below k were hidden | `3 groups with fewer than 10 customers were hidden.` / `1 group with fewer than 10 customers was hidden.` |
| `demographic` with no `k_column` (status `withheld`) | `Group sizes could not be checked, so the result was withheld.` |
| a ranked customer list | `Customer numbers are labels for this session only.` |
| a ranked list for a brand manager (S15) | `Each customer's spend counts only purchases in your brands.` |
| rows were cut | `Showing top 50 of 1,240 rows.` (N is after suppression, before the cap; the first number is what was kept) |
| every row seen was hidden, and the runner had cut the frame | `4,998 more rows were not checked, so this result may be incomplete.` (after the "groups were hidden" notice) |

An empty result has no notice. If every group was hidden the status is `empty` and the "groups were hidden" notice is the only explanation, so the Composer must not say "no data found". When the runner had cut the frame, the rows past the cut were never looked at, so the "not checked" notice follows it: "all groups were hidden" alone would say the result is complete.

## What was decided while building (impl)
- **One pattern list, one switch.** `pii.scan(text, extended=True)` adds the street address, coordinate and postal-code patterns (tokens `[ADDRESS_MASKED]`, `[COORDS_MASKED]`, `[POSTAL_MASKED]`). The tracer and the log filter keep the short list. The Result Guard scans cell values with the long list too, so an address inside a text cell is masked before the model sees it.
- **The Result Guard checks the query is the SQL Guard's own** (`GuardedQuery.authentic`). A look-alike or a changed copy could say `id_columns=()` and let real IDs through, so it fails closed.
- **An empty frame is `empty`, not `withheld`**, even for a demographic query without a group-size column: there is nothing to hide, and `empty` lets the loop widen the search.
- **Fail closed on inconsistency.** A ranked list whose ID column is not in the result, a group-size column that is missing or not a whole number, a repeated column name that cannot be told apart, nesting deeper than 8 levels, a cell type we do not handle (bytes, durations, anything else), a text cell over 20,000 characters, an array or struct over 2,000 values and a run over 5 seconds all raise `RESULT_GUARD_FAILED`. The clock is read between rows, between cells and after the summary. A regex cannot be interrupted, so the size limits are what bound it, and every pattern has to be linear by itself (`tests/test_pii.py` tries each opening with long runs of whitespace).
- **The tracing calls are inside the same wrapper.** If recording the decision fails, the caller still gets `RESULT_GUARD_FAILED` and no rows, not an arbitrary exception that plan 07 would report as INTERNAL.
- **Column names are compared by WORDS.** A name is split at separators and camelCase, and the words are compared with a vocabulary: `First Name`, `last-name`, `E-mail`, `firstName`, `postcode`, `customer_name`, `user_name`, `street`, `mobile` and `forename` are sensitive, while `smartphone_revenue`, `headphones_sold`, `addressable_market`, `mobile_orders` and `brand_name` are metrics. (The first version looked for letters inside the name, which hid metrics and missed `postcode`.) Compound words such as `useremail` and `customername` are caught by suffix; `phone` has no suffix rule on purpose (`smartphone`). A header that holds a pattern (a PIVOT names columns after values) is cleaned in `columns`, in the row keys and in the summary.
- **Names that end in `number`, `num` or `no` are not customer IDs.** `buyers_number` is a count, and a count must not become `Customer #21257`. Only `id`, `ids` and `key` after a customer word, plus what the SQL Guard declares in `id_columns`.
- **Strings inside an array or struct are scanned as the text they are,** before the JSON is written, and the JSON text is scanned too. JSON writes a line break as the two characters backslash and n, which no pattern reads across.
- **The "only your brands" notice** compares `GuardedQuery.scope_label` with `ALL_BRANDS_LABEL` from `src/guards/query.py`, the same constant the SQL Guard writes.
- **The group-size column never leaves the guard.** The summary is built from the rows that survived suppression, before the cap, so the Evidence Check can compare a total over all of them while the model sees 50.
- **Rows past the runner's cut are counted, not seen.** If `total_rows` is more than the frame, the difference is added to the total. Suppression cannot be applied to rows we never saw.
- **Customer ID columns are found three ways:** the SQL Guard's `id_columns`, the allow-list's customer ID column names, and names like `user_id` or `customer_no`. A bare `id` is only an ID when the SQL Guard says so.
- **The pseudonymizer keeps no real ID.** The lookup key is a keyed hash with a salt that lives in the object. The label space widens from 5 digits when half full, up to 7, and then stops (fail closed). `copy` and `pickle` raise, so a checkpoint write fails loudly.
- **The Output Guard blocks on the first finding and does not store the answer it held back, not even masked.** A pattern cannot mask a name or an unlabelled real ID, and `/trace` shows a user their own turns, so the held-back answer would be one command away. The `guard.output` span keeps the count, the kinds and where each finding was (`email@41-60`). Every finding counts in `pii_at_output`. A scan that crashes, an answer that is not text, and a failure while recording the result are all `OUTPUT_BLOCKED` too (one wrapper, like the Result Guard), and the user still gets the usual message when a finding was made.
- **The patterns are tuned for precision, because one hit blocks the whole answer and a report with action items is the core output.** Address: a house number (`42`, `221B`, `12-14`, `5/42`), one to four capitalized name words, a street word; the shortest reading wins; a `#`, `$`, `%` or `1,` before the number rules it out (money, a label); a name made of business nouns or the little words of a sentence (`Brands`, `To`, `For`) is not a street. Street words that are also English (`Drive`, `Way`, `Place`, `Court`, `Lane`, `Circle`, `Square`, `Trail`, `Alley`) count only at the end of the phrase (a comma, bracket, full stop or line end follows). Postal code: `City, ST 12345` with a real US state, `City ON K1A 0B1` with a real province, `City, SW1A 1AA`, or a code after "zip code" / "postal code" / "postcode". Coordinates: a pair needs four decimals each and either five decimals or a value of ten or more (`34.0522, -118.2437`, not `0.0543, 0.0612`). Every pattern is linear in the input and `scan` merges overlaps in one pass.
- **The fake-ID exemption is bounded.** `Customer #` followed by 5 to 7 digits is a label; `Customer #4111111111111111` is a card number with a prefix and is still blocked. The widths come from the pseudonymizer's own constants.
- **Spans:** `guard.result` and `guard.output` (kind `guard`), each with one `guard.decision`. Rule IDs: `COLUMN_MASKED`, `VALUE_MASKED`, `IDS_REPLACED`, `GROUPS_HIDDEN`, `ROWS_CAPPED` (outcome `rewrite`), `GROUP_SIZE_UNKNOWN`, `PII_AT_OUTPUT`, `GUARD_ERROR` (outcome `block`).

## Still to do
- **Run the Output Guard on real answers.** The fixture (`tests/test_output_guard.py`) is hand-written. Add real answer text from the golden cases once plans 07 and 09 produce it, and record the false-positive count again. A false positive blocks a good answer.
- **Known gaps of the pattern scan** (production replaces it with Cloud Sensitive Data Protection, plan 11): no pattern finds a person's name; a postcode on its own, without a place or a label (`SW1A 1AA`), is not found, and nor is a postal code in another country's format; a street name in lower case (`123 main st`) is not found, and neither is a format that puts the street word first (`Rue de Rivoli 5`, `Hauptstraße 5`); an address followed by a word when its street word is also English (`42 Wallaby Way via courier`) is not found; coordinates of two small numbers with four decimals (Accra, `5.6037, -0.1870`) are not found. Where a pattern errs the other way: a product written as a number, a capitalized word and a street word (`2 Park Place`) is read as an address, and only the nouns in `_NOT_STREET_NAMES` are excluded from street names, so a title-case phrase that ends in a plain street word and uses another noun can still be blocked. `tests/test_pii.py` keeps every one of these as an example, so a change in either direction is a decision.
- **Complementary suppression** is not handled (unchanged from the table below).

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
