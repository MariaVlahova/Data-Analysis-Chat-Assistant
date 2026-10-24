"""What is in a built sample-data file (`local-data info`): the counts, the dates, the brands and the planted effects.

The planted-effect lines are counted from the tables again each time, so they are always true of the file in front of you.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb

from src.errors import ConfigError
from src.localdata import schema
from src.localdata.generate import planted_notes


@dataclass(frozen=True)
class DataInfo:
    path: Path
    meta: dict[str, str]
    rows: dict[str, int]
    bytes: dict[str, int]
    latest_item: str
    brands: list[tuple[str, int, int]]  # name, products, order items
    notes: tuple[str, ...]


def read_info(path: Path) -> DataInfo:
    if not path.is_file():
        raise ConfigError(f"The local sample data was not found: {path}", fix="Run: python -m src.cli.main local-data build")
    try:
        con = duckdb.connect(str(path), read_only=True)
    except duckdb.Error:
        raise ConfigError(f"The local sample data could not be read: {path}", fix="Run: python -m src.cli.main local-data build") from None
    try:
        con.execute("SET TimeZone = 'UTC'")  # dates are UTC days, whatever the machine's time zone is
        meta = dict(con.execute(f'SELECT key, value FROM "{schema.META_TABLE}"').fetchall())
        stats = {name: (rows, size) for name, rows, size in con.execute(f'SELECT name, num_rows, num_bytes FROM "{schema.STATS_TABLE}"').fetchall()}
        latest = con.execute("SELECT CAST(MAX(created_at) AS DATE) FROM order_items").fetchone()[0]
        brands = con.execute(
            "SELECT COALESCE(p.brand, '(no brand)') AS brand, COUNT(DISTINCT p.id), COUNT(oi.id) FROM products p "
            "LEFT JOIN order_items oi ON oi.product_id = p.id GROUP BY p.brand ORDER BY COUNT(oi.id) DESC"
        ).fetchall()
        notes = planted_notes(con, date.fromisoformat(meta["end"]))
    finally:
        con.close()
    return DataInfo(
        path, meta, {name: int(rows) for name, (rows, _) in stats.items()}, {name: int(size) for name, (_, size) in stats.items()},
        str(latest), [(str(name), int(products), int(items)) for name, products, items in brands], notes,
    )
