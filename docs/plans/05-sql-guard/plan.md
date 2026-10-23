# Plan 05: SQL Guard
Status: not started
Estimate: about 1.5 h (our estimate) · Depends on: 01, 03 (live schema) · Blocks: 06, 07

**Goal:** Check every generated query on its sqlglot parse tree, against an approved table-and-column allow-list, so that only one SELECT or WITH runs, PII columns are blocked even through aliases, CONCAT or SELECT *, the user's brand filter is forced in, and a LIMIT is always added.

Tags and `(impl)` marks are explained in plan 01.

## Requirements covered
| ID | Requirement | Source |
|---|---|---|
| F11 | Code adds the brand filter to every query. The model cannot remove it. | PRD 6.2 |
| F12 | Only one `SELECT` or `WITH` runs. Only the four approved tables. A `LIMIT` is always added. | PRD 6.2, decision 3.5 |
| F13 | PII columns are blocked in SQL, also through aliases, `CONCAT` and `SELECT *` | PRD 6.2 |
| F17 (part) | Injection in user text or data does not change the rules, because code enforces them | PRD 6.2 |
| F32 | A guard violation is never retried. The agent explains the refusal. (This plan raises it. Plan 07 acts on it.) | PRD 6.4 |
| NFR16 | The model is never the only checker | [PROPOSED] |
| S7, S14, S15 | Customer-row rule; empty brand list; "spend on your brands" label | decision 9.2 |

## Decisions that apply
- 3.4 [DECIDED]: SQL Guard enforces allowed tables and columns, blocks PII columns (also through aliases, `CONCAT` and `SELECT *`) and adds the brand filter. The prompt only informs the model; nothing relies on it.
- 3.5 [PROPOSED]: order is structured output, **sqlglot parse, checks, scope rewrite**, dry-run, run with a byte cap. The guard checks the parse tree, not the text. Rejected: keyword block lists, asking the model to check its own safety, the read-only role alone. Checks: one `SELECT` or `WITH`, approved tables only, PII columns, forced `LIMIT`. Revisit if sqlglot fails on many valid queries (then the dry-run parses and sqlglot only rewrites).
- A1 / Q41: the brand filter applies **per order item**. Order totals cover only the user's brands, and answers say so.
- A2 / Q11 [OPEN]: the guard blocks direct identifiers. Other attributes appear only in groups of 10 or more (the Result Guard enforces the group size). The real `users.id` is not blocked; the Result Guard masks it (A3).
- A23: every order item joins to `products.brand`, so the brand filter covers all data.
- S14: an empty brand list is no access. The CEO has the explicit all-brands flag.
- S7 [High, rule still open]: "top customers" returns groups of one, so k = 10 would hide them all. Suggested rule: allowed only for ranked spend lists, never with demographic columns. We build it behind a config switch.
- 3.11: a guard violation gets no retry.
- 3.10: "same SQL twice" stops a loop, so this plan returns a fingerprint.

## Scope
- In: parse checks, allow-list, blocked-column scan, scope rewrite, `LIMIT`, tags for the Result Guard, the typed errors, unit tests.
- Out: dry-run and execution (plan 03), masking and k-suppression of rows (plan 06), the SQL-writer prompt (plan 07).
- Out (production): BigQuery authorized views and policy tags. The inline views below are the prototype stand-in for exactly these (decision §8).

## Interfaces
```python
class SQLGuard:
    def __init__(self, settings, allowlist: Allowlist)
    def check(self, sql: str, user: UserContext) -> GuardedQuery   # raises AgentError

@dataclass(frozen=True)
class GuardedQuery:
    sql: str                         # rewritten, generated from the tree, never the original text
    params: list[QueryParam]         # e.g. ArrayParam("brands", STRING, [...]); empty for the CEO
    tables: frozenset[str]
    id_columns: tuple[str, ...]      # output columns that carry customer IDs (for the Result Guard)
    per_customer: bool               # a ranked per-customer list
    demographic: bool                # touches a quasi-identifier column anywhere
    k_column: str | None             # alias of the injected distinct-customer count, or None
    limit: int
    rewrites: tuple[str, ...]        # rule IDs applied, for the trace
    fingerprint: str                 # hash of the normalized original tree, for "same SQL twice"
    scope_label: str                 # "your brands: A, B" or "all brands", for the Composer
```
Errors: `GUARD_VIOLATION` (carries a rule ID and a safe explanation), `NO_ACCESS`, and the repairable `SQL_SYNTAX` and `SQL_UNKNOWN_COLUMN`.

## Design
### The allow-list (`schema_allowlist.yaml`)
An allow-list, not a block list: a column that is not listed does not exist for the agent. A new column that appears upstream stays invisible until a human classifies it (fail closed). The file below is our **expected** content from our knowledge of `thelook_ecommerce`. Plan 03 dumps the live schema first (Q4) and this file is corrected to match. The owner reviews it, because the PII split is Q11 [OPEN].

```yaml
dataset: bigquery-public-data.thelook_ecommerce
customer_id_columns: [users.id, orders.user_id, order_items.user_id]
tables:
  users:
    allowed: [id, age, gender, state, city, country, traffic_source, created_at]
    quasi_identifiers: [age, gender, state, city, country, traffic_source]
    blocked: [first_name, last_name, email, street_address, postal_code, latitude, longitude, user_geom]
  orders:
    allowed: [order_id, user_id, status, gender, created_at, shipped_at, delivered_at, returned_at, num_of_item]
    quasi_identifiers: [gender]
  order_items:
    allowed: [id, order_id, user_id, product_id, inventory_item_id, status, created_at, shipped_at, delivered_at, returned_at, sale_price]
  products:
    allowed: [id, cost, category, name, brand, retail_price, department, sku, distribution_center_id]
descriptions: {...}   # one line per allowed column, used by plan 03's schema overview
```
- "Direct identifiers" (blocked everywhere): name, email, address, postal code, coordinates. "Quasi-identifiers" (allowed only inside aggregates of 10 or more customers): age, gender, state, city, country, traffic source. This is the Q11 default, not a client answer.
- `orders.num_of_item` counts all items in an order, including other brands. The scoped view recomputes it over in-scope items only (below).

### Check pipeline (in order; the first failure stops)
| Step | What it does | Failure |
|---|---|---|
| 0 | `user.scope_mode == none` | `NO_ACCESS` |
| 1 | `sqlglot.parse(sql, read="bigquery")`. A parse error is the model's syntax mistake. | `SQL_SYNTAX` (repairable) |
| 2 | Exactly one statement. Root is a `SELECT`, a set operation of selects, or a `WITH` that ends in one. No DDL, DML, scripting, `EXECUTE IMMEDIATE`, `CREATE TEMP FUNCTION`. | `GUARD_VIOLATION`: `MULTI_STATEMENT`, `NOT_SELECT` |
| 3 | Walk the tree for denied functions and table functions. The deny list is in config and starts with `EXTERNAL_QUERY`, `ML.*` and session or system functions. Everything not on the list is allowed. Also reject wildcard tables. | `DISALLOWED_FUNCTION`, `WILDCARD_TABLE` |
| 4 | **Table check using sqlglot scopes** (not string matching). Every base-table source must be one of the four tables in the configured dataset. Accepted spellings: `orders`, `thelook_ecommerce.orders`, `` `bigquery-public-data.thelook_ecommerce.orders` ``. Other dataset, other project, `INFORMATION_SCHEMA` → violation. A CTE that has the same name as a table is a CTE, not a base table (scopes tell them apart). | `TABLE_NOT_ALLOWED` |
| 5 | **Blocked-name scan.** Any column or identifier in any clause whose name is in the union of the `blocked` lists (case-insensitive, quoted or not) → violation. This covers `SELECT email`, `CONCAT(first_name, ...)`, `WHERE email LIKE ...`, `GROUP BY`, `ORDER BY`, and an alias created from a blocked column. | `PII_COLUMN` |
| 6 | **Lineage on a copy.** Run `sqlglot.optimizer.qualify` with the full schema (allowed and blocked) so every column resolves to a source table and column. If qualify fails, the query is not run: return a repairable error. No lineage metadata, no execution. | `SQL_UNKNOWN_COLUMN` (repairable) |
| 7 | **Customer-row and demographic rules** (next section). | `PER_CUSTOMER_ROWS`, `DEMOGRAPHIC_ROW_LEVEL` |
| 8 | **Scope rewrite** (next section). | `SCOPE_REWRITE_INCOMPLETE` (fail closed) |
| 9 | Inject the distinct-customer count when needed. Force the `LIMIT`. | |
| 10 | Generate SQL text from the tree. Compute `fingerprint`. Emit `guard.decision` events with the rule IDs. | |

Rule IDs are stable strings. The refusal text shown to the user comes from a table keyed by rule ID (plain words, no SQL detail, and it never repeats a blocked column list). Plan 07 shows it.

Because the executed text is regenerated from the tree, comment tricks, odd whitespace and keyword splitting never reach BigQuery.

### Scope rewrite: inline "authorized views" (F11, 3.4)
Every base-table source (found via scopes in step 4) is replaced by a subquery with the **same alias**, or with the table name as alias when the user gave none, so references like `orders.status` still resolve. Each subquery projects **only the allowed columns**. This means:
- `SELECT *` and `u.*` expand to allowed columns only. There is nothing else to expand to.
- `TO_JSON_STRING(u)` or selecting a whole row can only contain allowed columns.
- The brand filter is inside every view, so subqueries, CTEs, `UNION` branches, self-joins and scalar subqueries all get it. The model cannot reach unscoped data by any query shape.

For a brand manager (`scope_mode == brands`), with `@brands` as an array query parameter (the brand names are never pasted into the SQL text):

```sql
-- products
SELECT p.id, p.cost, p.category, p.name, p.brand, p.retail_price, p.department, p.sku, p.distribution_center_id
FROM `ds.products` p WHERE p.brand IN UNNEST(@brands)

-- order_items (per-item scope, A1)
SELECT oi.id, oi.order_id, oi.user_id, oi.product_id, oi.inventory_item_id, oi.status, oi.created_at,
       oi.shipped_at, oi.delivered_at, oi.returned_at, oi.sale_price
FROM `ds.order_items` oi JOIN `ds.products` p ON oi.product_id = p.id
WHERE p.brand IN UNNEST(@brands)

-- orders: only orders that contain an in-scope item, with num_of_item recomputed (Q41)
SELECT o.order_id, o.user_id, o.status, o.gender, o.created_at, o.shipped_at, o.delivered_at, o.returned_at, s.num_of_item
FROM `ds.orders` o
JOIN (SELECT oi.order_id, COUNT(*) AS num_of_item
      FROM `ds.order_items` oi JOIN `ds.products` p ON oi.product_id = p.id
      WHERE p.brand IN UNNEST(@brands) GROUP BY oi.order_id) s ON s.order_id = o.order_id

-- users: only customers who bought an in-scope item
SELECT u.id, u.age, u.gender, u.state, u.city, u.country, u.traffic_source, u.created_at
FROM `ds.users` u
WHERE u.id IN (SELECT oi.user_id FROM `ds.order_items` oi JOIN `ds.products` p ON oi.product_id = p.id
               WHERE p.brand IN UNNEST(@brands))
```
The templates use uncorrelated joins and `IN (subquery)`, not correlated subqueries, because BigQuery limits correlated subqueries. (impl)

For the CEO (`scope_mode == all`) the same views are used with **no brand predicate** and no `@brands` parameter. The CEO still gets the column projection, so the CEO never sees PII either.

- The views are parsed from templates with sqlglot and inserted after the base-table sources are collected, so the views' own table references are not rewritten again.
- **Post-condition (fail closed):** after the rewrite, walk the tree. Every base-table reference must sit inside a node marked `scope_view` (kept in node metadata, not in the SQL text). Any other reference raises `SCOPE_REWRITE_INCOMPLETE`.
- Mixed orders: totals cover only the user's brands. Customer totals are "spend on your brands" (S15). The guard returns `scope_label` so the Composer can say so.

### Customer rows and demographics (S7, F15 support) [PROPOSED rule, still open]
Definitions: a *customer-ID output* is any output column of the outermost `SELECT` whose lineage includes a `customer_id_columns` entry outside `COUNT`, `COUNT(DISTINCT)` and `APPROX_COUNT_DISTINCT`. A *demographic touch* is any quasi-identifier column in any clause anywhere in the query.

| Rule | Condition | Result |
|---|---|---|
| R1 | A customer-ID output exists and the query is **not** a ranked spend list | `PER_CUSTOMER_ROWS` violation |
| R2 | Ranked spend list: outermost select groups by the customer ID, has at least one aggregate, has `ORDER BY`, and has a `LIMIT` of at most `guards.rows_to_model` (the guard clamps it to 50) | Allowed. `per_customer = True`. Output column names go into `id_columns`. |
| R3 | A customer-ID output and a demographic touch in the same query | `PER_CUSTOMER_ROWS` violation (never demographics next to per-customer rows) |
| R4 | A demographic touch and the outermost select is not aggregated (no `GROUP BY`, no aggregate) | `DEMOGRAPHIC_ROW_LEVEL` violation |
| R5 | A demographic touch and the outermost select is aggregated | Allowed. `demographic = True`. Inject `COUNT(DISTINCT <customer id>) AS __k_users` into the outermost select when a customer-ID column is directly in its sources, and set `k_column`. If none is reachable (for example the ids were dropped inside a CTE), set `k_column = None`. The Result Guard then withholds the result because group sizes cannot be verified. |
- `guards.allow_ranked_customer_lists: false` turns R2 off. Then every customer-ID output is a violation.
- The SQL-writer prompt (plan 07) tells the model to keep demographic grouping in one `SELECT` and to use ranked lists for "top customers". That is guidance only; the rules above are what enforce it.

### `LIMIT`
- No `LIMIT` on the outermost select: add `bigquery.sql_row_limit` (1000). A larger `LIMIT`: clamp to it. Per-customer ranked lists clamp to `guards.rows_to_model` (50). For a set operation, set the limit on the whole union. Inner limits stay as written.
- The limit is 1000, not 50, so the Result Guard can suppress small groups first and then cap at 50.

### Fingerprint
`sha256` of the original tree after normalization (lowercase keywords, canonical spacing, comments removed). The loop in plan 07 keeps a set of fingerprints per sub-question. The same fingerprint twice stops the loop (3.10, F34).

## Files
```text
schema_allowlist.yaml
src/guards/__init__.py  allowlist.py  sql_guard.py  scope_views.py  refusals.py
tests/test_sql_guard.py  test_scope_rewrite.py  test_allowlist.py
```

## Tasks
- [ ] Take the live schema from plan 03. Write and review `schema_allowlist.yaml`. Mark everything not yet confirmed by the client.
- [ ] `allowlist.py`: loader, validation (no column both allowed and blocked), reconcile hook for plan 03.
- [ ] `sql_guard.py` steps 0 to 5 and 10, with the rule IDs.
- [ ] Lineage with `qualify` on a copy (step 6). Collect output-column lineage and quasi-identifier touches.
- [ ] `scope_views.py`: templates for `brands` and `all` modes. Scope rewrite using sqlglot scopes. The post-condition check.
- [ ] Customer-row and demographic rules R1 to R5. `__k_users` injection. `LIMIT`.
- [ ] `refusals.py`: user-facing text per rule ID.
- [ ] Emit `guard.decision` events (plan 02). Fill `rewrites` for the trace.
- [ ] Verify on the installed sqlglot: names of set-operation classes and the `qualify` options can differ between versions.

## Tests and acceptance
Each case checks the exact error kind and rule ID, or the shape of the rewritten tree. Plan 09 reuses and extends this matrix.

| Group | Cases |
|---|---|
| Valid | one-table aggregate; three-table join; CTE; scalar subquery; `UNION ALL`; window function; backticked fully qualified names; comments inside the SQL |
| Structure | `DROP`, `DELETE`, `INSERT`, `UPDATE`, `MERGE`, `CREATE`; `SELECT 1; DROP TABLE x`; `EXECUTE IMMEDIATE`; empty or comment-only input |
| Tables | other dataset; other project; `INFORMATION_SCHEMA`; wildcard table; `EXTERNAL_QUERY`; CTE named like a base table |
| PII | `SELECT email`; `CONCAT(first_name, ' ', last_name)`; `SELECT email AS e` then `SELECT e` in an outer query; `WHERE email LIKE '%@%'`; `ORDER BY` a blocked column; `EMAIL`, `` `Email` ``; `SELECT latitude, longitude` |
| Star and rows | `SELECT *` from `users` runs and has no blocked column; `u.*`; `TO_JSON_STRING(u)` has no blocked column |
| Scope | every base table in a join, a subquery, a CTE, a `UNION` branch and a self-join is replaced; a model-written `WHERE brand = 'Other'` cannot widen the view; `OR 1=1` cannot widen it; `@brands` is never pasted into the SQL text; the post-condition catches a deliberately skipped table |
| Modes | brand manager gets `@brands`; CEO gets no predicate but still no PII columns; empty-brand user gets `NO_ACCESS` and never an unscoped query (S14) |
| Customer rows | non-ranked `user_id` output → R1; ranked list passes and clamps to 50; ranked list plus `state` → R3; row-level `age` → R4; `GROUP BY state` gets `__k_users`; demographic grouping hidden in a CTE gets `k_column = None` |
| Repairable | unknown column → `SQL_UNKNOWN_COLUMN` (not a violation); parse error → `SQL_SYNTAX` |
| Limit | missing → 1000; `LIMIT 1000000` → 1000; union gets one limit |
| Determinism | same SQL with different whitespace or comments gives the same `fingerprint` |

Acceptance: the whole matrix passes. A fuzz test of 200 mutated queries (case, quoting, comments, aliases) never produces output that contains a blocked column name or an unscoped base-table reference.

## Open items and defaults used
| Item | Default used | Who answers |
|---|---|---|
| Q11 which columns are PII | Direct identifiers blocked. Quasi-identifiers only in aggregates of 10 or more. | OpsFleet client |
| S7 customer-row rule | R1 to R3 above, behind `allow_ranked_customer_lists` | Owner to confirm |
| Q4 real columns | Read live (plan 03) and correct the YAML | Us |
| Q41 per-item brand filter | Yes. Answers say so. | OpsFleet client |

## Risks and pitfalls
- sqlglot may fail on valid BigQuery syntax. Keep a list of failing queries. If many appear, switch to the 3.5 fallback: the dry-run parses and sqlglot only rewrites.
- A name-based blocked scan can flag a harmless alias that happens to be called `email`. That is acceptable: it fails closed, and the refusal is explained.
- Filter-based isolation of one person (for example a `WHERE` on three demographic columns) is why R5 counts customers after the filters. Complementary suppression (a total that reveals a hidden group) is a known residual risk (decision 9.1, "small segments stay a risk").
- The inline views add joins to every query. Check the dry-run bytes on a few real queries. The tables are small, so this should stay far under the cap.

## Hand-off
- Plan 03's `SafeQueryRunner` runs only the types in `safe_runner.ACCEPTED_QUERY_TYPES` (now just `SystemQuery`). Add `GuardedQuery` to that tuple, and nothing else; a query of any other type is refused with rule `UNGUARDED_QUERY`.
- Plan 03's `SchemaCatalog.with_policy(allowlist)` reconciles the file with the live schema. `Allowlist` needs `table_names()`, `allowed(table)`, `blocked(table)` and `describe(table, column)`, and an unknown table has no allowed or blocked columns. Call `with_policy` at startup; `overview()` and `prune()` refuse to list anything before it.
- Plan 06 reads `id_columns`, `per_customer`, `demographic` and `k_column`.
- Plan 07 passes `scope_label` to the Composer, uses `fingerprint` for loop stops, shows the refusal text, and gives the SQL writer a short rule list.
- Plan 09 extends the matrix and adds the attack prompts.
