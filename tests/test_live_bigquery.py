"""Live BigQuery checks (plan 03 acceptance). Manual: `pytest -m live`.

Needs `gcloud auth application-default login` and GOOGLE_CLOUD_PROJECT. Without credentials every test skips.
Cost: metadata calls and dry runs are free; the only billed queries are a COUNT(*), a MAX(created_at) and a
DISTINCT brand over one column each. Nothing here asserts a number we did not read from the dataset.
"""
import json
import functools
from pathlib import Path

import pytest

from src.config import Settings, load_settings
from src.data_access.brand_check import brand_coverage, top_brands
from src.data_access.catalog import TABLES, load_catalog
from src.data_access.safe_runner import SafeQueryRunner, SystemQuery
from src.errors import AgentError, ErrorKind
from src.observability import Tracer
from src.observability.metrics import read_runs

pytestmark = pytest.mark.live

ROOT = Path(__file__).resolve().parent.parent


@functools.lru_cache(maxsize=1)  # the metadata probe can take seconds on a laptop: ask once per run
def _have_credentials() -> bool:
    try:
        import google.auth

        google.auth.default()
        return True
    except Exception:  # noqa: BLE001 - any failure means "no usable credentials here"
        return False


@pytest.fixture
def settings(tmp_path) -> Settings:
    # Settings first: loading them reads the project's .env into the environment, which is where a key file path or
    # project may be set. The credentials are looked for after that, with the real environment (see conftest).
    real = load_settings(ROOT / "config.yaml")
    if not _have_credentials():
        pytest.skip("no Google credentials: run `gcloud auth application-default login` or set GOOGLE_APPLICATION_CREDENTIALS in .env")
    return real.model_copy(update={"app": real.app.model_copy(update={"data_dir": str(tmp_path)})})  # keep the repo's data/ clean


@pytest.fixture
def safe(settings) -> SafeQueryRunner:
    return SafeQueryRunner(settings)


def test_a_simple_aggregate_runs_reports_bytes_and_shows_up_in_the_trace_and_runs_jsonl(settings, safe, tmp_path):
    tracer = Tracer.from_settings(settings)
    with tracer.start_turn("livehash0001", "conv-live", "t-1", trace_id="1ee70003") as turn:
        # A column, not COUNT(*): BigQuery answers a bare COUNT(*) from table metadata and scans 0 bytes.
        outcome = safe.run(SystemQuery(f"SELECT COUNT(sale_price) AS items FROM {safe.table_ref('order_items')}"))
    assert int(outcome.df["items"].iloc[0]) > 0
    assert outcome.bytes_processed > 0 and outcome.bytes_billed >= 0 and outcome.total_rows == 1
    spans = [json.loads(line) for line in tracer.trace_path(turn.trace_id).read_text(encoding="utf-8").splitlines()]
    assert {"bq.dry_run", "bq.query"} <= {span["name"] for span in spans}
    row = read_runs(tracer.metrics_file)[0][0]
    assert row["bq_queries"] == 1 and row["bytes_billed"] == outcome.bytes_billed


def test_a_real_dry_run_accepts_a_select_and_reports_bytes(safe):
    info = safe.dry_run(SystemQuery(f"SELECT COUNT(sale_price) AS items FROM {safe.table_ref('order_items')}"))
    # The runner checks the statement type itself and fails closed (NOT_SELECT). If BigQuery did not report the type, this
    # raises GUARD_VIOLATION with "statement type None" and every query is refused: that is the sign to look at what a dry
    # run really returns. Returning at all means BigQuery said SELECT.
    assert info.bytes_processed > 0


def test_the_catalog_reads_the_live_schema_brands_and_latest_date(settings):
    loaded = load_catalog(settings)
    catalog = loaded.catalog
    assert set(catalog.tables) == set(TABLES) and all(catalog.tables[name] for name in TABLES)
    assert catalog.brands and catalog.latest_date.year >= 2000
    assert all(catalog.table_bytes[name] > 0 for name in TABLES)
    assert (settings.data_path / "cache" / "schema.json").exists() and (settings.data_path / "cache" / "brands.json").exists()
    print("\nlive columns:", {name: [c.name for c in cols] for name, cols in catalog.tables.items()})  # Q4: copy into plan 05's allow-list
    print("latest date:", catalog.latest_date, "table bytes:", catalog.table_bytes)


def test_top_brands_and_brand_coverage_answer_from_the_data(safe):
    brands = top_brands(safe, 5)
    assert brands and all(count > 0 for _, count in brands)
    coverage = brand_coverage(safe)
    assert coverage.items > 0 and 0 <= coverage.invisible_to_managers <= coverage.items
    print("\ntop brands:", brands, "\nA23 coverage:", coverage)


@pytest.mark.parametrize("sql,kind", [
    ("SELECT FROM", ErrorKind.SQL_SYNTAX),
    ("SELECT no_such_column FROM `bigquery-public-data.thelook_ecommerce.order_items`", ErrorKind.SQL_UNKNOWN_COLUMN),
])
def test_real_bigquery_messages_map_to_the_repairable_kinds(safe, sql, kind):
    with pytest.raises(AgentError) as err:
        safe.dry_run(SystemQuery(sql))  # a dry run: free, and where these errors show up
    assert err.value.kind == kind and err.value.repairable and err.value.detail


def test_a_tiny_cap_refuses_a_real_query_before_it_runs(settings, tmp_path):
    capped = settings.model_copy(update={"bigquery": settings.bigquery.model_copy(update={"max_bytes_billed": 1})})
    safe = SafeQueryRunner(capped)
    with pytest.raises(AgentError) as err:
        safe.run(SystemQuery(f"SELECT MAX(created_at) AS latest FROM {safe.table_ref('order_items')}"))
    assert err.value.kind == ErrorKind.QUERY_TOO_EXPENSIVE


def test_a_missing_dataset_is_unavailable_with_the_config_hint(settings):
    missing = settings.model_copy(update={"bigquery": settings.bigquery.model_copy(update={"dataset": "bigquery-public-data.no_such_dataset_here"})})
    with pytest.raises(AgentError) as err:
        SafeQueryRunner(missing).get_table("orders")
    assert err.value.kind == ErrorKind.BQ_UNAVAILABLE and "bigquery.dataset" in err.value.detail
