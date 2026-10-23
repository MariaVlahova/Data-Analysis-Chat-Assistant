"""Live checks for the SQL Guard (plan 05 acceptance). Manual: `pytest -m live -s`.

Needs `gcloud auth application-default login` and GOOGLE_CLOUD_PROJECT. Without credentials every test skips.
Cost: none. Metadata calls and dry runs are free, and nothing here runs a billed query except the catalog's two small
ones (the latest date and the brand list), which the cache avoids on a second run.

These close the two things the offline tests cannot know:
- Q4: `schema_allowlist.yaml` matches the live columns, and every live column is classified.
- The SQL that sqlglot generates is accepted by BigQuery: every query in `ANALYTIC_QUERIES` is sent as a dry run, once
  as a brand manager (with `@brands` bound to a real brand) and once as the CEO.
"""
import pytest

from src.access.users import UserContext
from src.config import Settings, load_settings
from src.data_access.brand_check import top_brands
from src.data_access.catalog import load_catalog
from src.data_access.safe_runner import SafeQueryRunner
from src.guards.allowlist import Allowlist, load_allowlist
from src.guards.sql_guard import SQLGuard
from tests.guard_support import ANALYTIC_QUERIES, CEO, REPO_ROOT
from tests.test_live_bigquery import _have_credentials

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def settings(tmp_path_factory) -> Settings:
    real = load_settings(REPO_ROOT / "config.yaml")  # reads .env, which may name the project and the key file
    if not _have_credentials():
        pytest.skip("no Google credentials: run `gcloud auth application-default login` or set GOOGLE_APPLICATION_CREDENTIALS in .env")
    return real.model_copy(update={"app": real.app.model_copy(update={"data_dir": str(tmp_path_factory.mktemp("live-data"))})})


@pytest.fixture(scope="module")
def safe(settings) -> SafeQueryRunner:
    return SafeQueryRunner(settings)


@pytest.fixture(scope="module")
def allowlist(settings) -> Allowlist:
    return load_allowlist(settings)


@pytest.fixture(scope="module")
def guard(settings, allowlist) -> SQLGuard:
    return SQLGuard(settings, allowlist)


@pytest.fixture(scope="module")
def brand_manager(safe) -> UserContext:
    (brand, items), *_ = top_brands(safe, 1)
    print(f"\nbrand manager for the live run: {brand} ({items:,} items)")
    return UserContext("live", "Live", "brand_manager", (brand,), False, "live-hash")


def test_the_allow_list_matches_the_live_schema_and_classifies_every_live_column(settings, allowlist):
    catalog = load_catalog(settings).catalog
    unclassified = catalog.reconcile(allowlist)  # raises ConfigError for an allowed column that does not exist live
    live = {table: [c.name for c in columns] for table, columns in catalog.tables.items()}
    print("\nlive columns:", live)
    print("When this and the dry runs below pass and the PII split is agreed with the owner, set reviewed_against_live: true in schema_allowlist.yaml.")
    assert not unclassified, (
        f"live columns in neither list: {', '.join(unclassified)}. Put each in schema_allowlist.yaml as allowed or blocked (Q4, Q11)."
    )


@pytest.mark.parametrize("name", list(ANALYTIC_QUERIES))
def test_bigquery_accepts_the_sql_the_guard_generates_for_a_brand_manager(guard, safe, brand_manager, name):
    gq = guard.check(ANALYTIC_QUERIES[name], brand_manager)
    info = safe.dry_run(gq)  # a real dry run: BigQuery parses the text and binds @brands
    print(f"\n{name}: {info.bytes_processed:,} bytes")
    assert info.bytes_processed >= 0


@pytest.mark.parametrize("name", list(ANALYTIC_QUERIES))
def test_bigquery_accepts_the_sql_the_guard_generates_for_the_ceo(guard, safe, name):
    gq = guard.check(ANALYTIC_QUERIES[name], CEO)
    assert gq.params == ()
    assert safe.dry_run(gq).bytes_processed >= 0


def test_the_scope_views_keep_a_broad_query_far_under_the_byte_cap(guard, safe, brand_manager, settings):
    """The views add joins to every query (plan 05, risks). The tables are small, so this should stay far below the cap."""
    gq = guard.check(ANALYTIC_QUERIES["margin by category"], brand_manager)
    info = safe.dry_run(gq)
    print(f"\nthree-way join through the scope views: {info.bytes_processed:,} bytes of {settings.bigquery.max_bytes_billed:,}")
    assert info.bytes_processed < settings.bigquery.max_bytes_billed / 2
