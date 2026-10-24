"""A DuckDB stand-in for `bigquery.Client`, so the whole app runs on generated sample data with no Google sign-in (`bigquery.backend: local`).

It sits where the real client sits: `SafeQueryRunner` still does the dry run, the byte cap, the timeout, the labels and the row
cap, and the SQL Guard, Result Guard and every other check run unchanged. Only the engine behind `client.query` and
`client.get_table` changes. That makes it a good place to see the guarded SQL run against real tables, and a poor place to
learn how BigQuery behaves. What it does NOT prove (see docs/local-testing.md):

- BigQuery's own parser and name resolution. The query is read with sqlglot's BigQuery dialect and rewritten for DuckDB, so a query
  BigQuery would refuse can run here, and a rare BigQuery-only function can fail here and work there.
- Cost. The dry run adds up the sizes of the tables the query reads (whole tables, not the columns it touches), so the byte cap
  is exercised, but the numbers are only a model of BigQuery's.
- Quotas, latency, caching, permissions, and the live schema.

Errors are raised as `google.api_core.exceptions` with BigQuery-like messages, so `errors_map.classify` turns them into the same
typed `AgentError`s as a real failure: an unknown column is a 400 "Unrecognized name", an unknown table a 404, a statement that is
not a SELECT reports its statement type in the dry run, and a query over the byte limit is refused with the limit's reason.

The file is opened read-only, so nothing a query says can change the data.
"""
from __future__ import annotations

import re
import threading
import warnings
from pathlib import Path
from typing import Any, Sequence

import db_dtypes  # noqa: F401 - registers the `dbdate` dtype that the BigQuery client uses for a DATE column
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import sqlglot
from google.api_core import exceptions as api_exc
from google.cloud import bigquery
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.annotate_types import annotate_types

from src.errors import ConfigError
from src.localdata import schema

QUERY_FIX = "Run: python -m src.cli.main local-data build"

_STATEMENT_TYPES = (
    (exp.Insert, "INSERT"), (exp.Update, "UPDATE"), (exp.Delete, "DELETE"), (exp.Merge, "MERGE"), (exp.Create, "CREATE_TABLE"),
    (exp.Drop, "DROP_TABLE"), (exp.Alter, "ALTER_TABLE"), (exp.TruncateTable, "TRUNCATE_TABLE"),
)
_COLUMN_NOT_FOUND = re.compile(r'(?:Referenced column|column named|column) "([^"]+)"', re.IGNORECASE)
_FUNCTION_NOT_FOUND = re.compile(r"Function with name (\S+) does not exist", re.IGNORECASE)


def _bad_request(message: str, reason: str = "invalidQuery") -> api_exc.BadRequest:
    return api_exc.BadRequest(message, errors=[{"reason": reason, "message": message}])


def _translate_error(exc: Exception) -> Exception:
    """A DuckDB failure as the BigQuery error it stands for."""
    import duckdb

    text = str(exc)
    first = text.splitlines()[0] if text else type(exc).__name__
    if isinstance(exc, duckdb.InterruptException):
        return TimeoutError("the local query was interrupted by its time limit")
    if isinstance(exc, duckdb.ParserException):
        return _bad_request(f"Syntax error: {first.removeprefix('Parser Error: ')} at [1:1]")
    function = _FUNCTION_NOT_FOUND.search(text)
    if function:
        return _bad_request(f"Function not found: {function.group(1)} at [1:1]")
    column = _COLUMN_NOT_FOUND.search(text)
    if column and isinstance(exc, (duckdb.BinderException, duckdb.CatalogException)):
        return _bad_request(f"Unrecognized name: {column.group(1)} at [1:1]")
    return _bad_request(f"Invalid query: {first.split(':', 1)[-1].strip()}")


class _Rows:
    """The row iterator of a finished job: only what `SafeQueryRunner` reads."""

    def __init__(self, frame: pd.DataFrame, total_rows: int, max_results: int | None) -> None:
        self.total_rows = total_rows
        self.max_results = max_results
        self._frame = frame

    def to_dataframe(self, **kwargs: Any) -> pd.DataFrame:
        frame = self._frame
        return frame if self.max_results is None else frame.head(self.max_results)


class _Job:
    def __init__(self, client: "LocalBigQueryClient", duck_sql: str, bound: dict[str, Any], statement_type: str, estimate: int,
                 dry_run: bool) -> None:
        self._client = client
        self._duck_sql = duck_sql
        self._bound = bound
        self.statement_type = statement_type
        self.total_bytes_processed: int | None = estimate
        self.dry_run = dry_run
        self.cache_hit = False
        self.cancelled = False
        self.labels: dict[str, str] = {}

    @property
    def total_bytes_billed(self) -> int | None:
        return None if self.dry_run else self.total_bytes_processed

    def result(self, max_results: int | None = None, timeout: float | None = None, **kwargs: Any) -> _Rows:
        if self.dry_run:
            raise api_exc.BadRequest("A dry run has no result rows")
        frame = self._client.run(self._duck_sql, self._bound, timeout)
        return _Rows(frame, len(frame), max_results)

    def cancel(self, **kwargs: Any) -> bool:
        self.cancelled = True  # a local query runs to its end inside `result`, so there is nothing left to stop
        return True


class LocalBigQueryClient:
    """`query` and `get_table`, the two calls the app makes, on a DuckDB file built by `src/localdata/generate.py`."""

    def __init__(self, db_path: Path, dataset: str) -> None:
        import duckdb

        self._dataset = dataset
        self._path = Path(db_path)
        if not self._path.is_file():
            raise ConfigError(f"The local sample data was not found: {self._path}", fix=QUERY_FIX)
        try:
            self._con = duckdb.connect(str(self._path), read_only=True)
            self._stats = {
                name: (int(rows), int(size))
                for name, rows, size in self._con.execute(f'SELECT name, num_rows, num_bytes FROM "{schema.STATS_TABLE}"').fetchall()
            }
        except duckdb.Error:
            raise ConfigError(f"The local sample data could not be read: {self._path}", fix=QUERY_FIX) from None
        if set(self._stats) != set(schema.TABLE_NAMES):
            raise ConfigError(f"The local sample data is not complete: {self._path}", fix=QUERY_FIX)
        self._lock = threading.Lock()  # a connection is not safe to share between threads; each query gets its own cursor

    # metadata ------------------------------------------------------------------------------------

    def get_table(self, ref: str, retry: Any = None, timeout: Any = None, **kwargs: Any) -> bigquery.Table:
        project_dataset, _, name = ref.rpartition(".")
        if (project_dataset and project_dataset != self._dataset) or name not in schema.TABLES:
            raise api_exc.NotFound(f"Not found: Table {ref} was not found")
        table = bigquery.Table(
            f"{self._dataset}.{name}", schema=[bigquery.SchemaField(column.name, column.bq_type) for column in schema.TABLES[name]]
        )
        table._properties["numBytes"] = str(self._stats[name][1])  # the library reads num_bytes from the raw resource
        table._properties["numRows"] = str(self._stats[name][0])
        return table

    # queries -------------------------------------------------------------------------------------

    def query(self, sql: str, job_config: bigquery.QueryJobConfig | None = None, **kwargs: Any) -> _Job:
        dry_run = bool(job_config and job_config.dry_run)
        parameters = list(getattr(job_config, "query_parameters", None) or ())
        duck_sql, bound, statement_type, tables = self._translate(sql, parameters)
        estimate = sum(self._stats[table][1] for table in tables)
        if statement_type == "SELECT":
            self._check(duck_sql, bound)  # names and syntax are checked here, as BigQuery's parser would, before any job exists
        if not dry_run:
            if statement_type != "SELECT":
                raise _bad_request("The local data is read-only: only a SELECT can run")
            limit = getattr(job_config, "maximum_bytes_billed", None)
            if limit is not None and estimate > limit:
                raise _bad_request(
                    f"Query exceeded limit for bytes billed: {limit}. {estimate} or higher required.", "bytesBilledLimitExceeded"
                )
        job = _Job(self, duck_sql, bound, statement_type, estimate, dry_run)
        job.labels = dict(getattr(job_config, "labels", None) or {})
        return job

    # translation: BigQuery SQL -> DuckDB SQL -------------------------------------------------------

    def _translate(self, sql: str, parameters: Sequence[Any]) -> tuple[str, dict[str, Any], str, list[str]]:
        try:
            statements = [s for s in sqlglot.parse(sql, read="bigquery") if s is not None]
        except SqlglotError as exc:
            raise _bad_request(f"Syntax error: {str(exc).splitlines()[0]} at [1:1]") from None
        if len(statements) != 1:
            raise _bad_request("Syntax error: expected exactly one statement at [1:1]")
        tree = statements[0]
        statement_type = "SELECT"
        for node_type, label in _STATEMENT_TYPES:
            if isinstance(tree, node_type):
                statement_type = label
                break
        else:
            if not isinstance(tree, (exp.Select, exp.SetOperation, exp.Subquery, exp.With)):
                statement_type = "SCRIPT"
        if statement_type != "SELECT":
            return sql, {}, statement_type, []

        ctes = {cte.alias_or_name.casefold() for cte in tree.find_all(exp.CTE)}
        tables: list[str] = []
        for table in tree.find_all(exp.Table):
            name = table.name
            if not table.args.get("db") and name.casefold() in ctes:
                continue  # a reference to a WITH clause, not to a table
            qualifier = ".".join(part for part in (table.catalog, table.db) if part)
            if (qualifier and qualifier not in (self._dataset, self._dataset.rpartition(".")[2])) or name not in schema.TABLES:  # `dataset.table` is fine too
                raise api_exc.NotFound(f"Not found: Table {self._dataset.replace('.', ':', 1)}.{name} was not found in location US")
            table.set("db", None)
            table.set("catalog", None)
            if name not in tables:
                tables.append(name)

        self._keep_dates_as_dates(tree)
        values = {str(getattr(p, "name", "")): p for p in parameters}
        bound: dict[str, Any] = {}
        for param in list(tree.find_all(exp.Parameter)):
            name = param.name
            supplied = values.get(name)
            if supplied is None:
                raise _bad_request(f"Query parameter '{name}' not found at [1:1]")
            bound[name] = list(supplied.values) if hasattr(supplied, "values") else supplied.value
            param.replace(exp.Placeholder(this=name))
        return tree.sql(dialect="duckdb"), bound, statement_type, tables

    @staticmethod
    def _keep_dates_as_dates(tree: exp.Expression) -> None:
        """`DATE_TRUNC` of a DATE is a DATE in BigQuery and a TIMESTAMP in DuckDB, which would show a month as `2026-01-01T00:00:00`.
        Where sqlglot can tell that the argument is a DATE, the result is cast back. A best effort: a column it cannot type is left."""
        try:
            typed = annotate_types(tree, dialect="bigquery")
            for node in list(typed.find_all(exp.DateTrunc)):
                if node.type is not None and node.type.is_type(exp.DataType.Type.DATE):
                    node.replace(exp.Cast(this=node.copy(), to=exp.DataType.build("DATE")))
        except SqlglotError:
            return

    # running ---------------------------------------------------------------------------------------

    def _cursor(self):
        with self._lock:
            cursor = self._con.cursor()
        cursor.execute("SET TimeZone = 'UTC'")
        return cursor

    def _check(self, duck_sql: str, bound: dict[str, Any]) -> None:
        import duckdb

        cursor = self._cursor()
        try:
            cursor.execute("EXPLAIN " + duck_sql, bound)
        except duckdb.Error as exc:
            raise _translate_error(exc) from None
        finally:
            cursor.close()

    def run(self, duck_sql: str, bound: dict[str, Any], timeout: float | None) -> pd.DataFrame:
        import duckdb

        cursor = self._cursor()
        timer = threading.Timer(timeout, cursor.interrupt) if timeout and timeout > 0 else None
        if timer is not None:
            timer.daemon = True
            timer.start()
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)  # "Arrow output on a DB-API result makes an extra copy": the copy is tiny
                table = cursor.execute(duck_sql, bound).to_arrow_table()
        except duckdb.Error as exc:
            translated = _translate_error(exc)
            raise translated from None
        finally:
            if timer is not None:
                timer.cancel()
            cursor.close()
        return _frame(table)


def _frame(table: pa.Table) -> pd.DataFrame:
    """Rows as the BigQuery client would hand them over: whole numbers as nullable `Int64`, text as plain objects."""
    columns = []
    for field in table.schema:
        column = table[field.name]
        if pa.types.is_decimal(field.type) and field.type.scale == 0:
            column = pc.cast(column, pa.int64())  # SUM over integers is a 38-digit decimal in DuckDB and an INT64 in BigQuery
        columns.append(column)
    table = pa.table(columns, names=[field.name for field in table.schema])
    frame = table.to_pandas(types_mapper=lambda kind: pd.Int64Dtype() if pa.types.is_integer(kind) else None)
    for name in frame.columns:
        kind = table.schema.field(name).type
        if pa.types.is_string(kind) or pa.types.is_large_string(kind):
            frame[name] = frame[name].astype(object)
        elif pa.types.is_date32(kind):  # a DATE is a `datetime.date` per cell in BigQuery's frame, not a timestamp at midnight
            frame[name] = pd.Series(pd.array(table[name].to_pylist(), dtype=db_dtypes.DateDtype()), index=frame.index)
    return frame


class LocalBigQueryRunner:
    """What `SafeQueryRunner` holds in place of `BigQueryRunner`: an object with a `.client`."""

    def __init__(self, db_path: Path, dataset: str) -> None:
        self.client = LocalBigQueryClient(db_path, dataset)
