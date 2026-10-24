"""The generated sample data (`src/localdata`): the same every time, shaped like thelook_ecommerce, and holding what the tests and demos need.

These tests read the file with DuckDB directly, as the truth the app's answers are compared with elsewhere.
"""
import hashlib
import re
from datetime import date

import duckdb
import pytest
import yaml
from typer.testing import CliRunner

from src import pii
from src.cli.main import app
from src.localdata import schema
from src.localdata.generate import (
    BRANDS, DECLINING_BRAND, ITEMS_WITHOUT_PRODUCT, OLD_AGE_USERS, RETURNING_BRAND, RETURNING_CATEGORY, BuildOptions, default_end, generate,
)
from tests.golden_support import REPO_ROOT

runner = CliRunner(env={"COLUMNS": "250"})
END = date(2026, 10, 22)


@pytest.fixture(scope="module")
def con(local_db):
    connection = duckdb.connect(str(local_db.path), read_only=True)
    connection.execute("SET TimeZone = 'UTC'")
    yield connection
    connection.close()


def one(con, sql, *args):
    return con.execute(sql, list(args)).fetchone()[0]


def content_hash(path) -> str:
    connection = duckdb.connect(str(path), read_only=True)
    try:
        digest = hashlib.sha256()
        for table in schema.TABLE_NAMES:
            # the sum of the hash of every row: a row cannot be left out or changed without changing it (and no Python datetime is made)
            digest.update(repr(connection.execute(f'SELECT COUNT(*), SUM(CAST(hash(t) AS HUGEINT)) FROM "{table}" t').fetchone()).encode("utf-8"))
        return digest.hexdigest()
    finally:
        connection.close()


# the same every time ---------------------------------------------------------------------------------------------------------

def test_the_same_seed_and_end_date_give_the_same_rows_and_another_seed_does_not(tmp_path):
    options = BuildOptions(seed=3, users=300, end=END)
    first, second = generate(tmp_path / "a.duckdb", options), generate(tmp_path / "b.duckdb", options)
    other = generate(tmp_path / "c.duckdb", BuildOptions(seed=4, users=300, end=END))
    assert first.rows == second.rows and content_hash(first.path) == content_hash(second.path)
    assert content_hash(other.path) != content_hash(first.path)


def test_building_again_replaces_the_file(tmp_path):
    path = tmp_path / "data.duckdb"
    generate(path, BuildOptions(seed=1, users=200, end=END))
    again = generate(path, BuildOptions(seed=1, users=300, end=END))
    assert again.rows["users"] == 300


def test_the_end_date_defaults_to_yesterday_and_bad_options_are_refused(tmp_path):
    assert default_end(date(2026, 10, 24)) == date(2026, 10, 23)
    with pytest.raises(ValueError, match="end date"):
        generate(tmp_path / "x.duckdb", BuildOptions(users=200, end=date(2023, 1, 20)))
    with pytest.raises(ValueError, match="100 users"):
        generate(tmp_path / "x.duckdb", BuildOptions(users=10, end=END))


# the shape: the same tables and columns as the allow-list ------------------------------------------------------------------------

def test_every_column_of_the_allow_list_exists_and_every_column_is_classified(local_db):
    """So the catalog's check passes and nothing is hidden as unclassified."""
    allowlist = yaml.safe_load((REPO_ROOT / "schema_allowlist.yaml").read_text(encoding="utf-8"))
    for table, columns in schema.TABLES.items():
        names = {column.name for column in columns}
        policy = allowlist["tables"][table]
        assert names == set(policy["allowed"]) | set(policy.get("blocked", [])), table


def test_the_local_client_reports_the_columns_and_types_that_bigquery_would(local_db):
    from src.data_access.local_bq import LocalBigQueryClient

    client = LocalBigQueryClient(local_db.path, "bigquery-public-data.thelook_ecommerce")
    users = client.get_table("bigquery-public-data.thelook_ecommerce.users")
    types = {field.name: field.field_type for field in users.schema}
    assert types["id"] == "INTEGER" and types["created_at"] == "TIMESTAMP" and types["latitude"] == "FLOAT"
    assert types["user_geom"] == "GEOGRAPHY" and types["email"] == "STRING"


def test_the_counts_are_what_the_build_said(con, local_db):
    assert {t: one(con, f'SELECT COUNT(*) FROM "{t}"') for t in schema.TABLE_NAMES} == local_db.rows
    assert local_db.rows["users"] == 2500 and local_db.rows["order_items"] > 5000


# integrity ---------------------------------------------------------------------------------------------------------------

def test_every_order_belongs_to_a_user_and_every_item_to_an_order_of_the_same_user(con):
    assert one(con, "SELECT COUNT(*) FROM orders o LEFT JOIN users u ON u.id = o.user_id WHERE u.id IS NULL") == 0
    assert one(con, "SELECT COUNT(*) FROM order_items oi LEFT JOIN orders o ON o.order_id = oi.order_id WHERE o.order_id IS NULL") == 0
    assert one(con, "SELECT COUNT(*) FROM order_items oi JOIN orders o ON o.order_id = oi.order_id WHERE o.user_id <> oi.user_id") == 0


def test_the_number_of_items_of_an_order_is_what_it_says(con):
    assert one(con, "SELECT COUNT(*) FROM orders o JOIN (SELECT order_id, COUNT(*) AS n FROM order_items GROUP BY order_id) s "
                    "ON s.order_id = o.order_id WHERE s.n <> o.num_of_item") == 0
    assert one(con, "SELECT COUNT(*) FROM orders o WHERE NOT EXISTS (SELECT 1 FROM order_items oi WHERE oi.order_id = o.order_id)") == 0


def test_ids_are_unique(con):
    for table, column in (("users", "id"), ("products", "id"), ("orders", "order_id"), ("order_items", "id")):
        assert one(con, f'SELECT COUNT(*) - COUNT(DISTINCT "{column}") FROM "{table}"') == 0


def test_the_statuses_are_the_five_of_the_real_dataset(con):
    expected = {"Processing", "Shipped", "Complete", "Returned", "Cancelled"}
    assert {r[0] for r in con.execute("SELECT DISTINCT status FROM order_items").fetchall()} == expected
    assert {r[0] for r in con.execute("SELECT DISTINCT status FROM orders").fetchall()} <= expected


def test_times_come_in_order_and_nothing_happens_after_the_end_date(con):
    assert one(con, "SELECT COUNT(*) FROM order_items WHERE shipped_at < created_at OR delivered_at < shipped_at OR returned_at < delivered_at") == 0
    assert one(con, "SELECT COUNT(*) FROM order_items WHERE status = 'Returned' AND returned_at IS NULL") == 0
    assert one(con, "SELECT COUNT(*) FROM order_items WHERE status IN ('Processing', 'Cancelled') AND shipped_at IS NOT NULL") == 0
    for table, columns in (("order_items", ("created_at", "shipped_at", "delivered_at", "returned_at")), ("orders", ("created_at", "returned_at")), ("users", ("created_at",))):
        for column in columns:
            assert one(con, f'SELECT COUNT(*) FROM "{table}" WHERE CAST("{column}" AS DATE) > DATE \'{END.isoformat()}\'') == 0, (table, column)
    assert one(con, "SELECT CAST(MIN(created_at) AS DATE) FROM users") >= date(2023, 1, 1)


def test_a_customer_orders_after_signing_up(con):
    assert one(con, "SELECT COUNT(*) FROM orders o JOIN users u ON u.id = o.user_id WHERE CAST(o.created_at AS DATE) < CAST(u.created_at AS DATE)") == 0


def test_the_latest_order_item_is_the_end_date_or_just_before_it(con, local_db):
    latest = one(con, "SELECT CAST(MAX(created_at) AS DATE) FROM order_items")
    assert (END - latest).days <= 3 and latest <= END and local_db.end == END


# the awkward brand data the checks exist for -----------------------------------------------------------------------------------

def test_there_are_twelve_real_brands_and_the_free_text_cases(con):
    brands = {r[0] for r in con.execute("SELECT DISTINCT brand FROM products").fetchall()}
    assert {name for name, *_ in BRANDS} <= brands and len(BRANDS) == 12
    assert "nordic peak" in brands and "Nordic Peak" in brands  # differs in case only: another brand to an exact match (S1)
    assert None in brands and " " in brands  # no brand, and a blank one (A23)
    assert any("'" in b for b in brands if b) and any("&" in b for b in brands if b)  # names that must be escaped


def test_some_items_have_no_product_and_some_products_no_brand(con):
    assert one(con, "SELECT COUNT(*) FROM order_items oi LEFT JOIN products p ON p.id = oi.product_id WHERE p.id IS NULL") == ITEMS_WITHOUT_PRODUCT
    assert one(con, "SELECT COUNT(*) FROM products WHERE brand IS NULL") > 0


def test_the_tiny_brand_is_tiny(con):
    assert one(con, "SELECT COUNT(*) FROM order_items oi JOIN products p ON p.id = oi.product_id WHERE p.brand = 'Wren & Willow'") < 120


# the personal data the guards must keep out -------------------------------------------------------------------------------------

def test_every_blocked_column_holds_something_a_leak_would_show(con):
    row = con.execute("SELECT first_name, last_name, email, street_address, postal_code, latitude, longitude, user_geom FROM users WHERE id = 1").fetchone()
    assert all(value not in (None, "") for value in row)
    assert re.fullmatch(r"POINT\(-?\d+\.\d+ -?\d+\.\d+\)", row[7])


def test_every_email_is_an_example_dot_com_address_and_the_scanner_finds_it(con):
    emails = [r[0] for r in con.execute("SELECT email FROM users").fetchall()]
    assert all(email.endswith("@example.com") for email in emails) and len(set(emails)) == len(emails)
    assert all(pii.scan(email) for email in emails[:50])  # the Output Guard would catch one that leaked


def test_the_names_are_invented_combinations_not_a_list_of_real_people(con):
    assert one(con, "SELECT COUNT(DISTINCT first_name) FROM users") <= 50 and one(con, "SELECT COUNT(DISTINCT last_name) FROM users") <= 40


# the small groups and the causes a question can find ----------------------------------------------------------------------------

def test_some_regions_have_fewer_than_ten_customers_and_ages_up_to_ninety_nine(con):
    assert one(con, "SELECT COUNT(*) FROM (SELECT state FROM users GROUP BY state HAVING COUNT(*) < 10)") >= 3
    assert one(con, "SELECT COUNT(*) FROM users WHERE age >= 95") == OLD_AGE_USERS and one(con, "SELECT MIN(age) FROM users") >= 12
    assert one(con, "SELECT COUNT(*) FROM (SELECT state, age FROM users WHERE age >= 95 GROUP BY state, age HAVING COUNT(*) < 10)") >= 1


def test_the_declining_brand_sold_less_in_the_last_sixty_days(con):
    count = lambda lo, hi: one(  # noqa: E731
        con, "SELECT COUNT(*) FROM order_items oi JOIN products p ON p.id = oi.product_id WHERE p.brand = ? AND CAST(oi.created_at AS DATE) BETWEEN ? AND ?",
        DECLINING_BRAND, lo, hi)
    recent, before = count(date(2026, 8, 24), END), count(date(2026, 6, 25), date(2026, 8, 23))
    assert recent < 0.7 * before, (recent, before)


def test_dresses_of_one_brand_come_back_far_more_often_than_anything_else(con):
    rate = lambda where, *args: one(  # noqa: E731
        con, "SELECT 1.0 * COUNT(*) FILTER (WHERE oi.status = 'Returned') / COUNT(*) FILTER (WHERE oi.status IN ('Complete', 'Returned')) "
             f"FROM order_items oi JOIN products p ON p.id = oi.product_id WHERE {where}", *args)
    special = rate("p.brand = ? AND p.category = ?", RETURNING_BRAND, RETURNING_CATEGORY)
    rest = rate("NOT (p.brand = ? AND p.category = ?)", RETURNING_BRAND, RETURNING_CATEGORY)
    assert special > 2.5 * rest, (special, rest)


def test_texas_customers_buy_cheaper_items_than_california_customers(con):
    price = lambda state: one(con, "SELECT AVG(oi.sale_price) FROM order_items oi JOIN users u ON u.id = oi.user_id WHERE u.state = ?", state)  # noqa: E731
    assert price("Texas") < 0.9 * price("California")


def test_revenue_grows_over_the_years_and_peaks_in_the_holidays(con):
    by_year = dict(con.execute("SELECT EXTRACT(YEAR FROM created_at), SUM(sale_price) FROM order_items WHERE EXTRACT(YEAR FROM created_at) < 2026 GROUP BY 1").fetchall())
    assert by_year[2025] > by_year[2024] > by_year[2023]
    months = dict(con.execute("SELECT EXTRACT(MONTH FROM created_at), SUM(sale_price) FROM order_items WHERE EXTRACT(YEAR FROM created_at) = 2025 GROUP BY 1").fetchall())
    assert max(months, key=months.get) in (11, 12)


def test_the_build_summary_counts_what_it_planted_from_the_tables(local_db):
    assert len(local_db.notes) == 4 and any(DECLINING_BRAND in note for note in local_db.notes)
    assert local_db.brands["Maple & Co"] > local_db.brands["Wren & Willow"] > 0


# the commands ------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def cli_config(project):
    (project / "users.yaml").write_text('users:\n  alice: {name: Alice, role: brand_manager, brands: ["Maple & Co"]}\n  ceo: {name: CEO, role: ceo, all_brands: true}\n', encoding="utf-8")
    (project / "config.yaml").write_text('bigquery:\n  backend: local\n  local_db: "data/sample.duckdb"\n', encoding="utf-8")
    return project / "config.yaml"


def test_build_info_and_seed_reports_work_from_the_command_line(cli_config):
    built = runner.invoke(app, ["local-data", "build", "--config", str(cli_config), "--users", "300", "--end-date", "2026-10-22", "--seed", "5"])
    assert built.exit_code == 0, built.output
    assert "Rows: orders" in built.output and "planted:" in built.output and "2026-10-22 (seed 5)" in built.output
    info = runner.invoke(app, ["local-data", "info", "--config", str(cli_config)])
    assert info.exit_code == 0 and "Built with seed 5 for 300 users" in info.output and "'Maple & Co'" in info.output and "Planted:" in info.output
    seeded = runner.invoke(app, ["local-data", "seed-reports", "--config", str(cli_config)])
    assert seeded.exit_code == 0 and "alice: 4 report(s) added" in seeded.output and "ceo: 14 report(s) added" in seeded.output
    assert "Not in users.yaml, so skipped: bob" in seeded.output
    again = runner.invoke(app, ["local-data", "seed-reports", "--config", str(cli_config)])
    assert "alice: 0 report(s) added (already there)" in again.output  # a second run adds nothing


def test_export_writes_one_parquet_file_per_table_with_the_same_rows_and_types(cli_config, tmp_path):
    import pyarrow.parquet as pq

    runner.invoke(app, ["local-data", "build", "--config", str(cli_config), "--users", "200", "--end-date", "2026-10-22"])
    out = tmp_path / "parquet"
    result = runner.invoke(app, ["local-data", "export", "--to", str(out), "--config", str(cli_config)])
    assert result.exit_code == 0, result.output
    assert sorted(path.name for path in out.iterdir()) == ["order_items.parquet", "orders.parquet", "products.parquet", "users.parquet"]
    users = pq.read_table(out / "users.parquet")
    assert users.num_rows == 200 and str(users.schema.field("created_at").type) == "timestamp[us, tz=UTC]" and str(users.schema.field("id").type) == "int64"
    assert "bq load" in result.output and "200 rows" in result.output


def test_export_without_a_build_says_how_to_build(cli_config, tmp_path):
    result = runner.invoke(app, ["local-data", "export", "--to", str(tmp_path / "x"), "--config", str(cli_config)])
    assert result.exit_code == 2 and "local-data build" in result.output


def test_info_without_a_build_says_how_to_build(cli_config):
    result = runner.invoke(app, ["local-data", "info", "--config", str(cli_config)])
    assert result.exit_code == 2 and "local-data build" in result.output


def test_a_bad_end_date_is_a_startup_problem(cli_config):
    result = runner.invoke(app, ["local-data", "build", "--config", str(cli_config), "--end-date", "yesterday"])
    assert result.exit_code == 2 and "--end-date must be a date" in result.output


def test_building_forgets_the_cached_schema_of_the_old_data(cli_config):
    cache = cli_config.parent / "data" / "cache"
    cache.mkdir(parents=True)
    for name in ("schema.json", "brands.json"):
        (cache / name).write_text("{}", encoding="utf-8")
    runner.invoke(app, ["local-data", "build", "--config", str(cli_config), "--users", "200", "--end-date", "2026-10-22"])
    assert not (cache / "schema.json").exists() and not (cache / "brands.json").exists()


def test_building_never_touches_the_cache_of_a_live_data_folder(project):
    (project / "config.yaml").write_text('bigquery:\n  backend: bigquery\n  local_db: "data/sample.duckdb"\n', encoding="utf-8")
    cache = project / "data" / "cache"
    cache.mkdir(parents=True)
    (cache / "schema.json").write_text("{}", encoding="utf-8")
    runner.invoke(app, ["local-data", "build", "--config", str(project / "config.yaml"), "--users", "200", "--end-date", "2026-10-22"])
    assert (cache / "schema.json").exists()


def test_the_shipped_local_files_are_valid_and_their_brands_are_in_the_generated_data(local_project, local_db):
    from src.access.users import load_user_directory
    from src.config import load_settings
    from src.data_access.brand_check import check_brand_mapping
    from src.data_access.catalog import load_catalog

    shipped = load_settings(REPO_ROOT / "config.local.yaml", load_env_file=False)
    assert shipped.bigquery.backend == "local" and shipped.app.users_file == "users.local.yaml" and shipped.golden.enabled
    assert shipped.app.data_dir == "data/local"  # apart from a live run's database, traces and schema cache
    directory = load_user_directory(REPO_ROOT / "users.local.yaml", salt="test")
    catalog = load_catalog(load_settings(local_project / "config.yaml", load_env_file=False)).catalog
    check_brand_mapping(directory, catalog)  # raises if a brand of users.local.yaml is not in the data
    assert directory.get("ceo").scope_mode == "all" and directory.get("nobrands").scope_mode == "none"
    assert {"alice", "bob", "carol", "dave", "ceo", "nobrands"} == set(directory.names)
