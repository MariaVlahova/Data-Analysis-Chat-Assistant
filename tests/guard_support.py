"""Helpers the SQL Guard tests share (plan 05; plan 06 and plan 09 reuse them).

The guard is built from the repository's own `schema_allowlist.yaml` and default settings, so these tests also fail if
that file stops matching what the rules expect.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.access.users import UserContext
from src.config import GuardsConfig, Settings
from src.errors import AgentError, ErrorKind
from src.guards.allowlist import Allowlist, load_allowlist
from src.guards.query import GuardedQuery
from src.guards.sql_guard import SQLGuard

REPO_ROOT = Path(__file__).resolve().parent.parent

ALICE = UserContext("alice", "Alice", "brand_manager", ("Brand A", "Brand B"), False, "hash-alice")
BOB = UserContext("bob", "Bob", "brand_manager", ("Brand C",), False, "hash-bob")
CEO = UserContext("ceo", "CEO", "ceo", (), True, "hash-ceo")
NOBODY = UserContext("nobrands", "No Brands", "brand_manager", (), False, "hash-nobody")

DATASET = "bigquery-public-data.thelook_ecommerce"
BLOCKED = ("first_name", "last_name", "email", "street_address", "postal_code", "latitude", "longitude", "user_geom")
BRAND_FILTER = "UNNEST(@brands)"  # each scope view holds exactly one of these


def make_settings(**guards) -> Settings:
    return Settings(guards=GuardsConfig(**guards))


def make_allowlist() -> Allowlist:
    return load_allowlist(Settings(), REPO_ROOT / "schema_allowlist.yaml")


def make_guard(**guards) -> SQLGuard:
    return SQLGuard(make_settings(**guards), make_allowlist())


def refused(guard: SQLGuard, sql: str, user: UserContext = ALICE) -> AgentError:
    """The error `check` raises. The test fails if the query was accepted."""
    with pytest.raises(AgentError) as err:
        guard.check(sql, user)
    return err.value


def violation_rule(guard: SQLGuard, sql: str, user: UserContext = ALICE) -> str:
    err = refused(guard, sql, user)
    assert err.kind == ErrorKind.GUARD_VIOLATION, f"{err.kind}: {err.detail}"
    assert err.rule_id, "a guard violation always names its rule"
    return err.rule_id


def accepted(guard: SQLGuard, sql: str, user: UserContext = ALICE) -> GuardedQuery:
    return guard.check(sql, user)


# Typical analytic queries over thelook_ecommerce, written the way a SQL writer would. The offline tests check that the
# guard accepts them and that sqlglot's generated text is stable. The live test (`pytest -m live`) sends the generated
# text to BigQuery as a free dry run, which is the one check that proves BigQuery accepts what sqlglot wrote (for
# example how it spells `INTERVAL 30 DAY`).
ANALYTIC_QUERIES: dict[str, str] = {
    "monthly revenue, last 365 days": (
        "SELECT DATE_TRUNC(DATE(created_at), MONTH) AS month, SUM(sale_price) AS revenue FROM order_items "
        "WHERE created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 365 DAY) GROUP BY month ORDER BY month"),
    "orders per year": "SELECT EXTRACT(YEAR FROM created_at) AS year, COUNT(*) AS orders FROM orders GROUP BY year ORDER BY year",
    "margin by category": (
        "SELECT p.category, SAFE_DIVIDE(SUM(oi.sale_price - p.cost), SUM(oi.sale_price)) AS margin FROM order_items oi "
        "JOIN products p ON p.id = oi.product_id GROUP BY p.category ORDER BY margin DESC"),
    "return rate": "SELECT COUNTIF(status = 'Returned') / COUNT(*) AS return_rate FROM order_items",
    "price quartiles": "SELECT APPROX_QUANTILES(sale_price, 4) AS quartiles FROM order_items",
    "orders in the last 30 days": (
        "SELECT COUNT(*) AS n FROM orders WHERE DATE(created_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)"),
    "month over month": (
        "WITH monthly AS (SELECT DATE_TRUNC(DATE(created_at), MONTH) AS month, SUM(sale_price) AS revenue FROM order_items GROUP BY month) "
        "SELECT month, revenue, LAG(revenue) OVER (ORDER BY month) AS previous, "
        "SAFE_DIVIDE(revenue - LAG(revenue) OVER (ORDER BY month), LAG(revenue) OVER (ORDER BY month)) AS growth FROM monthly ORDER BY month"),
    "top categories per brand": (
        "SELECT p.brand, ARRAY_AGG(DISTINCT p.category LIMIT 3) AS categories FROM products p GROUP BY p.brand"),
    "group by all": "SELECT department, COUNT(DISTINCT id) AS products FROM products GROUP BY ALL",
    "year-month label": "SELECT FORMAT_DATE('%Y-%m', DATE(created_at)) AS ym, COUNT(*) AS orders FROM orders GROUP BY ym ORDER BY ym",
    "kept or returned": "SELECT IF(returned_at IS NULL, 'kept', 'returned') AS outcome, COUNT(*) AS items FROM order_items GROUP BY outcome",
    "delivery time": (
        "SELECT AVG(TIMESTAMP_DIFF(delivered_at, shipped_at, HOUR)) AS avg_hours FROM orders WHERE delivered_at IS NOT NULL"),
    "price bands": (
        "SELECT CASE WHEN sale_price > 100 THEN 'high' WHEN sale_price > 50 THEN 'mid' ELSE 'low' END AS band, COUNT(*) AS items "
        "FROM order_items GROUP BY band"),
    "struct output": "SELECT STRUCT(p.brand AS brand, COUNT(*) AS n) AS s FROM products p GROUP BY p.brand",
    "date spine": "SELECT d FROM UNNEST(GENERATE_DATE_ARRAY(DATE '2024-01-01', DATE '2024-01-07')) AS d",
    "filter on a derived table": "SELECT * FROM (SELECT brand, COUNT(*) AS n FROM products GROUP BY brand) WHERE n > 10 ORDER BY n DESC",
    "customers per state": "SELECT u.state, COUNT(DISTINCT u.id) AS customers FROM users u GROUP BY u.state",
    "revenue by traffic source": (
        "SELECT u.traffic_source, SUM(oi.sale_price) AS revenue FROM order_items oi JOIN users u ON u.id = oi.user_id GROUP BY u.traffic_source"),
    "top customers by spend": "SELECT user_id, SUM(sale_price) AS spend FROM order_items GROUP BY user_id ORDER BY spend DESC LIMIT 10",
    "string functions": "SELECT UPPER(brand) AS b, LENGTH(name) AS n, REGEXP_CONTAINS(name, r'^[A-Z]') AS caps FROM products LIMIT 20",
    "percent of total": "SELECT category, SUM(retail_price) / SUM(SUM(retail_price)) OVER () AS share FROM products GROUP BY category",
    "having": "SELECT brand, COUNT(*) AS n FROM products GROUP BY brand HAVING COUNT(*) > 5 ORDER BY n DESC LIMIT 10",
    "union of two periods": (
        "SELECT 'this year' AS period, COUNT(*) AS n FROM orders WHERE EXTRACT(YEAR FROM created_at) = 2025 "
        "UNION ALL SELECT 'last year', COUNT(*) FROM orders WHERE EXTRACT(YEAR FROM created_at) = 2024"),
}
