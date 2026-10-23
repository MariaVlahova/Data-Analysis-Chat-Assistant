"""Column lineage for the SQL Guard's customer-row and demographic rules (plan 05, S7, step 6).

The input is a query that `sqlglot.optimizer.qualify` has already processed on a copy: every column carries its
source alias and every `*` is expanded. Lineage follows an output column back through derived tables, CTEs and set
operations to the base columns it was built from, for example `t.cid` -> `order_items.user_id`.

Two questions are answered, both from the allow-list's own column sets:
- which output columns of the outermost SELECT hold a customer ID (outside a COUNT), and is that a ranked spend list;
- does the query touch a quasi-identifier column anywhere, in any clause.

Anything the walk cannot place is treated as touching the sensitive column of the same name. A wrong answer here
only ever refuses a query, never lets one through.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlglot import exp
from sqlglot.optimizer.scope import Scope, build_scope

from src.guards.allowlist import Allowlist, fold

BaseColumn = tuple[str, str]  # (table, column), both folded

# A COUNT does not carry the ID out of the query, only a number. So `COUNT(DISTINCT user_id)` is not a customer-ID
# output. (A plain COUNT of ids still touches the column; that is the demographic pass, which does not skip them.)
COUNTING = (exp.Count, exp.ApproxDistinct)

# After `qualify`, a table alias used as a value (`TO_JSON_STRING(u)`, `SELECT u`) is a TableColumn: the whole row.
# Older sqlglot versions leave it as a bare Column, which `resolve` also handles.
ROW_REFERENCE: tuple[type, ...] = tuple(c for c in (getattr(exp, "TableColumn", None),) if c is not None)

# Aggregates that hand individual values back instead of reducing them to a statistic: ARRAY_AGG lists every value,
# STRING_AGG joins them, ANY_VALUE and MAX_BY/MIN_BY return one customer's value, APPROX_TOP_COUNT returns the rare
# values with their counts. A query that collects customer attributes this way is row-level data in one row.
COLLECTING: tuple[type, ...] = tuple(
    getattr(exp, name) for name in (
        "ArrayAgg", "ArrayConcatAgg", "ArrayUniqueAgg", "GroupConcat", "AnyValue", "ArgMax", "ArgMin",
        "ApproxTopK", "ApproxTopSum", "JSONArrayAgg", "JSONObjectAgg",
    ) if hasattr(exp, name)
)

# A guard on the work, not on the answer: lineage is memoised, so a real query needs a few thousand steps. A query that
# needs this many is built to make the analysis slow, and is refused as too complex.
MAX_LINEAGE_STEPS = 200_000


class LineageTooComplex(ValueError):
    """The query needs more analysis than any question about the store should."""


@dataclass(frozen=True)
class Analysis:
    id_outputs: tuple[str, ...]  # names of the output columns that hold a customer ID (as the qualified copy names them)
    id_positions: tuple[int, ...]  # their positions in the outermost SELECT's list, so the rewrite can name them for real
    ranked_list: bool  # those outputs form a ranked per-customer spend list (rule R2)
    demographic: bool  # a quasi-identifier column is touched anywhere
    aggregated: bool  # every outermost SELECT reduces its rows, and none collects customer attributes
    collects: bool  # a collecting aggregate (ARRAY_AGG ...) is built from a quasi-identifier column
    grouped_below: bool  # the outermost SELECTs only read derived tables, and one of those groups: the shape to rewrite
    # per outermost SELECT: the (source alias, column, supplies demographic data) triples to count distinct customers in, or
    # None when they cannot be counted (a source with demographic data has no customer ID). One is COUNT; several are the
    # smallest of the counts.
    k_sources: tuple[tuple[tuple[str, str, bool], ...] | None, ...]


def unwrap(node: exp.Expression) -> exp.Expression:
    """Drop the parentheses around a query: `((SELECT ...))` is the SELECT."""
    while isinstance(node, exp.Subquery) and node.this is not None:
        node = node.this
    return node


def outer_selects(node: exp.Expression) -> list[exp.Select]:
    """The SELECTs that produce a query's output rows: the branches of a set operation, or the one SELECT."""
    node = unwrap(node)
    if isinstance(node, exp.Select):
        return [node]
    if isinstance(node, exp.SetOperation):
        return outer_selects(node.this) + outer_selects(node.expression)
    return []


def unalias(node: exp.Expression) -> exp.Expression:
    return node.this if isinstance(node, exp.Alias) else node


def has_aggregate(select_expression: exp.Expression) -> bool:
    """Any aggregate that works on this SELECT's own rows: not inside a window function or a nested query."""
    stop = (exp.Window, exp.Subquery, exp.Select)
    return any(isinstance(n, exp.AggFunc) for n in select_expression.walk(prune=lambda n: isinstance(n, stop)))


def reduces(select_expression: exp.Expression) -> bool:
    """An aggregate that turns rows into a statistic (SUM, COUNT, AVG, MIN, MAX ...), as opposed to a collecting one."""
    stop = (exp.Window, exp.Subquery, exp.Select)
    found = (n for n in select_expression.walk(prune=lambda n: isinstance(n, stop)) if isinstance(n, exp.AggFunc))
    return any(not isinstance(n, COLLECTING) for n in found)


def is_aggregated(select: exp.Select) -> bool:
    if select.args.get("group") or select.args.get("having"):
        return True
    return any(reduces(projection) for projection in select.expressions)


def source_names(select: exp.Select) -> set[str]:
    """The aliases a SELECT's FROM and JOIN items can be referred to by, including the ones inside a parenthesised join:
    in `FROM (orders o JOIN order_items oi ON ...)` both `o` and `oi` are sources of the SELECT."""
    names: set[str] = set()

    def collect(item: exp.Expression | None) -> None:
        if item is None:
            return
        if item.alias_or_name:
            names.add(fold(item.alias_or_name))
        for join in item.args.get("joins") or []:
            collect(join.this)
        inner = item.this if isinstance(item, exp.Subquery) else None
        if isinstance(inner, (exp.Table, exp.Subquery)):  # a parenthesised join; a derived table holds a SELECT instead
            collect(inner)

    from_ = select.args.get("from_")
    collect(from_.this if from_ is not None else None)
    for join in select.args.get("joins") or []:
        collect(join.this)
    return names


class _Lineage:
    def __init__(self, root: exp.Expression, allowlist: Allowlist) -> None:
        scope = build_scope(root)
        if scope is None:
            raise ValueError("the query has no scope to analyse")
        self.allow = allowlist
        self.root_scope = scope
        self.scope_by_expression = {id(s.expression): s for s in scope.traverse()}
        self._memo: dict[tuple, frozenset[BaseColumn]] = {}
        self._busy: set[tuple] = set()
        self._cycles = 0  # how many times the walk met a query it was already inside
        self._steps = 0

    def _cached(self, key: tuple, compute) -> frozenset[BaseColumn]:
        """Work something out once per key. A result worked out while the walk was inside a cycle may be partial, so it
        is returned but not kept. (Without this a chain of CTEs that each read the one before twice took twice as
        long per level.)"""
        if key in self._memo:
            return self._memo[key]
        if key in self._busy:
            self._cycles += 1
            return frozenset()
        self._busy.add(key)
        cycles_before = self._cycles
        try:
            result = frozenset(compute())
        finally:
            self._busy.discard(key)
        if self._cycles == cycles_before:
            self._memo[key] = result
        return result

    # scopes --------------------------------------------------------------------------------------

    def scope_of(self, node: exp.Expression) -> Scope:
        current = node.parent
        while current is not None:
            scope = self.scope_by_expression.get(id(current))
            if scope is not None:
                return scope
            current = current.parent
        return self.root_scope

    def scope_for(self, select: exp.Select) -> Scope:
        scope = self.scope_by_expression.get(id(select))
        if scope is None:
            raise ValueError("a SELECT has no scope")
        return scope

    @staticmethod
    def _source(scope: Scope | None, name: str) -> exp.Expression | Scope | None:
        while scope is not None:  # a correlated subquery reads the sources of the query around it
            source = scope.sources.get(name)
            if source is not None:
                return source
            scope = scope.parent
        return None

    # what a node is built from ------------------------------------------------------------------

    def sources_of(self, node: exp.Expression, skip_counts: bool) -> frozenset[BaseColumn]:
        """Base columns that `node` (an output expression, or any part of the query) is built from."""
        prune = (lambda n: isinstance(n, COUNTING)) if skip_counts else None
        found: set[BaseColumn] = set()
        for part in node.walk(prune=prune):
            self._steps += 1
            if self._steps > MAX_LINEAGE_STEPS:
                raise LineageTooComplex("the query needs too much analysis")
            if isinstance(part, ROW_REFERENCE):
                found |= self._whole_row(self._source(self.scope_of(part), part.name), skip_counts)
            elif isinstance(part, exp.Column):
                found |= self.resolve(part, self.scope_of(part), skip_counts)
            elif isinstance(part, exp.Star) and not isinstance(part.parent, (exp.Column, *COUNTING)):
                found |= self._whole_scope(self.scope_of(part), skip_counts)  # a `*` that qualify could not expand
        return frozenset(found)

    def resolve(self, column: exp.Column, scope: Scope, skip_counts: bool) -> frozenset[BaseColumn]:
        table, name = column.table, column.name
        if isinstance(column.this, exp.Star):  # `t.*`
            return self._whole_row(self._source(scope, table), skip_counts) if table else self._whole_scope(scope, skip_counts)
        if table:
            return self._from_source(self._source(scope, table), name, skip_counts)
        folded = fold(name)
        if any(fold(p.alias) == folded for p in scope.expression.expressions if isinstance(p, exp.Alias)):
            return frozenset()  # a reference to a SELECT alias: the aliased expression is analysed where it is written
        whole_row = self._source(scope, name)  # BigQuery lets a table alias stand for the whole row
        if whole_row is not None:
            return self._whole_row(whole_row, skip_counts)
        return self._by_name(folded)

    def _from_source(self, source: exp.Expression | Scope | None, name: str, skip_counts: bool) -> frozenset[BaseColumn]:
        if isinstance(source, exp.Table):
            table = self.allow.table_name(source.name)
            return frozenset({(fold(table), fold(name))}) if table else frozenset()
        if isinstance(source, Scope):
            return self._output(source, fold(name), skip_counts)
        if source is not None and not isinstance(source, exp.Table):  # a source that is not a table or a scope: look inside it
            return self.sources_of(source, skip_counts)
        return frozenset()  # a struct field, or a name that is no source at all

    def _output(self, scope: Scope, name: str, skip_counts: bool) -> frozenset[BaseColumn]:
        return self._cached(("column", id(scope), name, skip_counts), lambda: self._output_now(scope, name, skip_counts))

    def _output_now(self, scope: Scope, name: str, skip_counts: bool) -> set[BaseColumn]:
        found: set[BaseColumn] = set()
        branches = outer_selects(scope.expression)
        if branches:
            # A set operation names its columns after the FIRST branch and lines the others up by position, so the
            # column called `name` is the same position in every branch, whatever the other branches call theirs.
            names = [fold(p.alias_or_name) for p in branches[0].expressions]
            positions = [i for i, n in enumerate(names) if n == name] or list(range(len(names)))  # unknown name: all of them
            for branch in branches:
                projections = branch.expressions
                chosen = positions if len(projections) == len(names) else range(len(projections))  # uneven widths: look at all
                for i in chosen:
                    found |= self.sources_of(projections[i], skip_counts)
        else:  # not a query but a table-like source, such as UNNEST(...): it holds what its expression is built from
            found |= self.sources_of(scope.expression, skip_counts)
        return found

    def _whole_row(self, source: exp.Expression | Scope | None, skip_counts: bool) -> frozenset[BaseColumn]:
        if isinstance(source, exp.Table):
            table = self.allow.table_name(source.name)
            return frozenset((fold(table), fold(c)) for c in self.allow.allowed(table)) if table else frozenset()
        if isinstance(source, Scope):
            return self._cached(("row", id(source), skip_counts), lambda: self._row_now(source, skip_counts))
        return frozenset()

    def _row_now(self, source: Scope, skip_counts: bool) -> set[BaseColumn]:
        found: set[BaseColumn] = set()
        branches = outer_selects(source.expression)
        for branch in branches:  # the row is its output expressions, not its WHERE clause
            for projection in branch.expressions:
                found |= self.sources_of(projection, skip_counts)
        if not branches:  # UNNEST(...) and the like
            found |= self.sources_of(source.expression, skip_counts)
        return found

    def _whole_scope(self, scope: Scope, skip_counts: bool) -> frozenset[BaseColumn]:
        found: set[BaseColumn] = set()
        for source in scope.sources.values():
            found |= self._whole_row(source, skip_counts)
        return frozenset(found)

    def _by_name(self, folded: str) -> frozenset[BaseColumn]:
        """Fallback for a column we cannot place: it touches any sensitive column with the same name."""
        sensitive = self.allow.customer_id_set | self.allow.quasi_identifier_set
        return frozenset(pair for pair in sensitive if pair[1] == folded)


def analyze(qualified: exp.Expression, allowlist: Allowlist) -> Analysis:
    """Run the lineage questions on a qualified copy of the query. Raises on a tree it cannot analyse."""
    lineage = _Lineage(qualified, allowlist)
    branches = outer_selects(qualified)
    if not branches:
        raise ValueError("the query has no SELECT to analyse")
    ids, quasi = allowlist.customer_id_set, allowlist.quasi_identifier_set

    id_positions: set[int] = set()
    for branch in branches:
        for position, projection in enumerate(branch.expressions):
            if lineage.sources_of(projection, skip_counts=True) & ids:
                id_positions.add(position)
    first = branches[0]
    id_outputs = tuple(first.expressions[p].alias_or_name for p in sorted(id_positions) if p < len(first.expressions))

    demographic = False
    for node in qualified.walk():
        if isinstance(node, (exp.Column, exp.Star, *ROW_REFERENCE)) and lineage.sources_of(node, skip_counts=False) & quasi:
            demographic = True
            break

    # A collecting aggregate over a quasi-identifier returns the customers' own values, grouped or not.
    collects = any(
        isinstance(node, COLLECTING) and lineage.sources_of(node, skip_counts=False) & quasi for node in qualified.walk()
    )

    single = len(branches) == 1 and isinstance(unwrap(qualified), exp.Select)
    ranked = bool(id_positions) and single and _is_ranked_list(branches[0], id_positions, lineage, ids)
    return Analysis(
        id_outputs=id_outputs,
        id_positions=tuple(sorted(id_positions)),
        ranked_list=ranked,
        demographic=demographic,
        aggregated=all(is_aggregated(b) for b in branches) and not collects,
        collects=collects,
        grouped_below=all(_grouped_below(b, lineage) for b in branches),
        k_sources=tuple(_k_sources(b, lineage, ids, quasi) for b in branches),
    )


def _group_keys(select: exp.Select, group: exp.Group) -> set[str] | None:
    """The GROUP BY keys as SQL text, with ordinals and SELECT aliases replaced by what they stand for.
    None for ROLLUP, CUBE and grouping sets: those add total rows, so they are never a plain ranked list."""
    if group.args.get("rollup") or group.args.get("cube") or group.args.get("grouping_sets") or group.args.get("totals"):
        return None
    projections = select.expressions
    by_alias = {fold(p.alias): p.this for p in projections if isinstance(p, exp.Alias)}
    keys: set[str] = set()
    if group.args.get("all"):  # GROUP BY ALL groups by every non-aggregate output
        keys |= {unalias(p).sql() for p in projections if not has_aggregate(p)}
    for key in group.expressions:
        if isinstance(key, exp.Literal) and not key.is_string and key.name.isdigit() and 1 <= int(key.name) <= len(projections):
            keys.add(unalias(projections[int(key.name) - 1]).sql())
        elif isinstance(key, exp.Column) and not key.table and fold(key.name) in by_alias:
            keys.add(by_alias[fold(key.name)].sql())
        else:
            keys.add(key.sql())
    return keys


def _is_ranked_list(select: exp.Select, id_positions: set[int], lineage: _Lineage, ids: frozenset[BaseColumn]) -> bool:
    """Rule R2: a ranked spend list groups by the customer ID, aggregates, orders, and does not page with OFFSET."""
    group = select.args.get("group")
    if not group or not select.args.get("order") or select.args.get("offset"):
        return False
    if not any(reduces(p) for p in select.expressions):
        return False
    keys = _group_keys(select, group)
    if keys is None:
        return False
    for position in id_positions:
        output = unalias(select.expressions[position])
        if output.sql() in keys:
            continue
        # An expression over group keys, such as CONCAT('c', CAST(user_id AS STRING)): every ID column in it must
        # itself be a group key and must not sit under an aggregate or a window function. If no ID column can be
        # found in it (a whole-row value, a `*`), it is not a plain ranked list.
        checked = False
        for column in output.find_all(exp.Column):
            if not lineage.resolve(column, lineage.scope_of(column), True) & ids:
                continue
            checked = True
            if column.sql() not in keys or column.find_ancestor(exp.AggFunc, exp.Window) is not None:
                return False
        if not checked:
            return False
    return True


def _id_column_of(source: exp.Expression | Scope, lineage: _Lineage, ids: frozenset[BaseColumn]) -> str | None:
    """The column of one source that holds a customer ID, to count distinct customers in. A table gives its ID column.
    A derived table or CTE gives an output that is a plain ID column. None when it has none."""
    if isinstance(source, exp.Table):
        table = lineage.allow.table_name(source.name)
        columns = lineage.allow.customer_id_columns_of(table) if table else []
        return columns[0] if columns else None
    if isinstance(source, Scope):
        branches = outer_selects(source.expression)
        if len(branches) != 1:
            return None
        branch_scope = lineage.scope_by_expression.get(id(branches[0]), source)
        for projection in branches[0].expressions:
            plain = unalias(projection)
            if isinstance(plain, exp.Column) and not isinstance(plain.this, exp.Star) and lineage.resolve(plain, branch_scope, True) & ids:
                return projection.alias_or_name
    return None


def _demographic_aliases(select: exp.Select, scope: Scope, lineage: _Lineage, quasi: frozenset[BaseColumn]) -> set[str]:
    """The aliases of this SELECT's own sources that quasi-identifier columns are read from, in any clause. A source
    that only a nested query reads (a filter on `users` inside `IN (...)`) is not one of them."""
    own = {fold(alias) for alias in scope.sources}
    found: set[str] = set()
    for node in select.walk():
        if isinstance(node, ROW_REFERENCE):
            if lineage.sources_of(node, skip_counts=False) & quasi:
                found.add(fold(node.name))
        elif isinstance(node, exp.Column) and node.table:
            if lineage.resolve(node, lineage.scope_of(node), False) & quasi:
                found.add(fold(node.table))
        elif isinstance(node, exp.Star) and not isinstance(node.parent, (exp.Column, *COUNTING)):
            found |= own  # a `*` that qualify could not expand: any source may be behind it
    return found & own


def _k_sources(select: exp.Select, lineage: _Lineage, ids: frozenset[BaseColumn], quasi: frozenset[BaseColumn]) -> tuple[tuple[str, str, bool], ...] | None:
    """Where to count distinct customers for the Result Guard's group-size check (R5).

    Every source of the SELECT that holds a customer ID is counted, and the smallest count is the group size: the rows of
    a group may only be as many customers as the thinnest source behind them. Counting the first source, as this once
    did, let `FROM order_items oi CROSS JOIN users u GROUP BY u.state` report the thousands of buyers in the whole join as
    the size of every state. And every source that supplies demographic data must hold an ID, or its customers cannot be
    counted: then None, and the Result Guard withholds the result."""
    scope = lineage.scope_for(select)
    holders: dict[str, str] = {}
    for alias, (_, source) in scope.selected_sources.items():
        column = _id_column_of(source, lineage, ids)
        if column:
            holders[fold(alias)] = column
    demographic = _demographic_aliases(select, scope, lineage, quasi)
    if any(alias not in holders for alias in demographic):
        return None
    return tuple((alias, column, alias in demographic) for alias, column in holders.items()) or None


def _grouped_below(select: exp.Select, lineage: _Lineage) -> bool:
    """True when this SELECT reads only derived tables or CTEs and one of them, at any depth, groups its rows: a query
    that has been split into a grouping step and a presentation step. Such a query can be written as one SELECT."""
    scope = lineage.scope_for(select)
    sources = [source for _, (_, source) in scope.selected_sources.items()]
    if not sources or any(isinstance(source, exp.Table) for source in sources):
        return False

    def groups(inner: Scope, seen: set[int]) -> bool:
        if id(inner) in seen:
            return False
        seen.add(id(inner))
        selects = outer_selects(inner.expression)
        if selects and all(is_aggregated(s) for s in selects):
            return True
        return any(
            isinstance(source, Scope) and groups(source, seen)
            for s in selects for _, (_, source) in lineage.scope_for(s).selected_sources.items()
        )

    return any(isinstance(source, Scope) and groups(source, set()) for source in sources)
