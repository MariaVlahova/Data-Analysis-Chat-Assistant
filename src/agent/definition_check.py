"""The revenue definition, checked in code (plan 12, D2; decision 3.5, NFR16).

The golden run on a real model (2026-10-25) gave two different figures for "revenue" in one report: one sub-question left out cancelled and returned
items, another left out only cancelled ones, so returned items were counted as revenue. The definition is in the prompt (the glossary), and a prompt is a
request. Nothing in code noticed. This is the check in code: after the SQL Guard has passed a query and before it runs, every plain `SUM(<alias>.sale_price)`
must sit in a SELECT that leaves out both cancelled and returned items. A query that does not is the model's own mistake, not a guard violation: it goes back
to the SQL repair step (`SQL_DEFINITION`), counts against `loop.sql_repairs`, and after the last repair the sub-question fails with a gap line in the answer
(a missing figure, not a wrong one).

What it checks, and what it leaves alone:
- It runs only for a sub-question that uses "revenue" or one of its glossary aliases, or says "not cancelled or returned", and does not ask for something else
  ("including returns", "gross", "the value of the returned items").
- It reads the sqlglot tree of the model's SQL, never the text.
- A sum inside a CASE or IF (the value of the returned items, for example) is not revenue, and is left alone. So is a sum over anything but the column itself.
- A SELECT may leave the two statuses out in any of the usual ways: `status NOT IN (...)`, `status <> 'x'` for each, a list or an equality of the statuses it
  keeps, or `returned_at IS NULL` for returns. The conditions are the top-level ANDs of its WHERE and of the ON of its joins.
- A sum over a column that comes from a subquery or a CTE is not judged: the filter may be inside it, and this check does not follow it (a net for the usual
  drift, not a proof). Another definition (churn, average order value) can drift the same way; the same pattern extends to it, one definition at a time.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

import sqlglot
from sqlglot import exp

from src.errors import AgentError, ErrorKind

REVENUE_TERMS = ("revenue", "sales", "turnover")  # the glossary's term and aliases; `build_agent` passes the real ones
DEFINITION = "revenue is the sum of item sale prices, without cancelled or returned items"
REQUIRED = frozenset({"cancelled", "returned"})
VALUE_COLUMN = "sale_price"
BASE_TABLE = "order_items"
STATUS_COLUMN, RETURNED_AT = "status", "returned_at"

_WORD = r"(?:cancell?ed|cancell?ations?|returned|returns?|refunds?)"
# "not cancelled or returned", "without returns", "excluding cancelled and returned items", "net of returns": the definition itself, in the question's words.
_NEGATED = re.compile(
    rf"\b(?:not|no|without|excluding|excludes?|except|net of|minus|ignoring)\b[^.;:?!]{{0,25}}?\b{_WORD}\b(?:\s*(?:,|and|or|nor)\s*(?:not\s+)?{_WORD}\b)*",
    re.IGNORECASE,
)
# Anything left after the definition's own words is a request for something else: the returns, the cancellations, the gross figure.
_SOMETHING_ELSE = re.compile(rf"\b(?:gross|{_WORD})\b", re.IGNORECASE)


@dataclass(frozen=True)
class DefinitionCheck:
    """The words that make a sub-question one about revenue. Without a glossary the built-in three."""

    terms: tuple[str, ...] = REVENUE_TERMS

    def applies(self, text: str) -> bool:
        """True when the sub-question's text is about revenue as defined, and not about returns, cancellations or a gross figure."""
        negated = bool(_NEGATED.search(text))
        rest = _NEGATED.sub(" ", text)
        if _SOMETHING_ELSE.search(rest):
            return False
        names_revenue = any(re.search(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", rest, re.IGNORECASE) for term in self.terms)
        return names_revenue or negated

    def check(self, question: str, sql: str) -> None:
        """Raises `SQL_DEFINITION` (repairable) when the question is about revenue and a plain sum of sale prices in `sql` counts returned or cancelled items."""
        if not self.applies(question):
            return
        problems = unfiltered_sums(sql)
        if problems:
            raise AgentError(
                ErrorKind.SQL_DEFINITION,
                detail=f"{DEFINITION}: put {STATUS_COLUMN} NOT IN ('Cancelled', 'Returned') in the WHERE of the SELECT that has SUM({VALUE_COLUMN}); {problems[0]}",
            )


def build_definition_check(glossary_words: Iterable[str] = ()) -> DefinitionCheck:
    """From the glossary's words for revenue (its term and aliases). The built-in terms always stay, so an emptied glossary does not switch the check off."""
    words = tuple(dict.fromkeys([*REVENUE_TERMS, *(word.strip().lower() for word in glossary_words if word.strip())]))
    return DefinitionCheck(words)


# the tree ---------------------------------------------------------------------------------------------------------------

def unfiltered_sums(sql: str) -> list[str]:
    """One short reason for each plain `SUM(<alias>.sale_price)` of `order_items` whose SELECT does not leave out both cancelled and returned items. Empty when
    every such sum does, when there is none, and when the SQL cannot be read (the SQL Guard has the say on that)."""
    try:
        tree = sqlglot.parse_one(sql, read="bigquery")
    except Exception:  # noqa: BLE001 - not this check's business; the SQL Guard already passed it or refused it
        return []
    if tree is None:
        return []
    ctes = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    reasons: list[str] = []
    for total in tree.find_all(exp.Sum):
        column = total.this
        if not (isinstance(column, exp.Column) and column.name.lower() == VALUE_COLUMN):
            continue  # SUM(CASE ...), SUM(price * quantity), SUM(DISTINCT ...): not the plain sum of the definition
        if total.find_ancestor(exp.Case, exp.If) is not None:
            continue  # the sum is one branch of a larger expression: not revenue by itself
        select = total.find_ancestor(exp.Select)
        if select is None or not _reads_order_items(select, column, ctes):
            continue
        missing = REQUIRED - excluded_statuses(select)
        if missing:
            reasons.append("it does not leave out " + " or ".join(sorted(missing)) + " items")
    return list(dict.fromkeys(reasons))


def _own_tables(select: exp.Select) -> list[exp.Table]:
    """The tables this SELECT reads itself (not those of its subqueries)."""
    return [table for table in select.find_all(exp.Table) if table.find_ancestor(exp.Select) is select]


def _reads_order_items(select: exp.Select, column: exp.Column, ctes: set[str]) -> bool:
    """True when `column` is the `sale_price` of the base table `order_items` in this SELECT (not a subquery's or a CTE's column of that name)."""
    tables = _own_tables(select)
    qualifier = column.table.lower() if column.table else ""
    if qualifier:
        source = next((table for table in tables if (table.alias_or_name or "").lower() == qualifier), None)
    else:
        source = tables[0] if len(tables) == 1 and not _has_derived_source(select) else None
    return source is not None and source.name.lower() == BASE_TABLE and BASE_TABLE not in ctes


def _has_derived_source(select: exp.Select) -> bool:
    return any(isinstance(node, (exp.Subquery, exp.Unnest)) for node in _sources(select))


def _sources(select: exp.Select) -> list[exp.Expression]:
    from_ = select.args.get("from_")
    nodes: list[exp.Expression] = [from_.this] if from_ is not None and from_.this is not None else []
    nodes += [join.this for join in select.args.get("joins") or [] if join.this is not None]
    return nodes


def _conjuncts(node: exp.Expression | None) -> list[exp.Expression]:
    if node is None:
        return []
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, exp.And):
        return [*_conjuncts(node.left), *_conjuncts(node.right)]
    return [node]


def _status_column(node: exp.Expression | None) -> bool:
    while isinstance(node, (exp.Lower, exp.Upper, exp.Trim, exp.Paren)):
        node = node.this
    return isinstance(node, exp.Column) and node.name.lower() == STATUS_COLUMN


def _literals(nodes: Iterable[exp.Expression]) -> set[str] | None:
    """The string literals of a list, lower-cased; None when something in it is not a plain string (a column, a subquery)."""
    values: set[str] = set()
    for node in nodes:
        if not (isinstance(node, exp.Literal) and node.is_string):
            return None
        values.add(node.name.strip().lower())
    return values


def excluded_statuses(select: exp.Select) -> set[str]:
    """Which of 'cancelled' and 'returned' the top-level conditions of this SELECT (its WHERE and the ON of its joins) leave out."""
    where = select.args.get("where")
    conditions = _conjuncts(where.this if where is not None else None)
    for join in select.args.get("joins") or []:
        conditions += _conjuncts(join.args.get("on"))
    out: set[str] = set()
    for condition in conditions:
        negated = False
        while isinstance(condition, (exp.Not, exp.Paren)):
            negated = negated != isinstance(condition, exp.Not)
            condition = condition.this
        if isinstance(condition, exp.Is):
            column = condition.this
            if isinstance(column, exp.Column) and column.name.lower() == RETURNED_AT and isinstance(condition.expression, exp.Null) and not negated:
                out.add("returned")  # returned_at IS NULL: never returned
            continue
        if isinstance(condition, exp.NEQ):  # a <> b is NOT (a = b)
            condition, negated = exp.EQ(this=condition.this, expression=condition.expression), not negated
        if isinstance(condition, exp.EQ):
            column, other = (condition.this, condition.expression) if _status_column(condition.this) else (condition.expression, condition.this)
            values = _literals([other]) if _status_column(column) else None
        elif isinstance(condition, exp.In) and _status_column(condition.this) and not condition.args.get("query"):
            values = _literals(condition.expressions)
        else:
            continue
        if values is not None:
            # `status IN values` (or `= value`) keeps those and leaves out the other known statuses; the negation leaves out those.
            out |= (REQUIRED & values) if negated else (REQUIRED - values)
    return out
