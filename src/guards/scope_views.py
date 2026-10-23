"""Scope rewrite: inline "authorized views" (plan 05, F11, decision 3.4).

Every base-table reference in the model's query is replaced by a subquery that has the same alias and projects only
the allowed columns, filtered to the user's brands. So the model cannot reach unscoped data by any query shape: the
filter sits inside every view, and `SELECT *`, `u.*` and `TO_JSON_STRING(u)` can only ever hold allowed columns.
This is the prototype stand-in for BigQuery authorized views and policy tags (decision §8).

The brand names travel as the array parameter `@brands`, never inside the SQL text. The views use uncorrelated joins
and `IN (subquery)`, because BigQuery limits correlated subqueries. (impl)

Fail closed: `verify` walks the finished tree, and any base table that is not inside one of the views `rewrite` made
raises `SCOPE_REWRITE_INCOMPLETE`. The views are known by identity (the nodes `rewrite` returned), never by a mark on the
node: sqlglot copies `/* sqlglot.meta key=value */` comments from the SQL text into node metadata, so a mark could be
written by the model.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, traverse_scope

from src.guards.allowlist import Allowlist, fold
from src.guards.refusals import violation

BRANDS_PARAM = "brands"
ViewMode = Literal["all", "brands"]


@dataclass(frozen=True)
class Rewritten:
    """What `rewrite` did: the table of each reference, as written in the allow-list, and the view nodes it put in."""

    tables: tuple[str, ...]
    views: tuple[exp.Subquery, ...]


def base_table_refs(root: exp.Expression) -> list[exp.Table]:
    """Every reference to a real table, found with sqlglot scopes and not by name. A CTE or derived table is a
    `Scope` source, so a CTE called `orders` is not a base table, while the `orders` inside that CTE's own body,
    or a dataset-qualified `thelook_ecommerce.orders` next to it, is."""
    seen: set[int] = set()
    refs: list[exp.Table] = []
    for scope in traverse_scope(root):
        for table in scope.tables:
            if id(table) not in seen and not isinstance(scope.sources.get(table.alias_or_name), Scope):
                seen.add(id(table))
                refs.append(table)
    return refs


def _inside(node: exp.Expression, view_ids: set[int]) -> bool:
    current: exp.Expression | None = node
    while current is not None:
        if id(current) in view_ids:
            return True
        current = current.parent
    return False


def strip_table_paths(refs: list[exp.Table]) -> None:
    """A table without an alias can be named in a column by its path (`dataset.orders.order_id`, or the whole path in
    backticks). Once the table is a view with the alias `orders`, only `orders.order_id` resolves. So wherever a column
    names an unaliased table by a longer path, drop the part in front of the table name. Done on the tree that runs and
    on the copy that is analysed, so both read the same."""
    names = {fold(ref.name) for ref in refs if ref.args.get("alias") is None}
    if not names or not refs:
        return
    root = refs[0].root()
    for column in root.find_all(exp.Column):
        if column.args.get("db") is not None and fold(column.table) in names:
            column.set("db", None)
            column.set("catalog", None)


class ScopeViews:
    def __init__(self, allowlist: Allowlist, dataset: str) -> None:
        self._allowlist = allowlist
        self._dataset = dataset
        self._parsed: dict[tuple[str, ViewMode], exp.Select] = {}

    # templates -----------------------------------------------------------------------------------

    def view(self, table: str, mode: ViewMode) -> exp.Select:
        """A fresh copy of the view's SELECT for an approved table (`table` as written in the allow-list)."""
        key = (table, mode)
        if key not in self._parsed:
            parsed = sqlglot.parse_one(self._template(table, mode), read="bigquery")
            if not isinstance(parsed, exp.Select):  # a template that does not parse to a SELECT is a bug, not input
                raise violation("SCOPE_REWRITE_INCOMPLETE", f"the view for {table} is not a SELECT")
            self._parsed[key] = parsed
        return self._parsed[key].copy()

    def _template(self, table: str, mode: ViewMode) -> str:
        """SQL text for one view. Every name in it comes from the validated allow-list or the validated dataset
        setting (letters, digits and underscores), never from the model."""
        def ref(name: str) -> str:
            return f"`{self._dataset}.{name}`"

        def columns(alias: str, replaced: dict[str, str] | None = None) -> str:
            replaced = replaced or {}
            return ", ".join(replaced.get(fold(c), f"{alias}.{c}") for c in self._allowlist.allowed(table))

        brands = f"UNNEST(@{BRANDS_PARAM})"
        items_of_brands = (
            f"{ref('order_items')} AS oi JOIN {ref('products')} AS p ON oi.product_id = p.id WHERE p.brand IN {brands}"
        )
        key = fold(table)
        if mode == "all":
            alias = {"orders": "o", "order_items": "oi", "products": "p", "users": "u"}[key]
            return f"SELECT {columns(alias)} FROM {ref(table)} AS {alias}"
        if key == "products":
            return f"SELECT {columns('p')} FROM {ref(table)} AS p WHERE p.brand IN {brands}"
        if key == "order_items":  # per-item scope (A1)
            return f"SELECT {columns('oi')} FROM {items_of_brands}"
        if key == "orders":  # only orders with an in-scope item, and num_of_item counts the in-scope items only (Q41)
            return (
                f"SELECT {columns('o', {'num_of_item': 's.num_of_item AS num_of_item'})} FROM {ref(table)} AS o "
                f"JOIN (SELECT oi.order_id AS order_id, COUNT(*) AS num_of_item FROM {items_of_brands} GROUP BY oi.order_id) AS s "
                f"ON s.order_id = o.order_id"
            )
        if key == "users":  # only customers who bought an in-scope item
            return f"SELECT {columns('u')} FROM {ref(table)} AS u WHERE u.id IN (SELECT oi.user_id FROM {items_of_brands})"
        raise violation("SCOPE_REWRITE_INCOMPLETE", f"no view is defined for table {table}")

    # rewrite -------------------------------------------------------------------------------------

    def reference_count(self, table: str, mode: ViewMode) -> int:
        """How many base-table references one view holds (the output check counts them)."""
        return len(base_table_refs(self.view(table, mode)))

    def rewrite(self, refs: list[exp.Table], mode: ViewMode) -> Rewritten:
        """Replace each reference in place with its view.

        The references were all collected before the first replacement, so the views' own table references are
        never rewritten again.
        """
        strip_table_paths(refs)
        replaced: list[str] = []
        views: list[exp.Subquery] = []
        for ref in refs:
            table = self._allowlist.table_name(ref.name)
            if table is None:  # step 4 approved every reference, so this means a bug upstream: do not guess
                raise violation("SCOPE_REWRITE_INCOMPLETE", "a table reference was not approved before the rewrite")
            alias = ref.args.get("alias")
            name = alias.this if isinstance(alias, exp.TableAlias) and alias.this is not None else ref.this
            view = exp.Subquery(this=self.view(table, mode), alias=exp.TableAlias(this=name.copy()))
            joins = ref.args.get("joins")
            if joins:  # `FROM (orders o JOIN order_items oi ON ...)`: the joins hang on the table, so they move to the view
                ref.set("joins", None)
                view.set("joins", joins)
            ref.replace(view)
            replaced.append(table)
            views.append(view)
        return Rewritten(tuple(replaced), tuple(views))

    def verify(self, root: exp.Expression, done: Rewritten) -> None:
        """The post-condition: every base table sits inside one of the views `rewrite` made, and all of those views are
        still in the tree. Any doubt, including a tree that cannot be analysed, raises `SCOPE_REWRITE_INCOMPLETE`."""
        view_ids = {id(view) for view in done.views}
        try:
            scopes = traverse_scope(root)
            cte_refs = {
                id(table) for scope in scopes for table in scope.tables
                if isinstance(scope.sources.get(table.alias_or_name), Scope)
            }
            outside = [t for t in root.find_all(exp.Table) if id(t) not in cte_refs and not _inside(t, view_ids)]
            found = sum(1 for node in root.find_all(exp.Subquery) if id(node) in view_ids)
        except Exception:  # noqa: BLE001 - a tree we cannot walk is not a tree we can vouch for
            raise violation("SCOPE_REWRITE_INCOMPLETE", "the rewritten query could not be checked") from None
        if outside:
            raise violation("SCOPE_REWRITE_INCOMPLETE", f"{len(outside)} table reference(s) are outside the scope views")
        if found != len(done.views):
            raise violation("SCOPE_REWRITE_INCOMPLETE", f"expected {len(done.views)} scope view(s), found {found}")

