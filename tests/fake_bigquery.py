"""A scripted stand-in for `bigquery.Client`: no network, no credentials (plan 03 tests, reused by plan 09).

A `Rule` says how the client answers a query whose SQL contains `contains`; the first matching rule wins and an
unmatched query fails the test loudly. Error lists are consumed one error per call, so `query_errors=[a, b]`
fails twice and then succeeds.

`default_client()` is the canned dataset the autouse fixture in conftest installs. Its columns and brands are
TEST FIXTURES shaped like plan 05's expected content, not the live schema (Q4 is still open until a live run).
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
from google.api_core import exceptions as api_exc
from google.cloud import bigquery

DATASET = "bigquery-public-data.thelook_ecommerce"

# The types are the REST API's legacy names, as the client library reports them (INTEGER, FLOAT), not GoogleSQL's.
FAKE_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "orders": [("order_id", "INTEGER"), ("user_id", "INTEGER"), ("status", "STRING"), ("gender", "STRING"),
               ("created_at", "TIMESTAMP"), ("returned_at", "TIMESTAMP"), ("shipped_at", "TIMESTAMP"),
               ("delivered_at", "TIMESTAMP"), ("num_of_item", "INTEGER")],
    "order_items": [("id", "INTEGER"), ("order_id", "INTEGER"), ("user_id", "INTEGER"), ("product_id", "INTEGER"),
                    ("inventory_item_id", "INTEGER"), ("status", "STRING"), ("created_at", "TIMESTAMP"),
                    ("shipped_at", "TIMESTAMP"), ("delivered_at", "TIMESTAMP"), ("returned_at", "TIMESTAMP"),
                    ("sale_price", "FLOAT")],
    "products": [("id", "INTEGER"), ("cost", "FLOAT"), ("category", "STRING"), ("name", "STRING"),
                 ("brand", "STRING"), ("retail_price", "FLOAT"), ("department", "STRING"), ("sku", "STRING"),
                 ("distribution_center_id", "INTEGER")],
    "users": [("id", "INTEGER"), ("first_name", "STRING"), ("last_name", "STRING"), ("email", "STRING"),
              ("age", "INTEGER"), ("gender", "STRING"), ("state", "STRING"), ("street_address", "STRING"),
              ("postal_code", "STRING"), ("city", "STRING"), ("country", "STRING"), ("latitude", "FLOAT"),
              ("longitude", "FLOAT"), ("traffic_source", "STRING"), ("created_at", "TIMESTAMP"),
              ("user_geom", "GEOGRAPHY")],
}
FAKE_TABLE_BYTES = {"orders": 9_000_000, "order_items": 42_000_000, "products": 3_000_000, "users": 11_000_000}
FAKE_BRANDS = ["A", "B", "C", "Brand A", "Brand B", "Brand C", "Calvin Klein", "Levi's", "Nike"]
FAKE_LATEST = pd.Timestamp("2026-10-22 11:30:00", tz="UTC")


@dataclass
class Rule:
    contains: str
    df: pd.DataFrame | None = None
    total_rows: int | None = None  # None: len(df)
    bytes_processed: int | None = 1_000_000  # None: the dry run did not report an estimate
    bytes_billed: int | None = None  # None: same as bytes_processed
    cache_hit: bool = False
    statement_type: str | None = "SELECT"  # None: BigQuery did not report one
    dry_errors: list[BaseException] = field(default_factory=list)
    query_errors: list[BaseException] = field(default_factory=list)
    result_errors: list[BaseException] = field(default_factory=list)
    cancel_error: BaseException | None = None


class FakeRowIterator:
    def __init__(self, df: pd.DataFrame, total_rows: int, max_results: int | None) -> None:
        self._df = df
        self.total_rows = total_rows
        self.max_results = max_results
        self.to_dataframe_kwargs: dict[str, Any] | None = None

    def to_dataframe(self, **kwargs: Any) -> pd.DataFrame:
        self.to_dataframe_kwargs = kwargs
        return self._df if self.max_results is None else self._df.head(self.max_results)


class FakeJob:
    def __init__(self, sql: str, config: bigquery.QueryJobConfig | None, rule: Rule, call_kwargs: dict[str, Any]) -> None:
        self.sql = sql
        self.config = config
        self.rule = rule
        self.call_kwargs = call_kwargs
        self.dry_run = bool(config and config.dry_run)
        self.cancelled = False
        self.cancel_error: BaseException | None = rule.cancel_error
        self.result_kwargs: dict[str, Any] | None = None
        self.iterator: FakeRowIterator | None = None

    statement_type = property(lambda self: self.rule.statement_type)
    cache_hit = property(lambda self: self.rule.cache_hit)
    total_bytes_processed = property(lambda self: self.rule.bytes_processed)

    @property
    def total_bytes_billed(self) -> int | None:
        if self.dry_run:
            return None
        return self.rule.bytes_processed if self.rule.bytes_billed is None else self.rule.bytes_billed

    def result(self, **kwargs: Any) -> FakeRowIterator:
        self.result_kwargs = kwargs
        if self.rule.result_errors:
            raise self.rule.result_errors.pop(0)
        df = self.rule.df if self.rule.df is not None else pd.DataFrame()
        total = len(df) if self.rule.total_rows is None else self.rule.total_rows
        self.iterator = FakeRowIterator(df, total, kwargs.get("max_results"))
        return self.iterator

    def cancel(self, **kwargs: Any) -> bool:
        self.cancelled = True
        if self.cancel_error is not None:
            raise self.cancel_error
        return True


class FakeClient:
    def __init__(self, rules: list[Rule] | None = None, tables: dict[str, bigquery.Table] | None = None) -> None:
        self.rules = rules or []
        self.tables = tables or {}
        self.table_errors: list[BaseException] = []
        self.down = False  # True: every call fails with a 503, so a test can take BigQuery away
        self.jobs: list[FakeJob] = []  # every query() call, including the ones that raised
        self.table_calls: list[str] = []
        self._lock = threading.Lock()  # the catalog reads the four tables from four threads

    def add(self, contains: str, **kwargs: Any) -> Rule:
        rule = Rule(contains, **kwargs)
        self.rules.insert(0, rule)  # the newest rule wins, so a test can override a canned one
        return rule

    @property
    def real_jobs(self) -> list[FakeJob]:
        return [job for job in self.jobs if not job.dry_run]

    @property
    def dry_jobs(self) -> list[FakeJob]:
        return [job for job in self.jobs if job.dry_run]

    def query(self, sql: str, job_config: bigquery.QueryJobConfig | None = None, **kwargs: Any) -> FakeJob:
        rule = next((r for r in self.rules if r.contains in sql), None)
        assert rule is not None, f"no fake rule for this query: {sql[:120]}"
        job = FakeJob(sql, job_config, rule, kwargs)
        self.jobs.append(job)
        if self.down:
            raise api_exc.ServiceUnavailable("503")
        errors = rule.dry_errors if job.dry_run else rule.query_errors
        if errors:
            raise errors.pop(0)
        return job

    def get_table(self, ref: str, **kwargs: Any) -> bigquery.Table:
        with self._lock:
            self.table_calls.append(ref)
            if self.down:
                raise api_exc.ServiceUnavailable("503")
            if self.table_errors:
                raise self.table_errors.pop(0)
        name = ref.rsplit(".", 1)[-1]
        if name not in self.tables:
            raise api_exc.NotFound("table not found")
        return self.tables[name]


class FakeRunner:
    """Looks like `BigQueryRunner` to `SafeQueryRunner`: it only needs `.client`."""

    def __init__(self, client: FakeClient) -> None:
        self.client = client


def make_table(name: str, columns: list[tuple[str, str]], num_bytes: int) -> bigquery.Table:
    table = bigquery.Table(f"{DATASET}.{name}", schema=[bigquery.SchemaField(col, typ) for col, typ in columns])
    table._properties["numBytes"] = str(num_bytes)  # the library reads num_bytes from the raw resource
    return table


def default_client() -> FakeClient:
    client = FakeClient(tables={
        name: make_table(name, columns, FAKE_TABLE_BYTES[name]) for name, columns in FAKE_COLUMNS.items()
    })
    client.add("MAX(created_at)", df=pd.DataFrame({"latest": [FAKE_LATEST]}), bytes_processed=8_000_000)
    client.add("SELECT DISTINCT brand", df=pd.DataFrame({"brand": FAKE_BRANDS}), bytes_processed=1_000_000)
    client.add("GROUP BY p.brand", df=pd.DataFrame({"brand": ["Nike", "Levi's"], "items": [1200, 900]}))
    client.add("COUNTIF", df=pd.DataFrame({"items": [180_000], "without_product": [0], "without_brand": [0]}))
    return client
