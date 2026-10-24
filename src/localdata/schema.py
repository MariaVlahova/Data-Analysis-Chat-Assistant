"""The four tables of `thelook_ecommerce` as the generator writes them and as the local client reports them.

The column list is the one in `schema_allowlist.yaml` plus the blocked columns (names, e-mail, address, coordinates), because
the point of the data is to hold personal data that the guards must keep out of every answer. `bq_type` is the REST API's
legacy type name, which is what the BigQuery client library reports (`INTEGER`, `FLOAT`); the catalog turns it into GoogleSQL's.

Like the allow-list, this is our expected shape of the live dataset, not a copy of it: `doctor --refresh` against the real
dataset is what shows the real columns (Q4).
"""
from __future__ import annotations

from dataclasses import dataclass

META_TABLE = "__meta"  # key/value facts about how the file was built
STATS_TABLE = "__table_stats"  # rows and estimated bytes per table (what a dry run adds up)


@dataclass(frozen=True)
class Column:
    name: str
    bq_type: str  # INTEGER, FLOAT, STRING, TIMESTAMP, GEOGRAPHY
    duck_type: str


def _c(name: str, bq: str) -> Column:
    duck = {"INTEGER": "BIGINT", "FLOAT": "DOUBLE", "STRING": "VARCHAR", "TIMESTAMP": "TIMESTAMPTZ", "GEOGRAPHY": "VARCHAR"}[bq]
    return Column(name, bq, duck)


TABLES: dict[str, tuple[Column, ...]] = {
    "orders": (
        _c("order_id", "INTEGER"), _c("user_id", "INTEGER"), _c("status", "STRING"), _c("gender", "STRING"),
        _c("created_at", "TIMESTAMP"), _c("returned_at", "TIMESTAMP"), _c("shipped_at", "TIMESTAMP"),
        _c("delivered_at", "TIMESTAMP"), _c("num_of_item", "INTEGER"),
    ),
    "order_items": (
        _c("id", "INTEGER"), _c("order_id", "INTEGER"), _c("user_id", "INTEGER"), _c("product_id", "INTEGER"),
        _c("inventory_item_id", "INTEGER"), _c("status", "STRING"), _c("created_at", "TIMESTAMP"),
        _c("shipped_at", "TIMESTAMP"), _c("delivered_at", "TIMESTAMP"), _c("returned_at", "TIMESTAMP"),
        _c("sale_price", "FLOAT"),
    ),
    "products": (
        _c("id", "INTEGER"), _c("cost", "FLOAT"), _c("category", "STRING"), _c("name", "STRING"), _c("brand", "STRING"),
        _c("retail_price", "FLOAT"), _c("department", "STRING"), _c("sku", "STRING"), _c("distribution_center_id", "INTEGER"),
    ),
    "users": (
        _c("id", "INTEGER"), _c("first_name", "STRING"), _c("last_name", "STRING"), _c("email", "STRING"), _c("age", "INTEGER"),
        _c("gender", "STRING"), _c("state", "STRING"), _c("street_address", "STRING"), _c("postal_code", "STRING"),
        _c("city", "STRING"), _c("country", "STRING"), _c("latitude", "FLOAT"), _c("longitude", "FLOAT"),
        _c("traffic_source", "STRING"), _c("created_at", "TIMESTAMP"), _c("user_geom", "GEOGRAPHY"),
    ),
}

TABLE_NAMES = tuple(TABLES)


def create_table_sql(name: str) -> str:
    columns = ", ".join(f'"{column.name}" {column.duck_type}' for column in TABLES[name])
    return f'CREATE TABLE "{name}" ({columns})'


def estimated_bytes(name: str, rows: int, text_bytes: dict[str, int]) -> int:
    """What BigQuery would count for the table: 8 bytes for a number or a timestamp, the text length plus 2 for a string, 16 for
    a geography. `text_bytes` is the total length of each text column. Close enough for the cost gate of a local dry run."""
    total = 0
    for column in TABLES[name]:
        if column.bq_type in ("INTEGER", "FLOAT", "TIMESTAMP"):
            total += 8 * rows
        elif column.bq_type == "STRING":
            total += text_bytes.get(column.name, 0) + 2 * rows
        else:
            total += 16 * rows
    return total
