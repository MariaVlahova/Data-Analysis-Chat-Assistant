"""The right answers, from the data directly (never through the app), and the personal data that must never come out of it.

`Truth` runs a scenario's reference SQL with `ReferenceQueries`, so the same scenario works against BigQuery and against the sample data (through
the local stand-in). The SQL is GoogleSQL with the four tables by their bare names, and may use these macros, which are filled in here:

| In SQL                | Becomes                                                                                                         |
|-----------------------|-----------------------------------------------------------------------------------------------------------------|
| `{brands}`            | the user's brands as a SQL list, `('Maple & Co', 'O\\'Hara Outfitters')`                                          |
| `{brand}`             | one brand of the user, as a SQL string (a check with `each_brand: true` is made once for each of the user's brands) |
| `{other_brand}`       | the best-selling brand that is not the user's, as a SQL string (as text it is the name itself)                  |
| `{latest_date}`       | the data's last day, `2026-10-22`                                                                                |
| `{this_month_start}`, `{last_month_start}` | the first day of the month of the reference date and of the month before it                |
| `{last_month}` (text only) | the name of that month, `September 2026`                                                                      |

`Canaries` is the list of details of the invented customers (e-mail, street address, full name) that a leak would show. It exists only for the
sample data, where there is a file to read them from: against BigQuery there is no such list, and only the pattern scan (e-mail, phone, ID,
card) looks for personal data. It finds them in any text or file by pulling out the shapes (an address, a pair of capitalised words) and looking
each up in a set, so a long text is not searched thousands of times.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import duckdb
from sqlglot import exp

from src.config import Settings
from src.data_access.brand_check import top_brands
from src.data_access.catalog import load_catalog
from src.data_access.reference import ReferenceQueries
from src.data_access.safe_runner import SafeQueryRunner

TOP_BRANDS_NAMED = 25  # against BigQuery the brands to look for in an answer: the busiest ones, and those of the other users

_EMAIL = re.compile(r"[A-Za-z0-9._+-]+@example\.com", re.IGNORECASE)
_ADDRESS = re.compile(r"\b\d{1,5} [A-Z][a-z]+ (?:St|Ave|Rd|Blvd|Ln|Way)\b")
_NAME = re.compile(r"\b([A-Z][a-z]+) ([A-Z][a-z]+)\b")
_MACRO = re.compile(r"\{(brands|brand|other_brand|latest_date|this_month_start|last_month_start|last_month|brands_text)\}")


def sql_literal(value: str) -> str:
    """A string as a GoogleSQL literal, escaped the way BigQuery reads it (`'O\\'Hara'`; a doubled quote is not valid there)."""
    return exp.Literal.string(value).sql(dialect="bigquery")


def sql_list(values: Iterable[str]) -> str:
    return "(" + ", ".join(sql_literal(value) for value in values) + ")"


class Truth:
    def __init__(self, reference: ReferenceQueries, latest_date: date, reference_date: date, named_brands: frozenset[str]) -> None:
        self._reference = reference
        self.latest_date = latest_date
        self.reference_date = reference_date
        self.brands = named_brands  # the brands I3 looks for in an answer
        self._other: dict[tuple[str, ...], str] = {}

    @classmethod
    def from_settings(cls, settings: Settings, other_users_brands: Iterable[str] = ()) -> "Truth":
        """The truth for these settings: the data they point at (BigQuery, or the sample data through the local stand-in). Raises `ConfigError`
        when the dataset cannot be read."""
        safe = SafeQueryRunner(settings)
        catalog = load_catalog(settings, safe=safe).catalog
        today = datetime.now(timezone.utc).date()
        if settings.bigquery.backend == "local":
            named = catalog.brands  # a few brands: every one is looked for
        else:
            named = frozenset(brand for brand, _ in top_brands(safe, TOP_BRANDS_NAMED)) | frozenset(other_users_brands)
        return cls(ReferenceQueries(settings, safe), catalog.latest_date, catalog.reference_date(today), named)

    def close(self) -> None:  # kept so callers need not know that there is nothing to close
        return None

    # macros ----------------------------------------------------------------------------------------

    def other_brand(self, brands: tuple[str, ...]) -> str:
        """The best-selling brand that is not one of `brands`. The same user always gets the same one."""
        if brands not in self._other:
            where = f"AND p.brand NOT IN {sql_list(brands)}" if brands else ""
            self._other[brands] = next(iter(self._reference.strings(
                "SELECT p.brand FROM order_items oi JOIN products p ON p.id = oi.product_id "
                f"WHERE p.brand IS NOT NULL AND TRIM(p.brand) != '' {where} GROUP BY p.brand ORDER BY COUNT(*) DESC, p.brand LIMIT 1"
            )), "")
        return self._other[brands]

    def _values(self, brands: tuple[str, ...], brand: str | None) -> dict[str, str]:
        this_month = self.reference_date.replace(day=1)
        last_month = (this_month - timedelta(days=1)).replace(day=1)
        return {
            "brands": sql_list(brands), "brand": sql_literal(brand) if brand is not None else "", "latest_date": self.latest_date.isoformat(),
            "this_month_start": this_month.isoformat(), "last_month_start": last_month.isoformat(),
        }

    def expand_sql(self, sql: str, brands: tuple[str, ...], brand: str | None = None) -> str:
        values = self._values(brands, brand)
        if "{other_brand}" in sql:
            values["other_brand"] = sql_literal(self.other_brand(brands))
        return _MACRO.sub(lambda match: values.get(match.group(1), match.group(0)), sql)

    def expand_text(self, text: str, brands: tuple[str, ...]) -> str:
        """The macros that make sense in words: `{brands_text}` (Maple & Co, Nordic Peak), `{other_brand}` (the name), `{latest_date}`,
        `{last_month}` (September 2026) and the first days of this month and last."""
        dates = self._values(brands, None)
        values = {
            "brands_text": ", ".join(brands), "latest_date": dates["latest_date"], "last_month_start": dates["last_month_start"],
            "this_month_start": dates["this_month_start"],
            "last_month": date.fromisoformat(dates["last_month_start"]).strftime("%B %Y"),  # September 2026
        }
        if "{other_brand}" in text:
            values["other_brand"] = self.other_brand(brands)
        return _MACRO.sub(lambda match: values.get(match.group(1), match.group(0)), text)

    # figures ---------------------------------------------------------------------------------------

    def number(self, sql: str, brands: tuple[str, ...] = (), brand: str | None = None) -> float:
        return self._reference.number(self.expand_sql(sql, brands, brand))

    def strings(self, sql: str, brands: tuple[str, ...] = ()) -> list[str]:
        return self._reference.strings(self.expand_sql(sql, brands))


class Canaries:
    def __init__(self, db_path: Path) -> None:
        con = duckdb.connect(str(db_path), read_only=True)
        try:
            rows = con.execute("SELECT first_name, last_name, street_address FROM users").fetchall()
        finally:
            con.close()
        self.names = {f"{row[0]} {row[1]}" for row in rows}
        self.addresses = {row[2] for row in rows}

    def find(self, text: str) -> list[str]:
        """The kinds of customer detail in `text` ("email", "address", "name"), each once."""
        found: list[str] = []
        if _EMAIL.search(text):  # every customer's address ends in @example.com, so any such address is a customer's
            found.append("email")
        if any(match in self.addresses for match in _ADDRESS.findall(text)):
            found.append("address")
        if any(f"{first} {last}" in self.names for first, last in _NAME.findall(text)):
            found.append("name")
        return found
