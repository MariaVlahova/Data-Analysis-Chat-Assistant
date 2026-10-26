"""Schema catalog (plan 03): live read, 24-hour cache, fallback when BigQuery is down, allow-list reconcile."""
import json
import logging
import threading
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest
from google.api_core import exceptions as api_exc

from src.config import load_settings
from src.data_access.catalog import (
    BRAND_ROW_LIMIT, CACHE_FALLBACK_MAX_AGE, CACHE_MAX_AGE, ColumnInfo, SchemaCatalog, googlesql_type, load_catalog,
)
from src.errors import ConfigError
from tests.fake_bigquery import FAKE_BRANDS, FAKE_COLUMNS, FAKE_TABLE_BYTES, make_table

NOW = datetime(2026, 10, 23, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def settings(project):
    return load_settings(project / "config.yaml", load_env_file=False)


@pytest.fixture
def cache_dir(settings):
    return settings.data_path / "cache"


class FakePolicy:
    """Stand-in for plan 05's allow-list."""

    def __init__(self, allowed, blocked=None, descriptions=None):
        self._allowed, self._blocked, self._descriptions = allowed, blocked or {}, descriptions or {}

    def table_names(self):
        return list(self._allowed)

    def allowed(self, table):
        return self._allowed.get(table, [])

    def blocked(self, table):
        return self._blocked.get(table, [])

    def describe(self, table, column):
        return self._descriptions.get((table, column), "")


POLICY = FakePolicy(
    allowed={
        "orders": ["order_id", "user_id", "status", "created_at", "num_of_item"],
        "order_items": ["id", "order_id", "product_id", "sale_price"],
        "products": ["id", "category", "brand", "retail_price"],
        "users": ["id", "age", "created_at"],
    },
    blocked={"users": ["first_name", "last_name", "email", "street_address", "postal_code", "latitude", "longitude", "user_geom"]},
    descriptions={("orders", "status"): "the order's current state", ("products", "brand"): "the brand name"},
)


# loading and caching -----------------------------------------------------------------------------

def test_first_load_reads_bigquery_and_saves_both_cache_files(settings, cache_dir, fake_bigquery):
    loaded = load_catalog(settings, now=NOW)
    catalog = loaded.catalog
    assert loaded.warnings == ()
    assert set(catalog.tables) == {"orders", "order_items", "products", "users"}
    assert [c.name for c in catalog.tables["products"]] == [name for name, _ in FAKE_COLUMNS["products"]]
    assert catalog.tables["products"][4] == ColumnInfo("brand", "STRING", "NULLABLE", "")
    assert catalog.tables["order_items"][-1] == ColumnInfo("sale_price", "FLOAT64", "NULLABLE", "")  # the API says FLOAT
    assert catalog.brands == frozenset(FAKE_BRANDS)
    assert catalog.latest_date == date(2026, 10, 22)
    assert catalog.table_bytes == FAKE_TABLE_BYTES
    assert catalog.checked_at == NOW
    assert json.loads((cache_dir / "schema.json").read_text(encoding="utf-8"))["latest_date"] == "2026-10-22"
    assert sorted(json.loads((cache_dir / "brands.json").read_text(encoding="utf-8"))["brands"]) == sorted(FAKE_BRANDS)


def test_metadata_calls_scan_nothing_and_the_three_queries_are_small(settings, fake_bigquery):
    load_catalog(settings, now=NOW)
    assert len(fake_bigquery.table_calls) == 4
    assert [j.sql for j in fake_bigquery.real_jobs] == [
        "SELECT MAX(created_at) AS latest FROM `bigquery-public-data.thelook_ecommerce.order_items`",
        "SELECT DISTINCT brand FROM `bigquery-public-data.thelook_ecommerce.products` WHERE brand IS NOT NULL",
        "SELECT DISTINCT category, department FROM `bigquery-public-data.thelook_ecommerce.products`",
    ]
    assert all(j.config.labels["user"] == "system" for j in fake_bigquery.real_jobs)


# product categories and departments (plan 12, A1) -------------------------------------------------

def test_the_product_categories_and_departments_are_read_and_saved(settings, cache_dir, fake_bigquery):
    catalog = load_catalog(settings, now=NOW).catalog
    assert catalog.product_words == frozenset({"Jeans", "Tops & Tees", "Women", "Men"})
    assert sorted(json.loads((cache_dir / "product_words.json").read_text(encoding="utf-8"))["words"]) == ["Jeans", "Men", "Tops & Tees", "Women"]


def test_a_failed_read_of_the_product_words_leaves_them_empty_and_never_stops_the_load(settings, cache_dir, fake_bigquery):
    fake_bigquery.add("SELECT DISTINCT category", query_errors=[api_exc.BadRequest("bad")] * 5, dry_errors=[api_exc.BadRequest("bad")] * 5)
    loaded = load_catalog(settings, now=NOW)
    assert loaded.catalog.product_words == frozenset() and loaded.warnings == ()
    assert loaded.catalog.brands == frozenset(FAKE_BRANDS)  # the rest of the catalog is whole
    assert not (cache_dir / "product_words.json").exists()


def test_the_saved_product_words_are_the_fallback_when_a_fresh_read_fails(settings, fake_bigquery):
    load_catalog(settings, now=NOW)
    fake_bigquery.add("SELECT DISTINCT category", query_errors=[api_exc.BadRequest("bad")] * 5, dry_errors=[api_exc.BadRequest("bad")] * 5)
    later = NOW + CACHE_MAX_AGE + timedelta(minutes=1)
    assert load_catalog(settings, now=later).catalog.product_words == frozenset({"Jeans", "Tops & Tees", "Women", "Men"})


def test_a_dataset_without_category_and_department_columns_has_no_product_words(settings, fake_bigquery):
    columns = [(name, typ) for name, typ in FAKE_COLUMNS["products"] if name not in ("category", "department")]
    fake_bigquery.tables["products"] = make_table("products", columns, FAKE_TABLE_BYTES["products"])
    assert load_catalog(settings, now=NOW).catalog.product_words == frozenset()
    assert not [job for job in fake_bigquery.real_jobs if "category" in job.sql]


def test_a_copy_under_24_hours_old_is_used_without_calling_bigquery(settings, fake_bigquery):
    load_catalog(settings, now=NOW)
    calls = (len(fake_bigquery.jobs), len(fake_bigquery.table_calls))
    again = load_catalog(settings, now=NOW + CACHE_MAX_AGE - timedelta(minutes=1))
    assert (len(fake_bigquery.jobs), len(fake_bigquery.table_calls)) == calls
    assert again.catalog.brands == frozenset(FAKE_BRANDS) and again.catalog.latest_date == date(2026, 10, 22)
    assert again.warnings == ()


def test_a_copy_older_than_24_hours_is_read_again(settings, fake_bigquery):
    load_catalog(settings, now=NOW)
    before = len(fake_bigquery.table_calls)
    later = NOW + CACHE_MAX_AGE + timedelta(minutes=1)
    assert load_catalog(settings, now=later).catalog.checked_at == later
    assert len(fake_bigquery.table_calls) == before + 4


def test_refresh_reads_again_even_when_the_copy_is_fresh(settings, fake_bigquery):
    load_catalog(settings, now=NOW)
    before = len(fake_bigquery.table_calls)
    load_catalog(settings, refresh=True, now=NOW + timedelta(minutes=1))
    assert len(fake_bigquery.table_calls) == before + 4


def test_only_the_stale_part_is_read_again(settings, cache_dir, fake_bigquery):
    load_catalog(settings, now=NOW)
    (cache_dir / "brands.json").unlink()
    jobs, tables = len(fake_bigquery.real_jobs), len(fake_bigquery.table_calls)
    load_catalog(settings, now=NOW + timedelta(hours=1))
    assert len(fake_bigquery.table_calls) == tables  # the schema copy was fresh
    assert [j.sql for j in fake_bigquery.real_jobs[jobs:]] == [fake_bigquery.real_jobs[1].sql]  # only the brand query ran (the product words were fresh)


def test_a_cache_from_another_dataset_is_ignored(settings, cache_dir, fake_bigquery):
    load_catalog(settings, now=NOW)
    for name in ("schema.json", "brands.json"):
        path = cache_dir / name
        data = json.loads(path.read_text(encoding="utf-8"))
        data["dataset"] = "someone-else.dataset"
        path.write_text(json.dumps(data), encoding="utf-8")
    before = len(fake_bigquery.table_calls)
    load_catalog(settings, now=NOW + timedelta(minutes=1))
    assert len(fake_bigquery.table_calls) == before + 4


@pytest.mark.parametrize("content", ["not json", "[]", '{"version": 1}', '{"version": 99, "dataset": "bigquery-public-data.thelook_ecommerce"}'])
def test_a_damaged_or_unknown_cache_file_is_treated_as_no_cache(settings, cache_dir, fake_bigquery, content):
    load_catalog(settings, now=NOW)
    (cache_dir / "schema.json").write_text(content, encoding="utf-8")
    (cache_dir / "brands.json").write_text(content, encoding="utf-8")
    assert load_catalog(settings, now=NOW + timedelta(minutes=1)).catalog.brands == frozenset(FAKE_BRANDS)


def test_a_cache_with_the_right_shape_but_wrong_content_is_treated_as_no_cache(settings, cache_dir, fake_bigquery):
    load_catalog(settings, now=NOW)
    data = json.loads((cache_dir / "schema.json").read_text(encoding="utf-8"))
    data["tables"]["orders"] = {"columns": "oops"}
    (cache_dir / "schema.json").write_text(json.dumps(data), encoding="utf-8")
    assert load_catalog(settings, now=NOW + timedelta(minutes=1)).catalog.table_bytes == FAKE_TABLE_BYTES


def test_a_cache_folder_that_cannot_be_written_does_not_stop_the_load(settings, fake_bigquery):
    settings.data_path.mkdir(parents=True, exist_ok=True)
    (settings.data_path / "cache").write_text("a file where the folder should be", encoding="utf-8")
    assert load_catalog(settings, now=NOW).catalog.brands == frozenset(FAKE_BRANDS)


def test_brand_names_keep_their_exact_spelling(settings, fake_bigquery):
    fake_bigquery.add("SELECT DISTINCT brand", df=pd.DataFrame({"brand": ["Nike", "Nike ", "nike"]}))
    assert load_catalog(settings, now=NOW).catalog.brands == frozenset({"Nike", "Nike ", "nike"})


# BigQuery down: saved copy or stop (NFR15) -------------------------------------------------------

def test_bigquery_down_with_a_saved_copy_uses_it_and_says_so(settings, fake_bigquery):
    load_catalog(settings, now=NOW)
    fake_bigquery.down = True
    loaded = load_catalog(settings, now=NOW + timedelta(days=2))
    assert loaded.catalog.brands == frozenset(FAKE_BRANDS) and loaded.catalog.checked_at == NOW
    assert len(loaded.warnings) == 2
    assert "BigQuery could not be reached" in loaded.warnings[0] and "2026-10-23" in loaded.warnings[0]


def test_bigquery_down_with_no_saved_copy_stops_startup(settings, fake_bigquery):
    fake_bigquery.down = True
    with pytest.raises(ConfigError) as err:
        load_catalog(settings, now=NOW)
    assert "no saved schema" in err.value.message and "BQ_UNAVAILABLE" in err.value.message
    assert "doctor" in err.value.fix


def test_a_saved_schema_but_no_saved_brand_list_still_stops(settings, cache_dir, fake_bigquery):
    load_catalog(settings, now=NOW)
    (cache_dir / "brands.json").unlink()
    fake_bigquery.add("SELECT DISTINCT brand", dry_errors=[api_exc.ServiceUnavailable("503")] * 50)
    with pytest.raises(ConfigError) as err:
        load_catalog(settings, now=NOW + timedelta(hours=1))
    assert "no saved brand list" in err.value.message


def test_a_forced_refresh_that_fails_falls_back_with_a_warning(settings, fake_bigquery):
    load_catalog(settings, now=NOW)
    fake_bigquery.down = True
    loaded = load_catalog(settings, refresh=True, now=NOW + timedelta(minutes=5))
    assert len(loaded.warnings) == 2


def test_a_truncated_brand_list_stops_instead_of_rejecting_real_brands(settings, fake_bigquery):
    fake_bigquery.add("SELECT DISTINCT brand", df=pd.DataFrame({"brand": ["a", "b"]}), total_rows=BRAND_ROW_LIMIT + 1)
    with pytest.raises(ConfigError) as err:
        load_catalog(settings, now=NOW)
    assert "only 2 were read" in err.value.message


def test_the_brand_query_is_allowed_more_rows_than_an_answer(settings, fake_bigquery):
    load_catalog(settings, now=NOW)
    assert fake_bigquery.real_jobs[1].result_kwargs["max_results"] == BRAND_ROW_LIMIT


def test_a_dataset_without_order_items_stops_with_a_clear_message(settings, fake_bigquery):
    fake_bigquery.add("MAX(created_at)", df=pd.DataFrame({"latest": [pd.NaT]}))
    with pytest.raises(ConfigError) as err:
        load_catalog(settings, now=NOW)
    assert "no rows" in err.value.message


# freshness (A5) ----------------------------------------------------------------------------------

def catalog_ending(latest: date) -> SchemaCatalog:
    return SchemaCatalog({}, frozenset(), latest, {}, NOW, NOW)


def test_reference_date_is_today_or_the_last_day_of_data():
    catalog = catalog_ending(date(2026, 10, 20))
    assert catalog.reference_date(today=date(2026, 10, 23)) == date(2026, 10, 20)
    assert catalog.reference_date(today=date(2026, 10, 19)) == date(2026, 10, 19)  # data from the future: today wins


def test_the_answer_must_mention_data_that_ends_more_than_a_week_ago():
    catalog = catalog_ending(date(2026, 10, 1))
    note = catalog.freshness_note(today=date(2026, 10, 23))
    assert note is not None and "2026-10-01" in note
    assert catalog.freshness_note(today=date(2026, 10, 8)) is None  # exactly 7 days: no note
    assert catalog.freshness_note(today=date(2026, 10, 9)) is not None


# facts for doctor --------------------------------------------------------------------------------

def test_string_columns_and_the_largest_table(settings, fake_bigquery):
    catalog = load_catalog(settings, now=NOW).catalog
    assert catalog.string_columns()["orders"] == ["status", "gender"]
    assert "email" in catalog.string_columns()["users"] and "created_at" not in catalog.string_columns()["users"]
    assert catalog.largest_table() == ("order_items", 42_000_000)
    assert catalog_ending(date(2026, 1, 1)).largest_table() is None


# the allow-list (plan 05 attaches it) ------------------------------------------------------------

@pytest.fixture
def catalog(settings, fake_bigquery):
    return load_catalog(settings, now=NOW).catalog


def test_without_an_allow_list_nothing_can_be_shown(catalog):
    for call in (catalog.overview, lambda: catalog.prune(["orders"]), lambda: catalog.visible_columns("orders")):
        with pytest.raises(RuntimeError):
            call()


def test_overview_lists_only_allow_listed_columns_with_our_descriptions(catalog):
    text = catalog.with_policy(POLICY).overview()
    assert "orders" in text and "  - status (STRING): the order's current state" in text
    assert "  - brand (STRING): the brand name" in text
    assert "  - num_of_item (INT64)" in text  # no description: just the name and type
    for hidden in ("email", "first_name", "street_address", "latitude", "user_geom", "gender", "state"):
        assert f"  - {hidden} (" not in text, hidden  # blocked or unclassified: never listed
    assert "email" not in text and "user_geom" not in text


def test_prune_returns_just_the_requested_tables_and_allowed_columns(catalog):
    policy_catalog = catalog.with_policy(POLICY)
    assert policy_catalog.prune(["products", "order_items"]) == (
        "products(id INT64, category STRING, brand STRING, retail_price FLOAT64)\n"
        "order_items(id INT64, order_id INT64, product_id INT64, sale_price FLOAT64)"
    )
    assert "email" not in policy_catalog.prune(["users"]) and policy_catalog.prune(["users"]) == "users(id INT64, age INT64, created_at TIMESTAMP)"


def test_prune_leaves_out_unknown_tables_and_repeats(catalog):
    assert catalog.with_policy(POLICY).prune(["orders", "payments", "orders", "INFORMATION_SCHEMA"]).count("\n") == 0


def test_an_allow_listed_column_missing_from_the_live_schema_stops_startup(catalog):
    wrong = FakePolicy({"orders": ["order_id", "revenue"], "users": ["id"]})
    with pytest.raises(ConfigError) as err:
        catalog.with_policy(wrong)
    assert "orders.revenue" in err.value.message and "order_id" not in err.value.message
    assert "doctor --refresh" in err.value.fix


def test_an_allow_listed_table_that_does_not_exist_stops_startup(catalog):
    with pytest.raises(ConfigError) as err:
        catalog.with_policy(FakePolicy({"payments": ["id"]}))
    assert "table payments" in err.value.message


def test_a_new_live_column_in_neither_list_is_logged_and_treated_as_blocked(catalog, caplog):
    with caplog.at_level(logging.WARNING, logger="src.data_access.catalog"):
        unclassified = catalog.reconcile(POLICY)
    assert "orders.gender" in unclassified and "users.gender" in unclassified and "products.name" in unclassified
    assert "users.email" not in unclassified  # blocked on purpose
    assert "unclassified, treated as blocked" in caplog.text and "users.gender" in caplog.text
    assert "gender" not in catalog.with_policy(POLICY).prune(["users"])


def test_a_table_missing_from_the_policy_is_entirely_unclassified(catalog):
    unclassified = catalog.reconcile(FakePolicy({"orders": ["order_id"]}))
    assert "users.id" in unclassified and "products.brand" in unclassified
    assert catalog.with_policy(FakePolicy({"orders": ["order_id"]})).prune(["users", "orders"]) == "orders(order_id INT64)"


# types for prompts -------------------------------------------------------------------------------

@pytest.mark.parametrize("api_type,mode,expected", [
    ("INTEGER", "NULLABLE", "INT64"), ("FLOAT", "REQUIRED", "FLOAT64"), ("BOOLEAN", "NULLABLE", "BOOL"),
    ("RECORD", "NULLABLE", "STRUCT"), ("STRING", "NULLABLE", "STRING"), ("TIMESTAMP", "NULLABLE", "TIMESTAMP"),
    ("NUMERIC", "NULLABLE", "NUMERIC"), ("INTEGER", "REPEATED", "ARRAY<INT64>"), ("float", "NULLABLE", "FLOAT64"),
])
def test_api_type_names_become_googlesql_names(api_type, mode, expected):
    assert googlesql_type(api_type, mode) == expected


def test_the_prompt_text_never_shows_a_legacy_type_name(catalog):
    text = catalog.with_policy(POLICY).prune(["orders", "order_items", "products", "users"])
    assert "INT64" in text and "FLOAT64" in text
    for legacy in ("INTEGER", " FLOAT ", "FLOAT)", "BOOLEAN", "RECORD"):
        assert legacy not in text


# how long a saved copy is trusted when BigQuery cannot be reached --------------------------------

def test_a_saved_copy_a_week_old_is_still_used_but_not_older(settings, fake_bigquery):
    load_catalog(settings, now=NOW)
    fake_bigquery.down = True
    assert len(load_catalog(settings, now=NOW + CACHE_FALLBACK_MAX_AGE).warnings) == 2
    with pytest.raises(ConfigError) as err:
        load_catalog(settings, now=NOW + CACHE_FALLBACK_MAX_AGE + timedelta(hours=1))
    assert "7 days old, too old to trust" in err.value.message and "doctor" in err.value.fix


@pytest.mark.parametrize("failure", [
    api_exc.Forbidden("Access Denied"),
    api_exc.NotFound("Not found: Dataset x"),
    api_exc.Unauthorized("expired"),
])
def test_an_error_that_is_not_an_outage_never_falls_back_to_the_saved_copy(settings, fake_bigquery, failure):
    load_catalog(settings, now=NOW)
    fake_bigquery.table_errors.append(failure)
    with pytest.raises(ConfigError) as err:
        load_catalog(settings, now=NOW + timedelta(days=2))
    assert "BigQuery cannot be used until the setup is fixed" in err.value.message and "is not used" in err.value.message


def test_a_cap_that_became_too_low_is_not_hidden_by_the_saved_copy(settings, fake_bigquery):
    load_catalog(settings, now=NOW)
    fake_bigquery.add("MAX(created_at)", bytes_processed=600_000_000)
    with pytest.raises(ConfigError) as err:
        load_catalog(settings, now=NOW + timedelta(days=2))
    assert "QUERY_TOO_EXPENSIVE" in err.value.message


# no saved copy: a refusal is not an outage ---------------------------------------------------------------------

def test_without_a_saved_copy_a_refused_query_is_not_called_an_outage(settings, fake_bigquery):
    fake_bigquery.add("MAX(created_at)", bytes_processed=600_000_000)  # over the 512 MiB cap
    with pytest.raises(ConfigError) as err:
        load_catalog(settings, now=NOW)
    assert "BigQuery refused the request (QUERY_TOO_EXPENSIVE" in err.value.message
    assert "could not be reached" not in err.value.message
    assert "connection" not in err.value.fix and "Fix the problem the message names" in err.value.fix


@pytest.mark.parametrize("failure", [api_exc.Forbidden("Access Denied"), api_exc.NotFound("Not found: Dataset x")])
def test_without_a_saved_copy_access_and_setup_errors_send_you_to_the_setup_not_the_network(settings, fake_bigquery, failure):
    fake_bigquery.table_errors.append(failure)
    with pytest.raises(ConfigError) as err:
        load_catalog(settings, now=NOW)
    assert "BigQuery cannot be used until the setup is fixed" in err.value.message and "BQ_UNAVAILABLE" in err.value.message
    assert "could not be reached" not in err.value.message and "doctor shows the setup" in err.value.fix


# one outage, one round of retries ------------------------------------------------------------------------------

def test_during_an_outage_the_brand_list_does_not_wait_through_the_retries_again(settings, fake_bigquery):
    load_catalog(settings, now=NOW)
    fake_bigquery.down = True
    tables, jobs = len(fake_bigquery.table_calls), len(fake_bigquery.jobs)
    loaded = load_catalog(settings, now=NOW + timedelta(days=2))  # both saved copies are stale
    # The four metadata reads run together and each tries 3 times (transient_retries is 2): one round of waiting.
    assert len(fake_bigquery.table_calls) - tables == 12
    assert len(fake_bigquery.jobs) == jobs  # the brand query was never tried
    assert len(loaded.warnings) == 2 and loaded.catalog.brands == frozenset(FAKE_BRANDS)


def test_an_outage_found_by_the_schema_part_still_stops_when_there_is_no_saved_brand_list(settings, cache_dir, fake_bigquery):
    load_catalog(settings, now=NOW)
    (cache_dir / "brands.json").unlink()
    fake_bigquery.down = True
    jobs = len(fake_bigquery.jobs)
    with pytest.raises(ConfigError) as err:
        load_catalog(settings, now=NOW + timedelta(days=2))
    assert "no saved brand list" in err.value.message and len(fake_bigquery.jobs) == jobs


# a copy dated in the future ------------------------------------------------------------------------------------

def test_a_cache_dated_in_the_future_is_read_again_not_trusted_until_the_clock_catches_up(settings, fake_bigquery):
    load_catalog(settings, now=NOW + timedelta(days=30))  # a clock that was set ahead once
    tables = len(fake_bigquery.table_calls)
    loaded = load_catalog(settings, now=NOW)
    assert len(fake_bigquery.table_calls) == tables + 4 and loaded.catalog.checked_at == NOW


def test_a_future_dated_copy_is_not_a_fallback_either(settings, fake_bigquery):
    load_catalog(settings, now=NOW + timedelta(days=30))
    fake_bigquery.down = True
    with pytest.raises(ConfigError) as err:
        load_catalog(settings, now=NOW)
    assert "no saved schema" in err.value.message


def test_a_few_minutes_of_clock_difference_is_tolerated(settings, fake_bigquery):
    load_catalog(settings, now=NOW + timedelta(minutes=2))
    tables = len(fake_bigquery.table_calls)
    load_catalog(settings, now=NOW)
    assert len(fake_bigquery.table_calls) == tables


# columns the startup SQL reads ---------------------------------------------------------------------------------

def test_a_missing_column_is_named_before_any_query_runs(settings, fake_bigquery):
    without_brand = [(name, kind) for name, kind in FAKE_COLUMNS["products"] if name != "brand"]
    fake_bigquery.tables["products"] = make_table("products", without_brand, 1_000)
    with pytest.raises(ConfigError) as err:
        load_catalog(settings, now=NOW)
    assert "products.brand" in err.value.message and "Q4" in err.value.fix
    assert fake_bigquery.jobs == []  # no query ran, so it cannot be mistaken for an outage or a refusal


def test_a_saved_copy_that_lacks_a_required_column_stops_before_the_brand_query(settings, cache_dir, fake_bigquery):
    load_catalog(settings, now=NOW)
    (cache_dir / "brands.json").unlink()
    data = json.loads((cache_dir / "schema.json").read_text(encoding="utf-8"))
    data["tables"]["order_items"]["columns"] = [c for c in data["tables"]["order_items"]["columns"] if c["name"] != "product_id"]
    (cache_dir / "schema.json").write_text(json.dumps(data), encoding="utf-8")
    jobs = len(fake_bigquery.jobs)
    with pytest.raises(ConfigError) as err:
        load_catalog(settings, now=NOW + timedelta(hours=1))
    assert "order_items.product_id" in err.value.message and len(fake_bigquery.jobs) == jobs


# the A21 list ---------------------------------------------------------------------------------------------------

def test_free_text_columns_include_arrays_of_strings_and_json():
    columns = {"t": [ColumnInfo("note", "STRING"), ColumnInfo("tags", "ARRAY<STRING>", "REPEATED"), ColumnInfo("blob", "JSON"),
                     ColumnInfo("n", "INT64"), ColumnInfo("ids", "ARRAY<INT64>", "REPEATED"), ColumnInfo("at", "TIMESTAMP")]}
    catalog = SchemaCatalog(columns, frozenset(), date(2026, 1, 1), {}, NOW, NOW)
    assert catalog.string_columns() == {"t": ["note", "tags", "blob"]}


# the four metadata reads run together ------------------------------------------------------------------------------

def test_the_four_metadata_reads_run_at_the_same_time(settings, fake_bigquery, monkeypatch):
    barrier = threading.Barrier(4, timeout=5)  # serial reads would wait here for the others and time out
    real = fake_bigquery.get_table

    def waits_for_the_others(ref, **kwargs):
        barrier.wait()
        return real(ref, **kwargs)

    monkeypatch.setattr(fake_bigquery, "get_table", waits_for_the_others)
    catalog = load_catalog(settings, now=NOW).catalog
    assert list(catalog.tables) == ["orders", "order_items", "products", "users"]  # in table order, whoever finishes first
    assert catalog.table_bytes == FAKE_TABLE_BYTES


def test_one_failing_read_among_the_four_stops_the_load_with_its_error(settings, fake_bigquery):
    fake_bigquery.table_errors.append(api_exc.Forbidden("Access Denied"))
    with pytest.raises(ConfigError) as err:
        load_catalog(settings, now=NOW)
    assert "cannot be used until the setup is fixed" in err.value.message


# a cache write that fails ---------------------------------------------------------------------------------------------

def test_a_cache_write_that_fails_leaves_no_temp_file_and_the_load_still_works(settings, cache_dir, fake_bigquery, monkeypatch):
    import os

    def locked(src, dst):
        raise PermissionError("held by a virus scanner")

    monkeypatch.setattr(os, "replace", locked)
    assert load_catalog(settings, now=NOW).catalog.brands == frozenset(FAKE_BRANDS)
    assert not cache_dir.exists() or [p.name for p in cache_dir.iterdir()] == []


# identity, not field-wise equality ------------------------------------------------------------------------------------

def test_the_catalog_can_be_hashed_and_cached_by_the_prompt_builders(settings, fake_bigquery):
    import functools

    first = load_catalog(settings, now=NOW).catalog
    second = load_catalog(settings, now=NOW).catalog  # same content, loaded again
    assert {first: "a"}[first] == "a" and len({first, second}) == 2 and first == first and first != second

    calls = []

    @functools.lru_cache(maxsize=None)
    def build_prompt(catalog):
        calls.append(1)
        return len(catalog.tables)

    assert build_prompt(first) == 4 and build_prompt(first) == 4 and len(calls) == 1
