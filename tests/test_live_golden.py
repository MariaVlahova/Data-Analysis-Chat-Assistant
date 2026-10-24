"""Live check for the Golden Bucket seeds (plan 10 acceptance). Manual: `pytest -m live -s tests/test_live_golden.py`.

Needs `gcloud auth application-default login` and GOOGLE_CLOUD_PROJECT. Without credentials every test skips.
Cost: none. Every query is a dry run, which BigQuery does not bill, and the catalog's two small queries are cached.

What the offline tests cannot know: that BigQuery itself accepts the SQL of every seed trio once the SQL Guard has rewritten it, for
a user who sees every brand and for a brand manager (with `@brands` bound to a real brand). The two brand-specific seeds name brands of
the sample data, so against the live dataset they are expected to be left out by the brand check (`golden validate` says so); their SQL
is still dry-run here.
"""
import pytest

from src.access.users import UserContext
from src.config import Settings, load_settings
from src.data_access.brand_check import top_brands
from src.data_access.safe_runner import SafeQueryRunner
from src.golden.schema import load_trio_files
from src.guards.allowlist import load_allowlist
from src.guards.sql_guard import SQLGuard
from tests.golden_support import CEO, REPO_ROOT, SEED_DIR
from tests.test_live_bigquery import _have_credentials

pytestmark = pytest.mark.live

TRIOS = [item.trio for item in load_trio_files(SEED_DIR) if item.trio is not None]


@pytest.fixture(scope="module")
def settings(tmp_path_factory) -> Settings:
    real = load_settings(REPO_ROOT / "config.yaml")
    if not _have_credentials():
        pytest.skip("no Google credentials: run `gcloud auth application-default login` or set GOOGLE_APPLICATION_CREDENTIALS in .env")
    return real.model_copy(update={"app": real.app.model_copy(update={"data_dir": str(tmp_path_factory.mktemp("live-golden"))})})


@pytest.fixture(scope="module")
def safe(settings) -> SafeQueryRunner:
    return SafeQueryRunner(settings)


@pytest.fixture(scope="module")
def guard(settings) -> SQLGuard:
    return SQLGuard(settings, load_allowlist(settings))


@pytest.fixture(scope="module")
def brand_manager(safe) -> UserContext:
    (brand, _), *_ = top_brands(safe, 1)
    return UserContext("live", "Live", "brand_manager", (brand,), False, "live-hash")


def test_there_are_seed_trios_to_check():
    assert len(TRIOS) == 12


@pytest.mark.parametrize("trio", TRIOS, ids=[trio.id for trio in TRIOS])
def test_bigquery_accepts_the_seed_sql_for_everyone_who_may_run_it(trio, guard, safe, brand_manager):
    for user in (CEO, brand_manager):
        info = safe.dry_run(guard.check(trio.sql, user))  # raises if BigQuery refuses the text or the estimate is over the cap
        assert info.bytes_processed >= 0
