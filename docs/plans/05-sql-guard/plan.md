# Plan 05: SQL Guard
Status: built and tested offline (the matrix below, a 200-query fuzz test, and a sabotage check of the main rules). Three things need a Google sign-in or the owner and are NOT done: comparing the allow-list with the live columns (Q4), dry-running the generated SQL on real BigQuery, and the owner's yes on the PII split (Q11) and the customer-row rule (S7) (see "Still to do with a Google sign-in")
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
| 3 | Walk the tree for denied functions and table functions. The deny list starts with `EXTERNAL_QUERY`, `ML.*` and session or system functions, and is built into the code (`ALWAYS_DENIED_FUNCTIONS`); `guards.denied_functions` in config adds names and can never remove one. Everything not on the list is allowed. Also reject wildcard tables. | `DISALLOWED_FUNCTION`, `WILDCARD_TABLE` |
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
src/guards/query.py     GuardedQuery and ArrayParam: no sqlglot, no BigQuery import (the runner imports it)
src/guards/lineage.py   column lineage and rules R1 to R5 on the qualified copy
tests/guard_support.py  users, settings and the analytic query corpus the tests share
tests/test_sql_guard.py  test_scope_rewrite.py  test_allowlist.py  test_guarded_query_origin.py
tests/test_sql_guard_review.py  test_sql_guard_review2.py  test_sql_guard_review3.py  test_guarded_query_seal.py   (regressions from the three code reviews)
tests/test_live_sql_guard.py   (marked live)
```
Also changed: `src/data_access/safe_runner.py` (accepts `GuardedQuery`, turns `ArrayParam` into BigQuery's parameter), `src/config.py` and `config.yaml` (`guards.denied_functions` adds to a built-in floor), `src/cli/main.py` (the chat attaches the allow-list to the catalog at startup), `src/cli/doctor_bigquery.py` (the "Column allow-list" check).

## Tasks
- [ ] Take the live schema from plan 03. Write and review `schema_allowlist.yaml`. Mark everything not yet confirmed by the client. (Written from our knowledge of the dataset and marked as expected content. Comparing it with the live columns needs a sign-in.)
- [x] `allowlist.py`: loader, validation (no column both allowed and blocked), reconcile hook for plan 03.
- [x] `sql_guard.py` steps 0 to 5 and 10, with the rule IDs.
- [x] Lineage with `qualify` on a copy (step 6). Collect output-column lineage and quasi-identifier touches.
- [x] `scope_views.py`: templates for `brands` and `all` modes. Scope rewrite using sqlglot scopes. The post-condition check.
- [x] Customer-row and demographic rules R1 to R5. `__k_users` injection. `LIMIT`.
- [x] `refusals.py`: user-facing text per rule ID.
- [x] Emit `guard.decision` events (plan 02). Fill `rewrites` for the trace.
- [x] Verify on the installed sqlglot: names of set-operation classes and the `qualify` options can differ between versions.

### What differs from the text above (decisions made while building)
| Where | Plan text | Built | Why |
|---|---|---|---|
| Star test | "`SELECT *` from `users` runs" | `SELECT *`, `u.*` and `TO_JSON_STRING(u)` over `users` are refused with `PER_CUSTOMER_ROWS` (R1). Over `products` they run and hold only allowed columns | The row carries `users.id` and the demographic columns, one customer per row. That is exactly what R1 and R4 refuse. A star over a table with no customer data runs. **Owner to confirm this reading.** |
| Step 6 schema | `qualify` with allowed and blocked columns | `qualify` with the allowed columns only | A blocked or unlisted column is then unknown even if the name scan (step 5) were skipped. Step 5 still gives the non-repairable refusal and catches aliases such as `age AS email` |
| Whole-row values | not mentioned | `qualify` turns a table alias used as a value into a `TableColumn`. Lineage treats it as every allowed column of that table | Without this, `TO_JSON_STRING(u)` slipped past R1 and R3 (found by the star tests) |
| Functions | deny list in config, everything else allowed | Also refused: a table function; `@name` and `?` parameters written by the model (`SQL_SYNTAX`, repairable); `@@name` system variables; a dotted call such as `my_ds.my_udf(1)` unless the namespace is built into BigQuery (`SAFE`, `NET`, `HLL_COUNT`, `KLL_QUANTILES`, `AEAD`, `KEYS`) | A function stored in a dataset can read tables the allow-list does not cover. A table function reads whatever it likes |
| Deny list | in config | A floor in the code (`ALWAYS_DENIED_FUNCTIONS`); config adds names and cannot remove one | An emptied or mistyped list in `config.yaml` would otherwise switch off `SESSION_USER` and the rest (code review) |
| Recursion | not mentioned | `WITH RECURSIVE` is `SQL_SYNTAX` (repairable) | The lineage cannot follow a query that reads its own output, and analytics do not need one |
| Set operations in lineage | by column name | by position: names come from the first branch and the other branches line up by position, as in BigQuery | By name, `SELECT a FROM (SELECT 0 AS a, 0 AS b UNION ALL SELECT user_id AS b, 0 AS a FROM orders)` returned customer IDs unflagged (code review) |
| `__k_users` and set operations | injected into every outermost SELECT | only into a single SELECT or a tree of `UNION ALL`. `EXCEPT`, `INTERSECT` and `UNION DISTINCT` compare whole rows, so another column changes the answer: they get no count (`k_column = None`, so the Result Guard withholds) | Code review |
| ID output names | the output column's name | An unaliased customer-ID expression gets the alias `customer_id` in the SQL that runs (BigQuery would call it `f0_`); `id_columns` lists names the query really outputs | The Result Guard replaces only the columns named in `id_columns` (code review) |
| String escapes | not mentioned | An escape BigQuery rejects (`'\d+'`) is `SQL_SYNTAX`, repairable; raw strings and legal escapes pass | sqlglot reads `'\d+'` as `'d+'` and would write that back, so a query BigQuery refuses would run and mean something else (code review) |
| `GROUP BY` | not mentioned | Written back as the model wrote it. A custom BigQuery generator leaves out sqlglot's `_alias_ordered_group`, which swaps `GROUP BY <expr>` for the SELECT alias | If the alias is also a column of the table, the swap groups by the raw column (code review) |
| Columns named with their table's path | not mentioned | `dataset.orders.col` and the backticked full path are rewritten to the view's alias, in the query that runs and in the analysed copy | Once the table is a view, only `orders.col` resolves (code review) |
| Which nodes are the views | a mark in node metadata | The nodes `rewrite` returned, by identity. Metadata that came from `/* sqlglot.meta k=v */` comments is also cleared after parsing | The model could write that comment, and one such comment renamed a function in the generated text (code review) |
| Error details | short text | No detail repeats the model's table path or a column name that is not a plain name | A value the user typed can land in a name position, and the log mask does not catch every kind (code review) |
| sqlglot version | `>=25.0` | `>=30.24,<31`, and the guard checks at construction that sqlglot has the argument names it reads and builds the BigQuery generator the way it expects | With an older sqlglot, `source_names` found nothing and every demographic result would be withheld silently (code review) |
| `reviewed_against_live` | not mentioned | A flag in `schema_allowlist.yaml`, `false` until a person has compared it with the live schema and agreed the PII split. While it is false `doctor` warns and **the chat does not start** (`guards.require_reviewed_allowlist: true`, the default; `false` starts with a warning in the log) | The file was written from our knowledge of the dataset, not read from it (code review, finding 13, and again in the second review). Nothing in the file can prove it right |
| Stored functions | a dotted call checked with a pattern over the generated text | Checked on the tree: the path in front of a call is read from `Dot` and `Anonymous` nodes. Any call with a path whose first part is not a BigQuery namespace is refused | A project with a hyphen in its name (`` `my-project`.ds.f(1) ``) did not match the pattern, so it was never checked. The text also came out upper-cased, naming a different function (second review) |
| `SELECT DISTINCT` and `__k_users` | counted as a plain SELECT | A `SELECT DISTINCT` gets no count: another column changes which rows are duplicates | Second review |
| Malformed expressions | `INTERNAL` | `SQL_SYNTAX` (repairable) when sqlglot parses an expression and then cannot write it back, or the rewrite cannot be read back. Not counted as a guard block | `FORMAT_DATE(, x)` is the model's typo; counted as a block it also inflated the alert (second review) |
| The `GuardedQuery` | any instance | **Sealed.** `SQLGuard.check` seals every field with a key that exists only in its process (`GuardedQuery.issue`); the runner refuses a query whose seal no longer matches, so `dataclasses.replace(gq, sql=...)`, a hand-made instance and a query from another process are all refused. A copy, a deepcopy and a pickle inside the process stay valid. The class cannot be subclassed | `isinstance` alone was not proof that the guard ran, and the test that scans for `GuardedQuery(` could not see `replace` (second review). **For plan 07:** keep the query inside the process that checked it, or check it again after a restart |
| The BigQuery generator | a copy of sqlglot's list of SELECT helpers | Built from sqlglot's own list with `_alias_ordered_group` removed; the guard stops at startup if the list is not where it expects. `requirements.txt` pins `sqlglot>=30.24,<31` | A hand-copied list silently misses what a later sqlglot adds (second review) |
| Config pattern | `$` | `fullmatch` | `$` also matches before a trailing newline, and a YAML block scalar ends in one, so a configured denial could silently do nothing (second review) |
| Group size (`__k_users`) | the first source with a customer ID | The smaller of the distinct-customer counts of **every** source that holds customers (`LEAST(COUNT(DISTINCT a.id), COUNT(DISTINCT b.user_id))`). If a source that supplies demographic data has no ID to count, no count: the Result Guard withholds | `FROM order_items oi CROSS JOIN users u GROUP BY u.state` counted the buyers of the whole join as the size of every state, so a state with one customer looked like a group of thousands (third review) |
| Collecting aggregates | any aggregate counts as aggregating | `ARRAY_AGG`, `STRING_AGG`, `ANY_VALUE`, `MAX_BY`/`MIN_BY`, `APPROX_TOP_COUNT` and the like over a quasi-identifier make the query row-level (R4) | One row with every customer's age and city is not an aggregate, whatever k says (third review) |
| Grouping step then a plain SELECT | `DEMOGRAPHIC_ROW_LEVEL` violation | A repairable `SQL_SYNTAX` that says to group and return in one SELECT with window functions, when the outermost SELECT reads only derived tables and one of them groups. A real violation otherwise (a base table is read, or the group collects values) | "Revenue share by country" through a CTE is an ordinary shape, and a violation is never retried. The data is not row-level; only the Result Guard's need for the size next to the groups is unmet. **Owner may prefer a refusal, or carrying the size through the CTE** |
| `users.created_at` | allowed, not classified | A quasi-identifier | It is different for nearly every customer, so it worked as a customer key that R1, R2 and the fake IDs never saw (third review). Still our default for Q11 |
| `SAFE.` | a built-in namespace | A prefix only: `SAFE.name` and `SAFE.NS.name` pass, `SAFE.dataset.function` is a stored function | Third review |
| Parenthesised joins | refused (`TABLE_NOT_ALLOWED`) | Run: the joins that hang on the table move to its view; `source_names` looks inside the parentheses | Valid BigQuery, and the refusal named the wrong reason (third review) |
| The function check | rendered every call with its arguments | Reads names from the tree (`sql_name`, the path), then once over the tokens of the text that will run, so a function the generator names differently (`GENERATE_UUID`) is still caught by a configured denial | Quadratic in nesting depth (third review) |
| Lineage cost | not specified | Memoised (whole-row references too) and capped at 200,000 steps; over the cap is a repairable `SQL_SYNTAX` | A chain of CTEs that each read the one before twice took four times as long every two levels: 12 s at depth 18 (third review) |
| `GuardedQuery.authentic` | not specified | Never raises: a seal or field that cannot be compared or written out is not authentic | The runner raised a bare `TypeError`, skipping the `UNGUARDED_QUERY` count (third review) |
| The generator canary | not specified | After building the BigQuery generator the guard writes a probe query and checks its `GROUP BY` came out as written, whatever the mechanism | The list of sqlglot helpers is read from a closure, which is internal; the probe checks the behaviour that matters (third review) |
| The span `guard.sql` | not in plan 02 | Added to plan 02's span table, where later plans look for names | Third review |
| Lineage sources | tables and queries | Also any other table-like source, such as `UNNEST(...)`. The analysis copy keeps the dataset on every table (`dataset.table`) | Two holes found by testing the guard with hostile queries, both fixed and pinned by tests: `UNNEST((SELECT ARRAY_AGG(user_id) FROM orders))` returned raw customer IDs, and a CTE called `users` made the lineage follow the CTE's constant instead of the real `thelook_ecommerce.users` |
| Table options | not mentioned | `FOR SYSTEM_TIME`, `TABLESAMPLE`, `PIVOT`, hints and an alias column list on a base table are refused (`TABLE_NOT_ALLOWED`) | The rewrite replaces the table with a view and would drop them silently |
| Table names | exact | matched without regard to case, then rewritten to the real name | BigQuery would refuse `Orders`. The rewrite targets the allow-listed table either way, so nothing is widened |
| Ranked list (R2) | group by id, aggregate, `ORDER BY`, limit at most 50 | Also: no `OFFSET` (paging walks the whole list), no `ROLLUP`, `CUBE` or grouping sets. A missing or larger `LIMIT` is clamped to 50, as the LIMIT section says | Enumeration 50 rows at a time |
| `__k_users` | fixed alias | the alias grows an underscore if the query already uses the name. `SELECT AS STRUCT/VALUE` gets none, so `k_column = None` | The Result Guard drops the column by name |
| Empty or comment-only input | not specified | `SQL_SYNTAX`, repairable | No statement was written |
| `params` | `list[QueryParam]` | `tuple[ArrayParam, ...]`, our own frozen type. `SafeQueryRunner` turns it into `bigquery.ArrayQueryParameter` | Nothing outside `data_access/` may import the BigQuery library |
| Generated text | "from the tree" | Also read back and checked again (step 10): one SELECT, no blocked name, every table spelled out in full, the right number of views, `@brands` exactly when needed. Function names come out in upper case and a one-unit interval as `INTERVAL 30 DAY` | sqlglot would write `INTERVAL '30' DAY`, which BigQuery's documentation does not show. The live dry-run test is the proof |
| Trace | `guard.decision` events | A `guard.sql` span (kind `guard`) around every check holds the event. Only blocks count in `guard_blocks`; a model mistake (`SQL_SYNTAX`, `SQL_UNKNOWN_COLUMN`) is no decision. A crash inside the guard is a block with rule `GUARD_ERROR` | Plan 02 names the event, not a span |

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

Status of the acceptance: the matrix and the fuzz test pass offline (`pytest tests/test_sql_guard.py tests/test_scope_rewrite.py tests/test_allowlist.py tests/test_guarded_query_origin.py`). The fuzz test has teeth: it fails when the scope rewrite skips a table, when `LIMIT` handling is removed, and when whole-row lineage is removed. Switching off only the name scan (step 5) does not fail the fuzz test, because the allowed-columns-only schema makes the blocked column unknown (a second layer); `test_pii_columns_are_blocked_through_every_clause_alias_quote_and_case` pins step 5 on its own.

**A code review, then fixes.** A `/code-review` run found thirteen problems: three bypasses of the customer-ID rules (set operations matched by name, recursive queries, unnamed ID outputs), one silent wrong answer (the customer count inside `EXCEPT`/`INTERSECT`), illegal escapes rewritten into legal ones, `GROUP BY` swapped for an alias, columns named with a table path, a forgeable view marker, details that repeated typed text, a deny list that config could empty, an old sqlglot floor, a flag nothing set, and a test helper that undid more than it should. Each was reproduced first, fixed, and pinned in `tests/test_sql_guard_review.py`; sabotaging each fix makes its test fail. The review reached 12 of 13 fully; the 13th (the allow-list was not read from the live schema) cannot be done offline and is enforced by the `reviewed_against_live` flag.

**A second `/code-review` run** found eleven more: stored functions with a hyphenated project slipping past the namespace rule, a count added to `SELECT DISTINCT`, malformed expressions reported as internal errors, a `GuardedQuery` that `dataclasses.replace` could forge, a hand-copied sqlglot helper list, a config pattern that accepted a trailing newline, the unreviewed allow-list (again), and cleanups (a test that pinned the unreviewed flag and would break the day it is set, tests duplicated across files, unused methods, a table list kept in three places, a second `ScopeMode`, a live test that checked nothing live). Each was reproduced first, fixed and pinned (`tests/test_sql_guard_review2.py`, `tests/test_guarded_query_seal.py`, `tests/test_allowlist.py`), and sabotaging each fix makes its test fail. A **third** found thirteen more: the group size taken from the wrong source, collecting aggregates counted as aggregating, a lineage cost that doubled per CTE level, an ordinary grouping shape refused with no repair, the sign-up timestamp as a customer key, `SAFE.` as a namespace, parenthesised joins, `authentic` that could raise, a refusal text that promised what was switched off, a quadratic function check, mismatched version messages, a brittle generator mechanism, and an undocumented span. Each was reproduced first, fixed, and pinned in `tests/test_sql_guard_review3.py`. Reproducing them also showed that one of the second review's examples, `FORMAT_DATE(, x)`, only failed because of the per-call rendering that the third review removed; sqlglot writes it as `FORMAT_DATE(x)` and BigQuery refuses that at the dry run.

Fixing the first of the second review's findings introduced a bug that an existing test caught at once: for a typed function sqlglot's `.name` is its first argument, so `NET.HOST('https://a.test/x')` briefly looked like a dotted path.

**No independent review by another agent has been done.** A second agent was asked to try to break the guard; a safety classifier stopped it before it reported anything, so that run counts for nothing. The only adversarial checks are the author's own: the sabotage runs above and about 200 hostile queries (`UNNEST`, whole rows, CTEs and aliases named like tables, set operations, star modifiers, deep nesting), which found and closed the two holes in the table above. A person should read `src/guards/` and try to break it before this is relied on.

Known limits. The residual risks in the plan stay (complementary suppression, decision 9.1). sqlglot does not parse every valid BigQuery query: `CHANGES`, `APPENDS` and `EXTERNAL_OBJECT_TRANSFORM` fail to parse and are refused as `SQL_SYNTAX`. The text sqlglot generates differs from what the model wrote in small ways (`DATE '2024-01-01'` becomes `CAST('2024-01-01' AS DATE)`, `NOT x IN` for `x NOT IN`). All the ones checked are equal in BigQuery, and the live dry-run test is the check for the rest. None of this opens any data.

### Still to do with a Google sign-in
1. `gcloud auth application-default login`, set `GOOGLE_CLOUD_PROJECT` in `.env`.
2. `python -m src.cli.main doctor --refresh`. The **Column allow-list** line fails if an allowed column is not live and warns about live columns in neither list. Fix `schema_allowlist.yaml` until it is green (Q4). Keep the PII split as our default until the client answers Q11.
3. `pytest -m live -s tests/test_live_sql_guard.py`. It dry-runs the 23 analytic queries from `tests/guard_support.py` for a brand manager (with the most frequent brand in the data) and for the CEO. Free. A failure shows which spelling BigQuery refuses: fix it in `generate_sql` in `src/guards/sql_guard.py` and keep the query in the corpus. If many fail, fall back to decision 3.5: the dry run parses and sqlglot only rewrites.
4. Read the printed byte counts. The views add joins, and the plan expects them to stay far below the cap.
5. Ask the owner to confirm S7 (rules R1 to R3) and the star reading above.
6. When 2 to 5 are done, set `reviewed_against_live: true` in `schema_allowlist.yaml`. Until then `doctor` warns and the chat refuses to start (set `guards.require_reviewed_allowlist: false` in `config.yaml` to start anyway, with a warning in the log). Mark Status as done at the same time.

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
- Plan 03's `SafeQueryRunner` runs only the types in `safe_runner.ACCEPTED_QUERY_TYPES`, now `(SystemQuery, GuardedQuery)`. Nothing else may be added; a query of any other type is refused with rule `UNGUARDED_QUERY`. Only `SQLGuard.check` makes a `GuardedQuery` (`GuardedQuery.issue`; `tests/test_guarded_query_origin.py` scans `src/` for any other), and the runner also refuses one whose seal does not match its fields (`src/guards/query.py`).
- Plan 03's `SchemaCatalog.with_policy(allowlist)` reconciles the file with the live schema. `Allowlist` needs `table_names()`, `allowed(table)`, `blocked(table)` and `describe(table, column)`, and an unknown table has no allowed or blocked columns. Done: the chat calls `with_policy` at startup (`open_session`), so a missing or invalid allow-list, or an allowed column that is not live, stops it with a clear message, and `ChatSession.catalog` already has the policy attached. `overview()` and `prune()` refuse to list anything before that.
- Plan 06 reads `id_columns`, `per_customer`, `demographic` and `k_column`.
- Plan 07 passes `scope_label` to the Composer, uses `fingerprint` for loop stops, shows the refusal text (`err.user_message`, keyed by `err.rule_id`), and gives the SQL writer a short rule list. Build the guard once per session with `SQLGuard(settings, load_allowlist(settings))`, call `guard.check(sql, user)` for each query, then `SafeQueryRunner.run(gq, budget)`. A `GUARD_VIOLATION` is never retried. `SQL_SYNTAX` and `SQL_UNKNOWN_COLUMN` are repairable, and their `detail` names the position or the column. The SQL-writer prompt should say: one SELECT; the four tables; write values into the query, no `@parameters`; keep demographic grouping in one SELECT; use a ranked list (`GROUP BY` the customer, `ORDER BY`, `LIMIT`) for top customers.
- Plan 09 extends the matrix and adds the attack prompts.
