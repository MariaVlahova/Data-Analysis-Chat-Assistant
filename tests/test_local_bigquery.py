"""The DuckDB stand-in for the BigQuery client (`src/data_access/local_bq.py`): what it reports, what it refuses, and how its failures look.

It is checked here against what `SafeQueryRunner` and the catalog read from a real client, and against how `errors_map` classifies a
failure. It does not prove that BigQuery would behave the same: that is what `pytest -m live` and docs/local-testing.md are for.
"""
import threading
from datetime import date

import pandas as pd
import pytest
from google.api_core import exceptions as api_exc
from google.cloud import bigquery

from src.config import load_settings
from src.data_access import safe_runner
from src.data_access.catalog import load_catalog
from src.data_access.errors_map import classify
from src.data_access.local_bq import LocalBigQueryClient, LocalBigQueryRunner
from src.data_access.safe_runner import SafeQueryRunner, SystemQuery
from src.errors import AgentError, ConfigError, ErrorKind
from src.guards.allowlist import load_allowlist
from src.guards.sql_guard import SQLGuard
from tests.golden_support import CEO, MAPLE, OHARA
from tests.conftest import REAL_MAKE_RUNNER

DATASET = "bigquery-public-data.thelook_ecommerce"
ORDERS = f"`{DATASET}.orders`"
ITEMS = f"`{DATASET}.order_items`"
PRODUCTS = f"`{DATASET}.products`"


@pytest.fixture
def client(local_db):
    return LocalBigQueryClient(local_db.path, DATASET)


@pytest.fixture
def settings(local_project):
    return load_settings(local_project / "config.yaml", load_env_file=False)


@pytest.fixture
def safe(settings):
    return SafeQueryRunner(settings, sleep=lambda seconds: None)


def dry(**config):
    return bigquery.QueryJobConfig(dry_run=True, **config)


def failure(call, phase="dry_run"):
    """The typed error a failure of `call` becomes, the way the runner classifies it."""
    with pytest.raises(Exception) as caught:
        call()
    error = classify(caught.value, phase)
    assert error is not None, repr(caught.value)
    return error


# metadata ----------------------------------------------------------------------------------------------------------------

def test_get_table_gives_the_schema_and_the_size_the_catalog_reads(client):
    table = client.get_table(f"{DATASET}.order_items")
    assert [f.name for f in table.schema][:3] == ["id", "order_id", "user_id"] and table.num_bytes > 100_000
    assert {f.name: f.field_type for f in table.schema}["sale_price"] == "FLOAT"


@pytest.mark.parametrize("ref", [f"{DATASET}.no_such_table", f"other-project.other_dataset.orders", "__meta", f"{DATASET}.__table_stats"])
def test_a_table_that_is_not_one_of_the_four_is_not_found(client, ref):
    with pytest.raises(api_exc.NotFound):
        client.get_table(ref)


def test_the_metadata_tables_of_the_file_are_not_reachable_by_a_query_either(client):
    for name in ("__meta", "__table_stats"):
        with pytest.raises(api_exc.NotFound):
            client.query(f"SELECT * FROM `{DATASET}.{name}`", dry())


def test_the_catalog_loads_from_the_stand_in_and_the_allow_list_fits_it(settings, safe, local_db):
    catalog = load_catalog(settings, safe=safe).catalog.with_policy(load_allowlist(settings))  # raises if a listed column is missing
    assert catalog.latest_date == local_db.end or (local_db.end - catalog.latest_date).days <= 3
    assert {"Maple & Co", "Nordic Peak", "O'Hara Outfitters", "nordic peak", " "} <= catalog.brands
    assert catalog.reconcile(load_allowlist(settings)) == []  # no live column in neither list
    assert all(size > 0 for size in catalog.table_bytes.values())


def test_the_catalog_can_read_the_four_tables_from_four_threads_at_once(client):
    errors = []

    def read(name):
        try:
            assert client.get_table(f"{DATASET}.{name}").schema
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=read, args=(n,)) for n in ("orders", "order_items", "products", "users") * 3]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []


# the dry run -------------------------------------------------------------------------------------------------------------

def test_a_dry_run_reports_a_select_and_the_bytes_of_the_tables_it_reads_and_bills_nothing(client):
    job = client.query(f"SELECT COUNT(*) AS n FROM {ITEMS}", dry())
    assert job.statement_type == "SELECT" and job.total_bytes_processed == client.get_table(f"{DATASET}.order_items").num_bytes
    assert job.total_bytes_billed is None and job.cache_hit is False


def test_the_estimate_adds_each_table_once_however_often_it_is_named(client):
    one = client.query(f"SELECT COUNT(*) FROM {ITEMS} a JOIN {ITEMS} b ON a.id = b.id", dry()).total_bytes_processed
    two = client.query(f"SELECT COUNT(*) FROM {ITEMS} a JOIN {PRODUCTS} p ON a.product_id = p.id", dry()).total_bytes_processed
    assert one == client.get_table(f"{DATASET}.order_items").num_bytes
    assert two == one + client.get_table(f"{DATASET}.products").num_bytes


def test_a_query_with_no_table_costs_nothing(client):
    assert client.query("SELECT 1 AS one", dry()).total_bytes_processed == 0


def test_a_with_clause_is_not_a_table(client):
    job = client.query(f"WITH x AS (SELECT * FROM {ORDERS}) SELECT COUNT(*) FROM x", dry())
    assert job.statement_type == "SELECT" and job.total_bytes_processed == client.get_table(f"{DATASET}.orders").num_bytes


@pytest.mark.parametrize("sql,statement", [
    (f"DROP TABLE {ORDERS}", "DROP_TABLE"), (f"DELETE FROM {ORDERS} WHERE TRUE", "DELETE"),
    (f"INSERT INTO {ORDERS} (order_id) VALUES (1)", "INSERT"), (f"UPDATE {ORDERS} SET status = 'x' WHERE TRUE", "UPDATE"),
])
def test_a_statement_that_is_not_a_select_says_so_in_the_dry_run_and_is_never_run(client, sql, statement):
    assert client.query(sql, dry()).statement_type == statement
    with pytest.raises(api_exc.BadRequest, match="read-only"):
        client.query(sql, bigquery.QueryJobConfig())


def test_the_file_is_read_only_whatever_a_query_says(local_db, client):
    """A second line behind the statement type: even a SELECT-shaped query cannot change the data."""
    cursor = client._cursor()
    try:
        import duckdb

        with pytest.raises(duckdb.Error):
            cursor.execute('DELETE FROM "orders"')
    finally:
        cursor.close()
    assert client.run('SELECT COUNT(*) AS n FROM "orders"', {}, None)["n"].iloc[0] > 0


# failures look like BigQuery's ---------------------------------------------------------------------------------------------

def test_a_syntax_error_is_a_repairable_sql_syntax_error(client):
    error = failure(lambda: client.query("SELEC 1", dry()))
    assert error.kind is ErrorKind.SQL_SYNTAX and error.kind.value == "SQL_SYNTAX"


def test_an_unknown_column_names_the_column_and_is_a_repairable_unknown_column_error(client):
    error = failure(lambda: client.query(f"SELECT oi.nope_column FROM {ITEMS} oi", dry()))
    assert error.kind is ErrorKind.SQL_UNKNOWN_COLUMN and "Unrecognized name: nope_column" in error.detail


def test_an_unknown_function_is_a_repairable_error_that_names_it(client):
    error = failure(lambda: client.query(f"SELECT NO_SUCH_FUNCTION(sale_price) FROM {ITEMS}", dry()))
    assert error.kind in (ErrorKind.SQL_SYNTAX, ErrorKind.SQL_UNKNOWN_COLUMN) and "Function not found: no_such_function" in error.detail


def test_an_unknown_table_is_a_404_the_repair_step_can_fix(client):
    error = failure(lambda: client.query(f"SELECT COUNT(*) FROM `{DATASET}.customers`", dry()))
    assert error.kind is ErrorKind.SQL_UNKNOWN_COLUMN and "customers" in error.detail


def test_a_table_named_with_the_dataset_only_is_read_like_the_full_name(client):
    full = client.query(f"SELECT COUNT(*) AS n FROM {ORDERS}", bigquery.QueryJobConfig()).result().to_dataframe()
    short = client.query("SELECT COUNT(*) AS n FROM thelook_ecommerce.orders", bigquery.QueryJobConfig()).result().to_dataframe()
    assert full["n"].iloc[0] == short["n"].iloc[0] > 0


def test_a_table_of_another_dataset_is_not_found(client):
    assert failure(lambda: client.query("SELECT COUNT(*) FROM `other-project.other.orders`", dry())).kind is ErrorKind.SQL_UNKNOWN_COLUMN


def test_two_statements_are_a_syntax_error(client):
    assert failure(lambda: client.query("SELECT 1; SELECT 2", dry())).kind is ErrorKind.SQL_SYNTAX


# parameters --------------------------------------------------------------------------------------------------------------

def test_an_array_parameter_is_bound_not_pasted_so_a_brand_with_a_quote_or_an_ampersand_is_one_value(client):
    param = bigquery.ArrayQueryParameter("brands", "STRING", ["O'Hara Outfitters", "Maple & Co"])
    job = client.query(f"SELECT DISTINCT brand FROM {PRODUCTS} WHERE brand IN UNNEST(@brands)", bigquery.QueryJobConfig(query_parameters=[param]))
    assert set(job.result().to_dataframe()["brand"]) == {"O'Hara Outfitters", "Maple & Co"}
    assert "O'Hara" not in job._duck_sql and "@brands" not in job._duck_sql  # never in the text; the engine's own parameter


def test_a_scalar_parameter_works(client):
    param = bigquery.ScalarQueryParameter("min_price", "FLOAT64", 100.0)
    job = client.query(f"SELECT COUNT(*) AS n FROM {PRODUCTS} WHERE retail_price > @min_price", bigquery.QueryJobConfig(query_parameters=[param]))
    assert job.result().to_dataframe()["n"].iloc[0] > 0


def test_a_parameter_that_was_not_supplied_is_an_error(client):
    assert failure(lambda: client.query(f"SELECT * FROM {PRODUCTS} WHERE brand IN UNNEST(@brands)", dry())).kind is ErrorKind.SQL_SYNTAX


def test_an_empty_array_matches_nothing_rather_than_everything(client):
    param = bigquery.ArrayQueryParameter("brands", "STRING", [])
    job = client.query(f"SELECT COUNT(*) AS n FROM {PRODUCTS} WHERE brand IN UNNEST(@brands)", bigquery.QueryJobConfig(query_parameters=[param]))
    assert job.result().to_dataframe()["n"].iloc[0] == 0


# the cost gate and the cap -------------------------------------------------------------------------------------------------

def test_a_real_job_over_the_byte_limit_is_refused_with_bigquerys_reason_and_is_too_expensive(client):
    config = bigquery.QueryJobConfig(maximum_bytes_billed=1000)
    error = failure(lambda: client.query(f"SELECT COUNT(*) FROM {ITEMS}", config), "dry_run")
    assert error.kind is ErrorKind.QUERY_TOO_EXPENSIVE


def test_a_real_job_bills_what_the_dry_run_estimated(client):
    sql = f"SELECT COUNT(*) AS n FROM {ITEMS}"
    estimate = client.query(sql, dry()).total_bytes_processed
    job = client.query(sql, bigquery.QueryJobConfig(maximum_bytes_billed=10**9))
    assert job.total_bytes_billed == estimate and not job.dry_run


def test_the_safe_runner_gates_a_query_by_the_estimate_before_it_starts(settings, local_db):
    cheap = load_settings(settings.config_path, load_env_file=False)
    runner = SafeQueryRunner(cheap.model_copy(update={"bigquery": cheap.bigquery.model_copy(update={"max_bytes_billed": 1000})}))
    with pytest.raises(AgentError) as caught:
        runner.run(SystemQuery(f"SELECT COUNT(*) AS n FROM {ITEMS}"))
    assert caught.value.kind is ErrorKind.QUERY_TOO_EXPENSIVE and "over the cap of 1000" in caught.value.detail


def test_the_safe_runner_caps_the_rows_and_reports_the_full_count(settings, local_db):
    small = settings.model_copy(update={"bigquery": settings.bigquery.model_copy(update={"sql_row_limit": 7})})
    outcome = SafeQueryRunner(small).run(SystemQuery(f"SELECT id FROM {ITEMS} ORDER BY id"))
    assert len(outcome.df) == 7 and outcome.total_rows > 7 and outcome.bytes_billed > 0 and outcome.cache_hit is False


def test_a_job_labelled_by_the_runner_keeps_its_labels(client):
    config = bigquery.QueryJobConfig(labels={"app": "opsfleet-agent", "kind": "simple"})
    assert client.query("SELECT 1 AS one", config).labels == {"app": "opsfleet-agent", "kind": "simple"}


def test_a_query_that_runs_past_its_time_limit_is_interrupted_and_is_a_timeout(client):
    heavy = 'SELECT SUM(a.order_id * b.id + c.id) FROM "orders" a, "order_items" b, "order_items" c'
    with pytest.raises(TimeoutError):
        client.run(heavy, {}, 0.05)
    error = failure(lambda: client.run(heavy, {}, 0.05), "query")
    assert error.kind is ErrorKind.QUERY_TIMEOUT
    assert client.run('SELECT 1 AS one', {}, 5.0)["one"].iloc[0] == 1  # and the connection is still good afterwards


# what comes back ---------------------------------------------------------------------------------------------------------

def test_the_dtypes_are_the_ones_the_bigquery_client_gives(client):
    frame = client.query(
        f"SELECT COUNT(*) AS n, SUM(sale_price) AS total, ANY_VALUE(status) AS status, MAX(created_at) AS latest FROM {ITEMS}", bigquery.QueryJobConfig(),
    ).result().to_dataframe()
    assert str(frame["n"].dtype) == "Int64" and str(frame["total"].dtype) == "float64" and frame["status"].dtype == object
    assert str(frame["latest"].dtype).startswith("datetime64") and frame["latest"].dt.tz is not None  # UTC, like a TIMESTAMP


def test_a_sum_over_whole_numbers_is_an_integer_not_a_38_digit_decimal(client):
    frame = client.query(f"SELECT SUM(num_of_item) AS items FROM {ORDERS}", bigquery.QueryJobConfig()).result().to_dataframe()
    assert str(frame["items"].dtype) == "Int64" and frame["items"].iloc[0] > 0


def test_a_null_stays_a_null(client):
    frame = client.query(f"SELECT brand FROM {PRODUCTS} WHERE brand IS NULL LIMIT 3", bigquery.QueryJobConfig()).result().to_dataframe()
    assert len(frame) == 3 and frame["brand"].isna().all()


def test_text_with_accents_and_symbols_survives(client):
    frame = client.query(f"SELECT DISTINCT state FROM `{DATASET}.users` WHERE state IN ('São Paulo', 'Île-de-France')", bigquery.QueryJobConfig()).result().to_dataframe()
    assert set(frame["state"]) == {"São Paulo", "Île-de-France"}


# the BigQuery dialect the model writes ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("sql", [
    f"SELECT FORMAT_DATE('%Y-%m', DATE(created_at)) AS m, SUM(sale_price) AS s FROM {ITEMS} GROUP BY m ORDER BY m",
    f"SELECT DATE_TRUNC(DATE(created_at), MONTH) AS m, COUNT(*) AS n FROM {ITEMS} GROUP BY m",
    f"SELECT COUNTIF(status = 'Returned') AS r, SAFE_DIVIDE(COUNTIF(status = 'Returned'), COUNT(*)) AS rate FROM {ITEMS}",
    f"SELECT COUNT(*) AS n FROM {ITEMS} WHERE created_at >= TIMESTAMP_SUB(TIMESTAMP '2026-10-22', INTERVAL 90 DAY)",
    f"SELECT EXTRACT(YEAR FROM created_at) AS y, COUNT(*) AS n FROM {ITEMS} GROUP BY y",
    f"SELECT APPROX_QUANTILES(sale_price, 4)[OFFSET(2)] AS median FROM {ITEMS}",
    f"SELECT CAST(sale_price AS INT64) AS p, ROUND(AVG(sale_price), 2) AS a FROM {ITEMS} GROUP BY p LIMIT 5",
    f"SELECT p.category, SUM(oi.sale_price) OVER (PARTITION BY p.category) AS s FROM {ITEMS} oi JOIN {PRODUCTS} p ON p.id = oi.product_id LIMIT 5",
    f"SELECT user_id, IFNULL(MAX(returned_at), TIMESTAMP '1970-01-01') AS last_return FROM {ITEMS} GROUP BY user_id LIMIT 5",
    f"WITH m AS (SELECT DATE_TRUNC(DATE(created_at), MONTH) AS month, SUM(sale_price) AS revenue FROM {ITEMS} GROUP BY month) "
    "SELECT month, revenue, LAG(revenue) OVER (ORDER BY month) AS previous FROM m ORDER BY month",
    f"SELECT STRING_AGG(DISTINCT status, ', ') AS statuses FROM {ITEMS}",
])
def test_the_sql_a_writer_usually_produces_runs(client, sql):
    frame = client.query(sql, bigquery.QueryJobConfig()).result().to_dataframe()
    assert len(frame) >= 1


def test_the_months_come_out_as_dates_in_utc(client):
    frame = client.query(f"SELECT DATE_TRUNC(DATE(created_at), MONTH) AS m FROM {ITEMS} GROUP BY m ORDER BY m", bigquery.QueryJobConfig()).result().to_dataframe()
    assert frame["m"].iloc[0] == date(2023, 1, 1)


# the whole path through the guard --------------------------------------------------------------------------------------------

def run_guarded(settings, safe, user, sql):
    guard = SQLGuard(settings, load_allowlist(settings))
    return safe.run(guard.check(sql, user))


def test_the_guarded_sql_runs_and_a_brand_manager_sees_only_their_brands(settings, safe):
    sql = f"SELECT p.brand, SUM(oi.sale_price) AS revenue FROM order_items oi JOIN products p ON p.id = oi.product_id GROUP BY p.brand ORDER BY revenue DESC"
    alice = run_guarded(settings, safe, MAPLE, sql).df
    bob = run_guarded(settings, safe, OHARA, sql).df
    ceo = run_guarded(settings, safe, CEO, sql).df
    assert set(alice["brand"]) == {"Maple & Co", "Nordic Peak"} and set(bob["brand"]) == {"O'Hara Outfitters"}
    assert set(alice["brand"]).isdisjoint(bob["brand"]) and len(ceo) > len(alice) + len(bob)
    assert "nordic peak" in set(ceo["brand"]) and "nordic peak" not in set(alice["brand"])  # an exact match, not a lower-case one


def test_the_runner_accepts_only_what_the_guard_issued_here_too(safe):
    class Fake:
        sql = "SELECT 1"
        params = ()

    with pytest.raises(AgentError) as caught:
        safe.run(Fake())
    assert caught.value.rule_id == "UNGUARDED_QUERY"


# the switch ------------------------------------------------------------------------------------------------------------------

def test_make_runner_gives_the_stand_in_when_the_config_says_local(settings):
    runner = REAL_MAKE_RUNNER(settings)
    assert isinstance(runner, LocalBigQueryRunner) and isinstance(runner.client, LocalBigQueryClient)


def test_make_runner_still_gives_the_real_client_by_default(monkeypatch):
    from src.config import Settings

    made = []
    monkeypatch.setattr(safe_runner, "BigQueryRunner", lambda project_id=None: made.append(project_id) or object())
    REAL_MAKE_RUNNER(Settings())
    assert made == [None]


def test_missing_sample_data_stops_with_the_command_that_builds_it(project, tmp_path, monkeypatch):
    monkeypatch.setattr(safe_runner, "make_runner", REAL_MAKE_RUNNER)
    (project / "config.yaml").write_text(f'bigquery:\n  backend: local\n  local_db: "{(tmp_path / "missing.duckdb").as_posix()}"\n', encoding="utf-8")
    settings = load_settings(project / "config.yaml", load_env_file=False)
    with pytest.raises(ConfigError) as caught:
        load_catalog(settings)
    assert "was not found" in caught.value.message and "local-data build" in caught.value.fix


def test_a_file_that_is_not_the_sample_data_is_refused(project, tmp_path, monkeypatch):
    import duckdb

    monkeypatch.setattr(safe_runner, "make_runner", REAL_MAKE_RUNNER)
    other = tmp_path / "other.duckdb"
    connection = duckdb.connect(str(other))
    connection.execute("CREATE TABLE unrelated (x INT)")
    connection.close()
    (project / "config.yaml").write_text(f'bigquery:\n  backend: local\n  local_db: "{other.as_posix()}"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="could not be read"):
        load_catalog(load_settings(project / "config.yaml", load_env_file=False))


def test_the_rows_returned_are_a_dataframe_and_never_a_bigquery_object(client):
    assert isinstance(client.query("SELECT 1 AS one", bigquery.QueryJobConfig()).result().to_dataframe(create_bqstorage_client=False), pd.DataFrame)
