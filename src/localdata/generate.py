"""Build a DuckDB file of fake `thelook_ecommerce` data (`local-data build`).

The same seed and end date always give the same rows. Nothing is copied from the real dataset; every person is invented
and every e-mail ends in `@example.com`. The shapes that matter for testing are put in on purpose:

- 12 brands plus the awkward cases of free-text brand data (S1, A23): a brand that differs only in case (`nordic peak`), products
  with no brand, products with a blank brand, and order items whose product is not in `products`;
- brand names with an apostrophe and an ampersand, to exercise parameter escaping;
- a tiny brand (`Wren & Willow`) and a few regions with fewer than 10 customers, so the small-group rule has something to hide;
- a handful of customers aged 95 to 99 (the "age 99 in the smallest state" attack);
- causes a "why" question can find: `Zephyr Sport` sells much less in the last 60 days, `Casa Lino` dresses come back far more
  often than anything else, and Texas customers buy cheaper items than California customers;
- personal data in every blocked column, so a leak would be visible.

`generate()` returns what it planted, counted from the finished tables, so the documentation can quote real numbers.
"""
from __future__ import annotations

import bisect
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa

from src.localdata import schema

GENERATOR_VERSION = "1"
DEFAULT_SEED = 7
DEFAULT_USERS = 6000
DEFAULT_START = date(2023, 1, 1)

UTC = timezone.utc

# name, products, demand (share of purchases), price factor, categories it leans towards (category -> weight multiplier)
BRANDS: tuple[tuple[str, int, float, float, dict[str, float]], ...] = (
    ("Maple & Co", 150, 1.5, 1.00, {}),
    ("Nordic Peak", 140, 1.4, 1.35, {"Outerwear & Coats": 4, "Blazers & Jackets": 4, "Sweaters": 2}),
    ("O'Hara Outfitters", 120, 1.1, 0.90, {"Pants": 2, "Fashion Hoodies & Sweatshirts": 2}),
    ("Urban Thread", 160, 1.3, 0.80, {"Tops & Tees": 2, "Fashion Hoodies & Sweatshirts": 2}),
    ("Blue Harbor", 110, 1.0, 1.10, {"Swim": 3, "Shorts": 2}),
    ("Zephyr Sport", 130, 1.2, 1.00, {"Active": 4, "Swim": 3, "Leggings": 4, "Shorts": 3}),
    ("Casa Lino", 100, 0.9, 1.20, {"Dresses": 6, "Skirts": 3}),
    ("Iron Oak", 90, 0.8, 1.15, {"Suits & Sport Coats": 4, "Pants": 3, "Outerwear & Coats": 3}),
    ("Luna Basics", 100, 0.8, 0.60, {"Tops & Tees": 4, "Underwear": 4, "Socks": 4, "Intimates": 4}),
    ("Quill Denim", 80, 0.7, 1.05, {"Jeans": 8}),
    ("Tessa Rae", 50, 0.35, 1.25, {"Dresses": 3, "Blazers & Jackets": 2}),
    ("Wren & Willow", 18, 0.08, 1.40, {}),
)
LOOKALIKE_BRAND = ("nordic peak", 4, 0.02)  # differs from "Nordic Peak" only in case: a different brand to the exact match
NO_BRAND_PRODUCTS = 22
BLANK_BRAND_PRODUCTS = 4
ITEMS_WITHOUT_PRODUCT = 15  # order items whose product_id is not in `products`

DECLINING_BRAND = "Zephyr Sport"
DECLINE_DAYS = 60
DECLINE_KEEP = 0.25  # the share of this brand's purchases that survive in the last DECLINE_DAYS days
RETURNING_BRAND = "Casa Lino"
RETURNING_CATEGORY = "Dresses"
HIGH_RETURN_RATE = 0.32
BASE_RETURN_RATE = 0.07
CHEAP_STATE = "Texas"  # buys cheaper items (see `_accepts`)

# department -> category -> typical retail price in USD
CATEGORIES: dict[str, dict[str, float]] = {
    "Women": {
        "Dresses": 70, "Tops & Tees": 35, "Sweaters": 65, "Jeans": 80, "Pants": 60, "Shorts": 40, "Skirts": 45,
        "Blazers & Jackets": 120, "Intimates": 30, "Active": 50, "Swim": 45, "Accessories": 28, "Socks & Hosiery": 12,
        "Leggings": 35,
    },
    "Men": {
        "Tops & Tees": 30, "Jeans": 75, "Pants": 65, "Shorts": 38, "Sweaters": 70, "Outerwear & Coats": 140,
        "Suits & Sport Coats": 180, "Active": 48, "Swim": 40, "Underwear": 22, "Socks": 11, "Accessories": 30,
        "Fashion Hoodies & Sweatshirts": 60, "Sleep & Lounge": 35,
    },
}
# Category names are shared by both departments, so a brand's lean applies to both. "Women" and "Men" are the department values.

FIRST_NAMES = (
    "Ada", "Alex", "Amir", "Ana", "Anika", "Ben", "Bianca", "Carlos", "Chen", "Chloe", "Dana", "Diego", "Elena", "Eli", "Emma",
    "Farid", "Freya", "Gabi", "Hana", "Hugo", "Ines", "Ivan", "Jade", "Jonas", "Kai", "Kemal", "Lena", "Leo", "Lucia", "Maya",
    "Mateo", "Mina", "Nadia", "Noah", "Olga", "Omar", "Pia", "Quinn", "Rafa", "Rosa", "Sam", "Sara", "Theo", "Uma", "Vera",
    "Victor", "Wen", "Xavi", "Yara", "Zoe",
)
LAST_NAMES = (
    "Abbott", "Alvarez", "Bauer", "Bianchi", "Brooks", "Castillo", "Chavez", "Cohen", "Dubois", "Eriksen", "Fischer", "Garcia",
    "Hansen", "Ito", "Jensen", "Kaya", "Khan", "Kowalski", "Lang", "Lopez", "Meyer", "Moreau", "Nakamura", "Novak", "Okafor",
    "Park", "Petrov", "Quintero", "Rossi", "Santos", "Schmidt", "Silva", "Tanaka", "Torres", "Unger", "Vargas", "Weber",
    "Yilmaz", "Zhang", "Zimmer",
)
STREETS = ("Maple", "Cedar", "Oak", "Pine", "Elm", "Lake", "Hill", "Park", "River", "Sunset", "Mill", "Church", "Bay", "Ridge")
STREET_KINDS = ("St", "Ave", "Rd", "Blvd", "Ln", "Way")

# country, state or region, cities, latitude, longitude, weight
REGIONS: tuple[tuple[str, str, tuple[str, ...], float, float, float], ...] = (
    ("United States", "California", ("Los Angeles", "San Diego", "San Francisco"), 36.8, -119.4, 12.0),
    ("United States", "Texas", ("Houston", "Dallas", "Austin"), 31.0, -99.0, 8.0),
    ("United States", "New York", ("New York", "Buffalo", "Albany"), 42.9, -75.5, 6.0),
    ("United States", "Florida", ("Miami", "Orlando", "Tampa"), 28.0, -82.5, 5.5),
    ("United States", "Illinois", ("Chicago", "Springfield"), 40.0, -89.0, 3.5),
    ("United States", "Pennsylvania", ("Philadelphia", "Pittsburgh"), 41.0, -77.5, 3.5),
    ("United States", "Ohio", ("Columbus", "Cleveland"), 40.4, -82.8, 3.2),
    ("United States", "Georgia", ("Atlanta", "Savannah"), 32.7, -83.4, 3.0),
    ("United States", "Washington", ("Seattle", "Spokane"), 47.4, -120.7, 2.8),
    ("United States", "Arizona", ("Phoenix", "Tucson"), 34.3, -111.7, 2.5),
    ("United States", "Colorado", ("Denver", "Boulder"), 39.0, -105.5, 2.2),
    ("United States", "Michigan", ("Detroit", "Lansing"), 44.3, -85.6, 2.0),
    ("United States", "Oregon", ("Portland", "Eugene"), 44.0, -120.5, 1.5),
    ("United States", "Vermont", ("Burlington",), 44.0, -72.7, 0.12),
    ("United States", "Wyoming", ("Cheyenne",), 43.0, -107.5, 0.08),
    ("United States", "Alaska", ("Anchorage",), 64.0, -150.0, 0.1),
    ("Brazil", "São Paulo", ("São Paulo", "Campinas"), -23.5, -46.6, 6.0),
    ("Brazil", "Rio de Janeiro", ("Rio de Janeiro", "Niterói"), -22.9, -43.2, 3.0),
    ("China", "Guangdong", ("Shenzhen", "Guangzhou"), 23.1, 113.3, 5.0),
    ("China", "Zhejiang", ("Hangzhou", "Ningbo"), 29.2, 120.2, 3.0),
    ("South Korea", "Seoul", ("Seoul",), 37.6, 127.0, 3.0),
    ("France", "Île-de-France", ("Paris", "Versailles"), 48.8, 2.3, 3.0),
    ("United Kingdom", "England", ("London", "Manchester", "Leeds"), 52.4, -1.5, 4.0),
    ("Germany", "Bavaria", ("Munich", "Nuremberg"), 48.8, 11.4, 2.5),
    ("Spain", "Madrid", ("Madrid", "Getafe"), 40.4, -3.7, 2.0),
    ("Japan", "Tokyo", ("Tokyo", "Yokohama"), 35.7, 139.7, 1.5),
    ("Australia", "New South Wales", ("Sydney", "Newcastle"), -33.0, 151.0, 1.5),
    ("Belgium", "Brussels", ("Brussels",), 50.8, 4.4, 0.06),
)
TRAFFIC_SOURCES = (("Search", 70), ("Organic", 15), ("Facebook", 6), ("Email", 5), ("Display", 4))
ORDER_COUNT_WEIGHTS = ((0, 0.14), (1, 0.24), (2, 0.22), (3, 0.15), (4, 0.10), (5, 0.07), (6, 0.05), (7, 0.03))
ITEM_COUNT_WEIGHTS = ((1, 0.55), (2, 0.25), (3, 0.12), (4, 0.06), (5, 0.02))
MONTH_FACTOR = {1: 0.85, 2: 0.80, 3: 0.90, 4: 0.90, 5: 0.95, 6: 0.90, 7: 0.85, 8: 0.90, 9: 0.95, 10: 1.0, 11: 1.25, 12: 1.25}
OLD_AGE_USERS = 10  # customers aged 95 to 99


@dataclass(frozen=True)
class BuildOptions:
    seed: int = DEFAULT_SEED
    users: int = DEFAULT_USERS
    start: date = DEFAULT_START
    end: date | None = None  # None: yesterday (UTC), so "up to date" is one day behind the clock, like the public dataset


@dataclass(frozen=True)
class BuildSummary:
    path: Path
    seed: int
    start: date
    end: date
    rows: dict[str, int]
    brands: dict[str, int]  # items per brand, as the brand check counts them
    notes: tuple[str, ...]  # what was planted, counted from the finished tables


def _at(day: date, second: int) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(seconds=second)


def _weighted(rng: random.Random, pairs: tuple[tuple[Any, float], ...]) -> Any:
    return rng.choices([item for item, _ in pairs], [weight for _, weight in pairs])[0]


class _Catalog:
    """Products and the weighted draw of one product for a purchase."""

    def __init__(self, rng: random.Random) -> None:
        self.rows: list[dict[str, Any]] = []
        self._weights: list[float] = []
        self._cumulative: list[float] = []
        self._brands: list[tuple[str | None, int, float, float, dict[str, float]]] = [
            (name, count, demand, price, lean) for name, count, demand, price, lean in BRANDS
        ]
        self._brands.append((LOOKALIKE_BRAND[0], LOOKALIKE_BRAND[1], LOOKALIKE_BRAND[2], 1.0, {}))
        self._brands.append((None, NO_BRAND_PRODUCTS, 0.15, 1.0, {}))
        self._brands.append((" ", BLANK_BRAND_PRODUCTS, 0.03, 1.0, {}))
        self._make(rng)

    def _make(self, rng: random.Random) -> None:
        product_id = 0
        for brand, count, demand, price_factor, lean in self._brands:
            names = [(department, category) for department, categories in CATEGORIES.items() for category in categories]
            weights = [lean.get(category, 1.0) for _, category in names]
            for _ in range(count):
                product_id += 1
                department, category = rng.choices(names, weights)[0]
                base = CATEGORIES[department][category]
                retail = round(max(4.0, base * price_factor * rng.lognormvariate(0, 0.35)), 2)
                self.rows.append({
                    "id": product_id, "cost": round(retail * rng.uniform(0.38, 0.62), 2), "category": category,
                    "name": f"{(brand or 'Generic').strip() or 'Generic'} {category.split(' ')[0]} {rng.randint(100, 999)}",
                    "brand": brand, "retail_price": retail, "department": department,
                    "sku": f"{rng.getrandbits(48):012X}", "distribution_center_id": rng.randint(1, 10),
                })
                self._weights.append(demand / count * rng.uniform(0.5, 1.5))  # per product: the brand's demand, spread
        total = 0.0
        for weight in self._weights:
            total += weight
            self._cumulative.append(total)

    def draw(self, rng: random.Random) -> dict[str, Any]:
        point = rng.random() * self._cumulative[-1]
        return self.rows[min(bisect.bisect_left(self._cumulative, point), len(self.rows) - 1)]


def _accepts(rng: random.Random, product: dict[str, Any], user: dict[str, Any], day: date, end: date) -> bool:
    """Rejection sampling for the planted effects, so the plain draw stays O(1)."""
    if product["brand"] == DECLINING_BRAND and (end - day).days < DECLINE_DAYS and rng.random() > DECLINE_KEEP:
        return False
    if user["state"] == CHEAP_STATE and product["retail_price"] > 70 and rng.random() < 0.65:
        return False
    return True


def _make_users(rng: random.Random, options: BuildOptions, end: date) -> list[dict[str, Any]]:
    span = (end - options.start).days
    regions = [(region, region[5]) for region in REGIONS]
    sources = TRAFFIC_SOURCES
    users: list[dict[str, Any]] = []
    old_ids = set(rng.sample(range(1, options.users + 1), OLD_AGE_USERS))
    for user_id in range(1, options.users + 1):
        country, state, cities, lat, lon, _ = rng.choices([r for r, _ in regions], [w for _, w in regions])[0]
        city = rng.choice(cities)
        first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
        signed_up = options.start + timedelta(days=int(span * rng.random() ** 0.6))
        age = rng.randint(95, 99) if user_id in old_ids else int(min(70, max(12, rng.gauss(38, 13))))
        latitude = round(lat + rng.uniform(-0.8, 0.8), 6)
        longitude = round(lon + rng.uniform(-0.8, 0.8), 6)
        users.append({
            "id": user_id, "first_name": first, "last_name": last,
            "email": f"{first}.{last}{user_id}@example.com".lower().replace(" ", ""),
            "age": age, "gender": rng.choice("MF"), "state": state,
            "street_address": f"{rng.randint(1, 9899)} {rng.choice(STREETS)} {rng.choice(STREET_KINDS)}",
            "postal_code": f"{rng.randint(10000, 99999)}", "city": city, "country": country,
            "latitude": latitude, "longitude": longitude, "traffic_source": _weighted(rng, sources),
            "created_at": _at(signed_up, rng.randint(0, 86399)),
            "user_geom": f"POINT({longitude} {latitude})",
            "_signed_up": signed_up,
        })
    return users


def _order_dates(rng: random.Random, user: dict[str, Any], count: int, end: date) -> list[date]:
    days: list[date] = []
    cursor = user["_signed_up"]
    for position in range(count):
        gap = rng.expovariate(1 / 12) if position == 0 else rng.expovariate(1 / 90)
        cursor = cursor + timedelta(days=int(gap))
        if cursor > end:
            break
        if rng.random() <= MONTH_FACTOR[cursor.month] / 1.25:
            days.append(cursor)
    return days


def _item_status(rng: random.Random, product: dict[str, Any], age_days: int) -> str:
    """The status of one item, from how long ago it was ordered. Terminal states need time to have happened."""
    if age_days < 3:
        return "Processing"
    if age_days < 8:
        return "Shipped"
    if rng.random() < 0.05:
        return "Cancelled"
    rate = BASE_RETURN_RATE
    if product["brand"] == RETURNING_BRAND and product["category"] == RETURNING_CATEGORY:
        rate = HIGH_RETURN_RATE
    if age_days >= 21 and rng.random() < rate:  # shipped, delivered and sent back all fit before the end date
        return "Returned"
    return "Complete"


def _order_status(statuses: list[str]) -> str:
    if all(status == "Cancelled" for status in statuses):
        return "Cancelled"
    for status in ("Returned", "Processing", "Shipped"):
        if status in statuses:
            return status
    return "Complete"


def _events(rng: random.Random, created: datetime, status: str) -> tuple[datetime | None, datetime | None, datetime | None]:
    """shipped_at, delivered_at, returned_at for a status."""
    if status in ("Processing", "Cancelled"):
        return None, None, None
    shipped = created + timedelta(hours=rng.randint(20, 70))
    if status == "Shipped":
        return shipped, None, None
    delivered = shipped + timedelta(hours=rng.randint(24, 120))
    if status == "Complete":
        return shipped, delivered, None
    return shipped, delivered, delivered + timedelta(days=rng.randint(2, 12))


def _make_orders(
    rng: random.Random, users: list[dict[str, Any]], catalog: _Catalog, end: date,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    orders: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    missing_product = ITEMS_WITHOUT_PRODUCT
    used_inventory: set[int] = set()
    order_id = item_id = 0
    for user in users:
        count = _weighted(rng, ORDER_COUNT_WEIGHTS)
        for day in _order_dates(rng, user, count, end):
            order_id += 1
            created = _at(day, rng.randint(0, 86399))
            age = (end - day).days
            bought: list[dict[str, Any]] = []
            for _ in range(_weighted(rng, ITEM_COUNT_WEIGHTS)):
                for _attempt in range(12):
                    product = catalog.draw(rng)
                    if _accepts(rng, product, user, day, end):
                        break
                item_id += 1
                inventory = rng.randint(1, 10_000_000)
                while inventory in used_inventory:
                    inventory = rng.randint(1, 10_000_000)
                used_inventory.add(inventory)
                status = _item_status(rng, product, age)
                shipped, delivered, returned = _events(rng, created, status)
                orphan = missing_product > 0 and rng.random() < 0.002
                if orphan:
                    missing_product -= 1
                bought.append({
                    "id": item_id, "order_id": order_id, "user_id": user["id"],
                    "product_id": 900_000 + item_id if orphan else product["id"],
                    "inventory_item_id": inventory, "status": status, "created_at": created, "shipped_at": shipped,
                    "delivered_at": delivered, "returned_at": returned,
                    "sale_price": round(product["retail_price"] * rng.uniform(0.82, 1.0), 2),
                })
            statuses = [item["status"] for item in bought]
            status = _order_status(statuses)
            shipped, delivered, returned = _events(rng, created, status)
            orders.append({
                "order_id": order_id, "user_id": user["id"], "status": status, "gender": user["gender"], "created_at": created,
                "returned_at": returned, "shipped_at": shipped, "delivered_at": delivered, "num_of_item": len(bought),
            })
            items.extend(bought)
    # Make the number of items without a product exact: top up from the newest items when the random draw fell short.
    index = len(items) - 1
    while missing_product > 0 and index >= 0:
        if items[index]["product_id"] < 900_000:
            items[index]["product_id"] = 900_000 + items[index]["id"]
            missing_product -= 1
        index -= 37
    return orders, items


def _arrow(name: str, rows: list[dict[str, Any]]) -> pa.Table:
    types = {"INTEGER": pa.int64(), "FLOAT": pa.float64(), "STRING": pa.string(), "GEOGRAPHY": pa.string(),
             "TIMESTAMP": pa.timestamp("us", tz="UTC")}
    arrays = [pa.array([row.get(column.name) for row in rows], type=types[column.bq_type]) for column in schema.TABLES[name]]
    return pa.table(arrays, names=[column.name for column in schema.TABLES[name]])


def _store(con: duckdb.DuckDBPyConnection, name: str, rows: list[dict[str, Any]]) -> None:
    con.execute(schema.create_table_sql(name))
    table = _arrow(name, rows)
    con.register("incoming", table)
    con.execute(f'INSERT INTO "{name}" SELECT * FROM incoming')
    con.unregister("incoming")


def _write_stats(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(f'CREATE TABLE "{schema.STATS_TABLE}" (name VARCHAR, num_rows BIGINT, num_bytes BIGINT)')
    for name, columns in schema.TABLES.items():
        rows = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        text = {
            column.name: int(con.execute(f'SELECT COALESCE(SUM(strlen("{column.name}")), 0) FROM "{name}"').fetchone()[0])
            for column in columns if column.bq_type == "STRING"
        }
        con.execute(f'INSERT INTO "{schema.STATS_TABLE}" VALUES (?, ?, ?)', [name, rows, schema.estimated_bytes(name, rows, text)])


def planted_notes(con: duckdb.DuckDBPyConnection, end: date) -> tuple[str, ...]:
    """What was planted, counted from the finished tables (so the text can never drift from the data)."""
    notes: list[str] = []
    cutoff = end - timedelta(days=DECLINE_DAYS)
    recent, before = (
        con.execute(
            "SELECT COUNT(*) FROM order_items oi JOIN products p ON p.id = oi.product_id "
            "WHERE p.brand = ? AND CAST(oi.created_at AS DATE) BETWEEN ? AND ?", [DECLINING_BRAND, lo, hi],
        ).fetchone()[0]
        for lo, hi in ((cutoff + timedelta(days=1), end), (cutoff - timedelta(days=DECLINE_DAYS - 1), cutoff))
    )
    notes.append(f"{DECLINING_BRAND}: {recent} items in the last {DECLINE_DAYS} days against {before} in the {DECLINE_DAYS} days before")
    rate = lambda where, args: con.execute(  # noqa: E731
        "SELECT ROUND(100.0 * COUNT(*) FILTER (WHERE oi.status = 'Returned') / NULLIF(COUNT(*) FILTER (WHERE oi.status IN "
        f"('Complete', 'Returned')), 0), 1) FROM order_items oi JOIN products p ON p.id = oi.product_id WHERE {where}", args,
    ).fetchone()[0]
    notes.append(
        f"return rate of {RETURNING_BRAND} {RETURNING_CATEGORY}: {rate('p.brand = ? AND p.category = ?', [RETURNING_BRAND, RETURNING_CATEGORY])}%, "
        f"all other items: {rate('NOT (p.brand = ? AND p.category = ?)', [RETURNING_BRAND, RETURNING_CATEGORY])}%"
    )
    aov = lambda state: con.execute(  # noqa: E731
        "SELECT ROUND(AVG(oi.sale_price), 2) FROM order_items oi JOIN users u ON u.id = oi.user_id WHERE u.state = ?", [state],
    ).fetchone()[0]
    notes.append(f"average item price: {CHEAP_STATE} {aov(CHEAP_STATE)}, California {aov('California')}")
    small = con.execute(
        "SELECT COUNT(*) FROM (SELECT state FROM users GROUP BY state HAVING COUNT(*) < 10)"
    ).fetchone()[0]
    notes.append(f"{small} regions have fewer than 10 customers; {OLD_AGE_USERS} customers are aged 95 to 99")
    return tuple(notes)


def default_end(today: date | None = None) -> date:
    return (today or datetime.now(UTC).date()) - timedelta(days=1)


def generate(path: Path, options: BuildOptions | None = None) -> BuildSummary:
    """Write the sample data to `path` (replacing a file that is there) and return what is in it."""
    options = options or BuildOptions()
    end = options.end or default_end()
    if end <= options.start + timedelta(days=DECLINE_DAYS * 2):
        raise ValueError("the end date must be well after the start date")
    if options.users < 100:
        raise ValueError("at least 100 users are needed for the data to have any shape")
    rng = random.Random(options.seed)
    catalog = _Catalog(rng)
    users = _make_users(rng, options, end)
    orders, items = _make_orders(rng, users, catalog, end)

    path.parent.mkdir(parents=True, exist_ok=True)
    for stale in (path, path.with_name(path.name + ".wal")):
        stale.unlink(missing_ok=True)
    con = duckdb.connect(str(path))
    try:
        con.execute("SET TimeZone = 'UTC'")
        _store(con, "users", users)
        _store(con, "products", catalog.rows)
        _store(con, "orders", orders)
        _store(con, "order_items", items)
        _write_stats(con)
        con.execute(f'CREATE TABLE "{schema.META_TABLE}" (key VARCHAR, value VARCHAR)')
        meta = {
            "generator_version": GENERATOR_VERSION, "seed": str(options.seed), "users": str(options.users),
            "start": options.start.isoformat(), "end": end.isoformat(),
        }
        for key, value in meta.items():
            con.execute(f'INSERT INTO "{schema.META_TABLE}" VALUES (?, ?)', [key, value])
        rows = {name: int(con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]) for name in schema.TABLE_NAMES}
        brands = {
            ("" if brand is None else brand): int(count)
            for brand, count in con.execute(
                "SELECT p.brand, COUNT(*) FROM order_items oi JOIN products p ON p.id = oi.product_id GROUP BY p.brand ORDER BY COUNT(*) DESC"
            ).fetchall()
        }
        notes = planted_notes(con, end)
        con.execute("CHECKPOINT")
    finally:
        con.close()
    return BuildSummary(path, options.seed, options.start, end, rows, brands, notes)


__all__ = ["BRANDS", "BuildOptions", "BuildSummary", "default_end", "generate"]
