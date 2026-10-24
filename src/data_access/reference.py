"""Reference queries: read the data directly, outside the guards, to know what the right answer is (scenario figures).

A scenario says "the answer must state revenue of 2025 for the user's brands", and this is how the test learns that number without asking the
app: it runs the SQL itself. That is a bypass of the SQL Guard on purpose (no brand filter, no allow-list), exactly like `SystemQuery`, so it is
kept here in `data_access/` with the same limits: every query still gets `SafeQueryRunner`'s dry run, byte cap, timeout and labels, and only a
SELECT passes (the dry run reports the statement type and anything else is refused). Only `src/scenarios` may use it
(`tests/test_reference_queries.py` fails if anything else imports it).

The SQL is written once, in GoogleSQL, with the four tables by their bare names (`order_items`). It is qualified with the configured dataset here,
so the same scenario runs against BigQuery and, through the local stand-in, against the sample data.
"""
from __future__ import annotations

import pandas as pd
import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from src.config import Settings
from src.data_access.catalog import TABLES
from src.data_access.safe_runner import SafeQueryRunner, SystemQuery
from src.errors import AgentError, ErrorKind

MAX_ROWS = 100_000


class ReferenceQueries:
    def __init__(self, settings: Settings, safe: SafeQueryRunner | None = None) -> None:
        self._safe = safe or SafeQueryRunner(settings)
        self._project, _, self._dataset = settings.bigquery.dataset.rpartition(".")

    def qualify(self, sql: str) -> str:
        """The SQL with every bare table name of the dataset spelled in full. A table that already has a dataset, or a name that is not one of
        the four tables (a WITH clause, say), is left as it is."""
        try:
            tree = sqlglot.parse_one(sql, read="bigquery")
        except SqlglotError:
            raise AgentError(ErrorKind.SQL_SYNTAX, detail="the reference SQL could not be read") from None
        ctes = {cte.alias_or_name.casefold() for cte in tree.find_all(exp.CTE)}
        for table in list(tree.find_all(exp.Table)):
            if table.args.get("db") or table.name.casefold() in ctes or table.name not in TABLES:
                continue
            table.set("db", exp.to_identifier(self._dataset))
            table.set("catalog", exp.to_identifier(self._project, quoted=True))
        return tree.sql(dialect="bigquery")

    def frame(self, sql: str) -> pd.DataFrame:
        return self._safe.run(SystemQuery(self.qualify(sql), max_rows=MAX_ROWS)).df

    def number(self, sql: str) -> float:
        """The first cell of the first row, as a float."""
        frame = self.frame(sql)
        if frame.empty or pd.isna(frame.iloc[0, 0]):
            raise ValueError("the SQL returned no number")
        return float(frame.iloc[0, 0])

    def strings(self, sql: str) -> list[str]:
        """The first column, as text, without nulls."""
        frame = self.frame(sql)
        return [str(value) for value in frame.iloc[:, 0].tolist() if value is not None and not pd.isna(value)] if not frame.empty else []
