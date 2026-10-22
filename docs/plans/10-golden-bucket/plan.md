# Plan 10: Golden Bucket (optional)
Status: not started
Estimate: about 0.5 h (our estimate) · Depends on: 03, 05, 06, 07 · Blocks: none (plan 11 documents the design either way)

**Goal:** Define the trio format, seed a small set of brand-tagged trios and a glossary, and retrieve up to 3 similar trios per question above a similarity threshold with in-memory search.

Tags and `(impl)` marks are explained in plan 01.

This plan is **optional**. The client calls a seeded set with retrieval "a nice touch", not required. The design for requirement 1 (how the bucket is updated and how trios are found at query time) is design-only and is written up in plan 11 whether or not this plan is built. If time runs short, cut this plan first and keep the locked definitions that plan 07 already carries in code.

## Requirements covered
| ID | Requirement | Source |
|---|---|---|
| F8 | The agent uses similar past trios (question, SQL, report) as examples (P1, optional) | PRD 6.1 |
| F50 (design) | Trios in Cloud Storage, hybrid search, top 3 above a threshold, brand filtering [PROPOSED], analyst approval, nightly re-check | PRD 6.6 |
| S5 [High] | Search filters trios by table, not by brand. A report excerpt can show other brands' numbers. Tag each trio with its brands and filter on them. | decision 9.2 |
| A15 | Confirmed by the client: the Golden Bucket does not exist. We define the format and seed a small set. | decision §5 |

## Decisions that apply
- Client Q&A (confirmed): the Golden Bucket is "entirely theoretical". Define the trio format yourself (Question, SQL Query, Analyst Report) and seed a sample set. Explain how the bucket is updated and how trios are found at query time. Work with mock data you create; there is no external search service.
- 3.3 [PROPOSED]: Cloud Storage holds trios. Postgres with pgvector holds the index. At query time the system finds up to **3** similar trios (vector plus full-text search). Below a similarity threshold it adds nothing. Prototype: `golden/seed/*.yaml` and `glossary.yaml` with **in-memory search**. Rejected: Vertex AI Vector Search (always-on cost), BigQuery `VECTOR_SEARCH` (seconds of delay), a vector database service (extra vendor). Revisit above about 1M items or across regions.
- 3.3 updates: an analyst reviews each candidate. A nightly job re-checks the SQL. No automatic promotion (3.8). The trio format is ours to define (Q43).
- Q43 default fields: question, SQL, report, notes, tables, brands, status, version.
- Q21 default: filter trios by the user's brands; analyst review, nightly re-check, no automatic promotion.
- Q40 default: ingestion is out of scope for the client, but keep a short Golden update design because requirement 1 asks for it.
- A9: production trios are one per line, up to about 10k, in English; analysts review a few dozen candidates a week.
- Step inputs (requirements folder): the **Planner** sees the golden question and interpretation notes only. The **SQL writer** sees question, notes and golden SQL. The **Composer** sees the golden report excerpt only.
- Cold start (decision 9.1): with no trios on day 1, answers are weaker. Seeds, the glossary and locked definitions reduce this.

## Scope
- In: trio schema and validation, about 10 seed trios, glossary seeds, in-memory index, brand and status filters, per-step views, the Context Assembly hook, a `golden validate` command (the Ingest Job in miniature), tests.
- Out (design-only, plan 11): Cloud Storage bucket, Eventarc, the production Ingest Job, pgvector and full-text hybrid search, the candidate queue, nightly re-check job, failure mining.
- Out: embeddings in the prototype (see below).

## Trio format (`golden/seed/*.yaml`, one trio per file)
```yaml
id: g-003
version: 1
status: approved              # draft | approved | retired. Only approved trios are searchable.
question: "Which product categories drive the most revenue?"
interpretation_notes: "Revenue means the sum of item sale price, excluding cancelled and returned items. Categories come from products.category."
sql: |
  SELECT p.category, SUM(oi.sale_price) AS revenue
  FROM order_items oi JOIN products p ON oi.product_id = p.id
  WHERE oi.status NOT IN ('Cancelled', 'Returned')
  GROUP BY p.category ORDER BY revenue DESC LIMIT 10
report: |
  Analyst report text: structure and wording the analysts use. Contains no names, no emails and, for brand-agnostic trios, no brand names or brand-specific figures.
tables: [order_items, products]
brands: ["*"]                 # "*" = brand-agnostic. Otherwise the brands whose data the trio shows.
reviewed_by: seed-author      # mock analyst name
created_at: 2026-10-22
```
- `id` unique; `version` increases when an analyst edits a trio; `status` moves draft → approved → retired.
- Seed set: about 10 trios covering revenue over time, top customers (ranked list, no demographics), product comparison, category revenue, state comparison, returns, customer segments, a report with action items, plus **two brand-specific trios** and one `draft` and one `retired` to exercise the filters.
- **Keep seed questions different from the golden-case questions** in plan 09, otherwise retrieval makes those tests trivially pass.
- Seed SQL must pass the SQL Guard. Seed reports must pass the Output Guard.

## Glossary (`glossary.yaml`)
```yaml
- term: churn
  aliases: [churned customers, churn rate]
  definition: "A customer is churned when they have no order in the last 90 days."
- term: last month
  definition: "The previous full calendar month in UTC, relative to the reference date."
```
Seeds, all [PROPOSED] and needing owner review (Q6): churn (90 days, matches A5), last month, revenue (sum of sale price excluding cancelled and returned items), active customer, average order value, repeat customer, top customers, underspending (spend per customer below the comparison group's average, and the answer states this reading), return rate. Locked definitions for churn and last month already live in code (plan 07); the glossary extends them and does not replace them.

## Design
### Retrieval (3.3, prototype stand-in)
- Tokenize `question + interpretation_notes + tables` of each trio: lowercase alphanumerics, small English stop list, no stemming. Build a TF-IDF vector per trio at load.
- Score a query by cosine similarity, so the score is between 0 and 1. Return the top `golden.top_k` (3) with score at least `golden.min_similarity`. Below the threshold: return nothing (3.3: adds nothing).
- Pick the threshold on the seed set. Use a handful of paraphrased questions that should match and a handful of unrelated ones that should not, and write the chosen value and the probes into the test. Start at 0.25. (impl)
- Why not embeddings in the prototype: they cost Gemini calls on a rate-limited free tier, and the seed set is tiny. The `Retriever` interface is the same, so a vector or hybrid retriever (the production design) swaps in without touching the graph.

### Brand and status filters (S5, Q21)
A trio is eligible for a user when its `status` is `approved` **and** one of:
- `brands == ["*"]` (brand-agnostic), or
- every brand in `brands` is in the user's brands, or
- the user has the all-brands flag.

The filter runs **before** scoring and applies to the whole trio, so no part of a trio from another brand ever reaches a prompt. This is stricter than filtering by table.

### Per-step views
`Trio.view(step)` returns only what that step may see: `planner` → question and notes; `sql` → question, notes, SQL; `composer` → an excerpt of the report (first 600 characters). Plan 07 calls the right view for each prompt.

### Hook in `context_assembly` (plan 07)
`deps.golden` is optional. When it is `None` or `golden.enabled` is false, retrieval returns an empty list. Retrieved text is wrapped in `<untrusted_data source="golden_trio" id="g-003">…</untrusted_data>` and the glossary entries used are added the same way. The trace records a `golden.retrieve` span with trio IDs and scores only, never their text.

### `golden validate` (the Ingest Job in miniature)
`python -m src.cli.main golden validate [path]` runs the checks the production Ingest Job would run (validate, dry-run, DLP; "embed" becomes "build the index"):

| Check | How |
|---|---|
| Schema | All fields present and typed. `id` unique. `status` valid. |
| SQL | Passes the `SQLGuard` under a validation user with all-brands scope. Then a BigQuery **dry-run** through `SafeQueryRunner` (free) stays under the byte cap. |
| Report | `OutputGuard.scan` finds no PII. Warn on instruction-like phrases such as "ignore previous". |
| Brands | Every brand in `brands` exists in the real brand list (plan 03). |
| Tables | Only the four allowed tables. |
| Duplicates | Warn when two trios have nearly the same question. |

Prints a pass or fail line per trio. A failing trio is not loaded. This makes the update path safe and visible without building ingestion, which the client says another team owns.

## Design-only parts (written in plan 11; summarized here so the plan is complete)
- **Update loop:** analysts put approved trios into a Cloud Storage bucket. Eventarc triggers the Ingest Job (validate, dry-run, Cloud Sensitive Data Protection scan, embed). It writes the Golden Index (Postgres with pgvector, hybrid vector plus full-text).
- **Candidates:** thumbs-up answers, saved reports and good runs become **candidate** trios with PII removed. They enter a candidate queue (where it lives is [NOT SPECIFIED]). An analyst approves each one. A nightly job re-runs the SQL and flags trios that break after a schema change. No automatic promotion. Weekly failure mining proposes glossary entries and test cases.
- **Query time:** the Input Guard intent and the standalone question go to hybrid search; filter by brand and status; top 3 above the threshold; each pipeline step receives its own view.
- **Open:** Q16 (where the glossary and candidate queue live), Q19 (who builds and labels), Q40 (whether ingestion is in the client's scope).

## Files
```text
glossary.yaml
golden/seed/g-001.yaml … g-010.yaml
src/golden/__init__.py  schema.py  index.py  glossary.py  validate.py
tests/test_golden_schema.py  test_golden_index.py  test_golden_filters.py  test_golden_validate.py
```

## Tasks
- [ ] `schema.py`: pydantic model for the trio. Loader that skips invalid files with a clear message.
- [ ] Write about 10 seed trios and the glossary. Run `golden validate` on them.
- [ ] `index.py`: TF-IDF build, cosine search, eligibility filter, per-step views.
- [ ] `glossary.py`: term and alias lookup.
- [ ] Wire `deps.golden` into `context_assembly`. Add the `golden.retrieve` span.
- [ ] Tune `min_similarity` on probe questions. Record the value and the probes in a test.
- [ ] `golden validate` command.
- [ ] Optional check: run the golden cases with the index on and off and compare scores (plan 09).

## Tests and acceptance
- A paraphrase of a seed question returns that trio at rank 1; an unrelated question returns nothing.
- Only `approved` trios are returned; `draft` and `retired` never are.
- **Brand filter (S5):** a user with brand A never gets a trio tagged only with brand B, in any view; the CEO gets all; brand-agnostic trios go to everyone.
- Per-step views: the Planner view has no SQL and no report; the SQL view has no report; the Composer view has no SQL.
- Prompt wrapping: retrieved text is inside `untrusted_data` tags.
- Validation: a bad SQL trio, a trio with an email in the report, an unknown brand, and a missing field each fail with a clear reason; a good trio passes.
- With `golden.enabled: false` the graph runs unchanged.
- Acceptance: `golden validate` is green on the seed set, and a live question close to a seed trio shows the trio ID in `/trace`.

## Open items and defaults used
| Item | Default used | Who answers |
|---|---|---|
| Q43 trio fields | question, SQL, report, notes, tables, brands, status, version (plus id, reviewed_by, created_at) | Us |
| Q21 filter by brand | Yes | Us |
| Q40 does "ingestion out of scope" cover the Ingest Job | No. Keep a short update design. | OpsFleet client |
| Q16 where glossary and candidate queue live | Prototype: YAML files. Production: Postgres. | Us |
| Q6 glossary definitions | Seeds above, flagged for review | OpsFleet client |

## Risks and pitfalls
- A brand-agnostic report excerpt that still contains a specific number can leak another brand's figures (S5). Seed authors write such reports qualitatively, and `validate` warns on numbers in `["*"]` trios. (impl)
- Retrieval can mislead the SQL writer when a trio is close but wrong. The SQL Guard and the numeric check still apply, and the trio is only an example.
- Overlap between seeds and golden cases hides real quality. Keep them apart.

## Hand-off
- Plan 07's `context_assembly` calls `GoldenIndex.search` and `Glossary.lookup` and passes the right view to each prompt.
- Plan 11 writes the Golden update design and the production retrieval design from the "Design-only parts" section.
