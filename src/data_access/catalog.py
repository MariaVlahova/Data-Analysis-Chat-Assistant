"""Schema catalog: the live columns, the real brand list, the latest order date and the table sizes (plan 03).

It answers questions about the data structure (F5), anchors "up-to-date" to the dataset's latest date (A5), and
holds the brand list the startup check compares `users.yaml` with (S1). Metadata calls are free. Two small
queries (latest date, brand list) go through `SafeQueryRunner` like every other query.

Cached under `data/cache/` (`schema.json`, `brands.json`) and refreshed when older than 24 hours or on
`doctor --refresh`. If BigQuery cannot be reached, a saved copy is used and the caller is told. With no saved
copy there is nothing to check against, so loading fails closed (NFR15).

Which columns the agent may see comes from the allow-list (plan 05, `schema_allowlist.yaml`). Until a policy is
attached, `overview()` and `prune()` refuse to list anything: a column that is not allow-listed does not exist.
"""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol, Sequence, TypeVar

import pandas as pd

from src.config import Settings
from src.data_access.safe_runner import SafeQueryRunner, SystemQuery
from src.errors import AgentError, ConfigError, ErrorKind, crash_summary
from src.fileutil import atomic_write_text

log = logging.getLogger(__name__)

TABLES = ("orders", "order_items", "products", "users")
CACHE_DIR_NAME = "cache"
SCHEMA_FILE = "schema.json"
BRANDS_FILE = "brands.json"
CACHE_VERSION = 1
CACHE_MAX_AGE = timedelta(hours=24)  # a copy younger than this is used without calling BigQuery
CACHE_FALLBACK_MAX_AGE = timedelta(days=7)  # BigQuery down: the oldest copy still trusted (impl)
BRAND_ROW_LIMIT = 100_000  # the full brand list is far above the 1000-row cap meant for answers
STALE_DATA_DAYS = 7  # A5: past this, the answer says the data ends earlier than today
CLOCK_SKEW = timedelta(minutes=5)  # a cache dated further ahead than this is not trusted

# The columns the startup and `doctor` SQL reads. They are checked against the live schema before any query
# runs, so a rename shows up as "no column X" and not as a failed query that looks like an outage.
REQUIRED_COLUMNS = {"order_items": ("created_at", "product_id"), "products": ("id", "brand")}

# Column types that can hold free text (A21). Nested STRUCT fields are not inspected.
FREE_TEXT_TYPES = frozenset({"STRING", "ARRAY<STRING>", "JSON"})

# The client library reports the REST API's legacy type names. The prompts and plan 05's allow-list use GoogleSQL
# names, because `CAST(x AS FLOAT)` is not valid GoogleSQL.
_GOOGLESQL_TYPES = {"INTEGER": "INT64", "FLOAT": "FLOAT64", "BOOLEAN": "BOOL", "RECORD": "STRUCT"}

T = TypeVar("T")


class ColumnPolicy(Protocol):
    """What the catalog needs from plan 05's allow-list. An unknown table has no allowed or blocked columns."""

    def table_names(self) -> Iterable[str]: ...

    def allowed(self, table: str) -> Sequence[str]: ...

    def blocked(self, table: str) -> Sequence[str]: ...

    def describe(self, table: str, column: str) -> str: ...


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    type: str
    mode: str = "NULLABLE"
    description: str = ""


@dataclass(frozen=True, eq=False)  # compared and hashed by identity: the dict fields make a field-wise hash impossible
class SchemaCatalog:
    tables: dict[str, list[ColumnInfo]]  # live schema
    brands: frozenset[str]
    latest_date: date
    table_bytes: dict[str, int]
    checked_at: datetime  # when the schema was last read from BigQuery
    brands_checked_at: datetime
    policy: ColumnPolicy | None = None

    # allow-list ----------------------------------------------------------------------------------
    def with_policy(self, policy: ColumnPolicy) -> "SchemaCatalog":
        """Attach the allow-list after checking it against the live schema. Raises `ConfigError`."""
        self.reconcile(policy)
        return replace(self, policy=policy)

    def reconcile(self, policy: ColumnPolicy) -> list[str]:
        """An allow-listed column or table that is missing from the live schema stops startup: the file is wrong.

        A live column in neither the allowed nor the blocked list is logged and treated as blocked (fail
        closed). Returns those `table.column` names so `doctor` can show them.
        """
        missing: list[str] = []
        for table in policy.table_names():
            if table not in self.tables:
                missing.append(f"table {table}")
                continue
            live = {column.name for column in self.tables[table]}
            missing.extend(f"{table}.{column}" for column in policy.allowed(table) if column not in live)
        if missing:
            raise ConfigError(
                "The allow-list names tables or columns that are not in BigQuery: " + ", ".join(missing) + ".",
                fix="Correct schema_allowlist.yaml. If the dataset just changed, run: python -m src.cli.main doctor --refresh",
            )
        unclassified = []
        for table, columns in self.tables.items():
            known = set(policy.allowed(table)) | set(policy.blocked(table))
            unclassified.extend(f"{table}.{column.name}" for column in columns if column.name not in known)
        if unclassified:
            log.warning("unclassified, treated as blocked: %s", ", ".join(unclassified))
        return unclassified

    def _policy(self) -> ColumnPolicy:
        if self.policy is None:
            raise RuntimeError("the schema allow-list is not attached, so no column can be shown to the agent")
        return self.policy

    def visible_columns(self, table: str) -> list[ColumnInfo]:
        """Allow-listed columns that exist live, in live order."""
        allowed = set(self._policy().allowed(table))
        return [column for column in self.tables.get(table, []) if column.name in allowed]

    # text for prompts ----------------------------------------------------------------------------
    def prune(self, tables: list[str]) -> str:
        """A compact slice for the SQL loop: only the columns of the tables a sub-question needs.

        A name that is not an allow-listed live table is left out.
        """
        lines = []
        for table in dict.fromkeys(tables):
            columns = self.visible_columns(table)
            if columns:
                lines.append(f"{table}({', '.join(f'{column.name} {column.type}' for column in columns)})")
        return "\n".join(lines)

    def overview(self) -> str:
        """The allow-listed tables and columns with our own one-line descriptions. Never lists a blocked column."""
        policy = self._policy()
        blocks = []
        for table in sorted(self.tables):
            columns = self.visible_columns(table)
            if not columns:
                continue
            rows = []
            for column in columns:
                description = policy.describe(table, column.name)
                rows.append(f"  - {column.name} ({column.type})" + (f": {description}" if description else ""))
            blocks.append(table + "\n" + "\n".join(rows))
        return "Tables and columns I can use:\n\n" + "\n\n".join(blocks)

    # freshness (A5) ------------------------------------------------------------------------------
    def reference_date(self, today: date | None = None) -> date:
        """What "today" means for "last month" and "up-to-date": the calendar date, or the data's last date."""
        return min(today or datetime.now(timezone.utc).date(), self.latest_date)

    def freshness_note(self, today: date | None = None) -> str | None:
        """A sentence for the answer when the data ends more than a week before today, else None."""
        today = today or datetime.now(timezone.utc).date()
        if (today - self.latest_date).days > STALE_DATA_DAYS:
            return f"The data ends on {self.latest_date.isoformat()}, so \"recent\" means up to that date."
        return None

    # facts for `doctor` --------------------------------------------------------------------------
    def string_columns(self) -> dict[str, list[str]]:
        """Columns that can hold free text (STRING, ARRAY<STRING>, JSON) per table. A reviewer reads these to
        confirm no free-text cell carries PII (A21)."""
        return {
            table: [column.name for column in columns if column.type in FREE_TEXT_TYPES]
            for table, columns in self.tables.items()
        }

    def largest_table(self) -> tuple[str, int] | None:
        sized = {table: size for table, size in self.table_bytes.items() if size}
        if not sized:
            return None
        name = max(sized, key=sized.__getitem__)
        return name, sized[name]


@dataclass(frozen=True)
class CatalogLoad:
    catalog: SchemaCatalog
    warnings: tuple[str, ...] = ()  # a saved copy was used, or a part could not be refreshed


@dataclass(frozen=True)
class _SchemaPart:
    tables: dict[str, list[ColumnInfo]]
    table_bytes: dict[str, int]
    latest_date: date
    checked_at: datetime


@dataclass(frozen=True)
class _BrandsPart:
    brands: frozenset[str]
    checked_at: datetime


def googlesql_type(field_type: str, mode: str = "NULLABLE") -> str:
    name = _GOOGLESQL_TYPES.get(field_type.upper(), field_type.upper())
    return f"ARRAY<{name}>" if mode == "REPEATED" else name


# cache files -----------------------------------------------------------------------------------

def _read_json(path: Path, dataset: str) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        log.warning("ignoring the unreadable cache file %s", path.name)
        return None
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION or data.get("dataset") != dataset:
        return None  # another dataset or an older layout: not our cache
    return data


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    try:
        atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=1))
    except OSError as exc:
        log.warning("could not save %s: %s", path.name, crash_summary(exc))  # the cache is optional


def _aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _schema_from_cache(data: dict[str, Any] | None) -> _SchemaPart | None:
    if data is None:
        return None
    try:
        tables = {
            name: [ColumnInfo(str(c["name"]), str(c["type"]), str(c.get("mode", "NULLABLE")), str(c.get("description", "")))
                   for c in entry["columns"]]
            for name, entry in data["tables"].items()
        }
        sizes = {name: int(entry["num_bytes"]) for name, entry in data["tables"].items()}
        return _SchemaPart(tables, sizes, date.fromisoformat(data["latest_date"]), _aware(data["checked_at"]))
    except (KeyError, TypeError, ValueError, AttributeError):
        log.warning("ignoring the cache file %s: unexpected content", SCHEMA_FILE)
        return None


def _brands_from_cache(data: dict[str, Any] | None) -> _BrandsPart | None:
    if data is None:
        return None
    try:
        brands = data["brands"]
        if not isinstance(brands, list) or not all(isinstance(b, str) for b in brands):
            raise TypeError("brands must be a list of strings")
        return _BrandsPart(frozenset(brands), _aware(data["checked_at"]))
    except (KeyError, TypeError, ValueError):
        log.warning("ignoring the cache file %s: unexpected content", BRANDS_FILE)
        return None


# live reads ------------------------------------------------------------------------------------

def _fetch_schema(safe: SafeQueryRunner, now: datetime) -> _SchemaPart:
    tables: dict[str, list[ColumnInfo]] = {}
    sizes: dict[str, int] = {}
    # The four metadata reads are independent, so they run together: a cold start waits for the slowest, not the sum.
    # During an outage each one retries on its own, all at the same time, so the wait is also one round, not four.
    with ThreadPoolExecutor(max_workers=len(TABLES)) as pool:
        fetched = list(pool.map(safe.get_table, TABLES))
    for name, table in zip(TABLES, fetched):
        columns = [ColumnInfo(f.name, googlesql_type(f.field_type, f.mode), f.mode, f.description or "") for f in table.schema]
        if not columns:
            raise ConfigError(f"The table {name} has no columns in BigQuery.", fix="Check bigquery.dataset in config.yaml.")
        tables[name] = columns
        sizes[name] = int(table.num_bytes or 0)
    _require_columns(tables)  # before the latest-date query, which reads one of them
    return _SchemaPart(tables, sizes, _fetch_latest_date(safe), now)


def _require_columns(tables: dict[str, list[ColumnInfo]]) -> None:
    missing = [
        f"{table}.{column}" for table, columns in REQUIRED_COLUMNS.items() for column in columns
        if column not in {c.name for c in tables.get(table, [])}
    ]
    if missing:
        raise ConfigError(
            f"The dataset has no column {', '.join(missing)}, which the startup checks read.",
            fix="Check bigquery.dataset in config.yaml, or update the column names in src/data_access/ (Q4: read them from the live schema).",
        )


def _fetch_latest_date(safe: SafeQueryRunner) -> date:
    """The newest order item. One column of one table is scanned."""
    outcome = safe.run(SystemQuery(f"SELECT MAX(created_at) AS latest FROM {safe.table_ref('order_items')}"))
    value = outcome.df["latest"].iloc[0] if len(outcome.df) else None
    if value is None or pd.isna(value):
        raise ConfigError("The order_items table has no rows, so there is no latest date.", fix="Check bigquery.dataset in config.yaml.")
    return pd.Timestamp(value).date()


def _fetch_brands(safe: SafeQueryRunner, now: datetime) -> _BrandsPart:
    outcome = safe.run(
        SystemQuery(f"SELECT DISTINCT brand FROM {safe.table_ref('products')} WHERE brand IS NOT NULL", max_rows=BRAND_ROW_LIMIT)
    )
    if outcome.total_rows > len(outcome.df):  # a short list would reject real brands or hide a typo
        raise ConfigError(
            f"The brand list has {outcome.total_rows} entries and only {len(outcome.df)} were read.",
            fix=f"Raise BRAND_ROW_LIMIT in src/data_access/catalog.py (now {BRAND_ROW_LIMIT}).",
        )
    return _BrandsPart(frozenset(str(brand) for brand in outcome.df["brand"]), now)


@dataclass
class _Outage:
    """Set by the first part that finds BigQuery down, so the next part does not wait through the retries again."""

    kind: str = ""
    reason: str = ""

    @property
    def down(self) -> bool:
        return bool(self.kind)


RETRY_FIX = "Check your connection and Google Cloud sign-in (python -m src.cli.main doctor), then try again."
REFUSED_FIX = "Fix the problem the message names (python -m src.cli.main doctor shows the setup), then try again."


def _refresh(
    cached: T | None,
    fetch: Callable[[], T],
    save: Callable[[T], None],
    label: str,
    warnings: list[str],
    now: datetime,
    outage: _Outage,
) -> T:
    """Fetch a fresh part and save it. If BigQuery cannot be reached, fall back to the saved copy when it is under
    7 days old. Anything else stops startup (fail closed): no copy, a copy too old to trust, or an error that is
    not an outage (revoked access, a wrong project, a cap that is too low), which a saved copy would only hide.

    Once one part has found BigQuery down, the others skip the live call: it would only wait through the same
    retries again.
    """
    if not outage.down:
        try:
            fresh = fetch()
        except AgentError as err:
            reason = err.kind.value + (f": {err.detail}" if err.detail else "")
            if not (err.retryable or err.kind == ErrorKind.QUERY_TIMEOUT):
                saved = f", so the saved {label} is not used" if cached is not None else ""
                # A setup or sign-in problem is not BigQuery turning the request down; a cap or a query error is.
                what = "BigQuery cannot be used until the setup is fixed" if err.kind == ErrorKind.BQ_UNAVAILABLE else "BigQuery refused the request"
                raise ConfigError(f"{what} ({reason}){saved}.", fix=REFUSED_FIX) from None
            outage.kind, outage.reason = err.kind.value, reason
        else:
            save(fresh)
            return fresh
    if cached is None:
        raise ConfigError(
            f"BigQuery could not be reached and there is no saved {label}, so there is nothing to check against ({outage.reason}).",
            fix=RETRY_FIX,
        )
    age = now - cached.checked_at
    if age > CACHE_FALLBACK_MAX_AGE:
        raise ConfigError(
            f"BigQuery could not be reached and the saved {label} is {age.days} days old, too old to trust ({outage.reason}).",
            fix=RETRY_FIX,
        )
    warnings.append(
        f"BigQuery could not be reached ({outage.kind}), so the {label} saved on {cached.checked_at:%Y-%m-%d} is used."
    )
    return cached


def _is_stale(checked_at: datetime, now: datetime) -> bool:
    return now - checked_at > CACHE_MAX_AGE


def _not_from_the_future(part: T | None, name: str, now: datetime) -> T | None:
    """A copy dated ahead of the clock (a clock set wrong once, a copied file) would look fresh until the clock
    caught up. It is not trusted, so it is read again."""
    if part is not None and part.checked_at - now > CLOCK_SKEW:
        log.warning("ignoring the cache file %s: it is dated in the future", name)
        return None
    return part


def load_catalog(
    settings: Settings,
    *,
    refresh: bool = False,
    safe: SafeQueryRunner | None = None,
    now: datetime | None = None,
) -> CatalogLoad:
    """The catalog from the saved copy when it is under 24 hours old, else from BigQuery.

    Raises `ConfigError` when BigQuery cannot be reached and there is no saved copy.
    """
    now = now or datetime.now(timezone.utc)
    dataset = settings.bigquery.dataset
    cache_dir = settings.data_path / CACHE_DIR_NAME
    schema = _not_from_the_future(_schema_from_cache(_read_json(cache_dir / SCHEMA_FILE, dataset)), SCHEMA_FILE, now)
    brands = _not_from_the_future(_brands_from_cache(_read_json(cache_dir / BRANDS_FILE, dataset)), BRANDS_FILE, now)
    warnings: list[str] = []
    outage = _Outage()
    safe = safe or SafeQueryRunner(settings)

    if refresh or schema is None or _is_stale(schema.checked_at, now):
        def save_schema(part: _SchemaPart) -> None:
            _write_json(cache_dir / SCHEMA_FILE, {
                "version": CACHE_VERSION, "dataset": dataset, "checked_at": part.checked_at.isoformat(),
                "latest_date": part.latest_date.isoformat(),
                "tables": {
                    name: {"num_bytes": part.table_bytes[name],
                           "columns": [{"name": c.name, "type": c.type, "mode": c.mode, "description": c.description}
                                       for c in columns]}
                    for name, columns in part.tables.items()
                },
            })

        schema = _refresh(schema, lambda: _fetch_schema(safe, now), save_schema, "schema", warnings, now, outage)

    _require_columns(schema.tables)  # also for a saved copy, before the brand query reads products.brand

    if refresh or brands is None or _is_stale(brands.checked_at, now):
        def save_brands(part: _BrandsPart) -> None:
            _write_json(cache_dir / BRANDS_FILE, {
                "version": CACHE_VERSION, "dataset": dataset, "checked_at": part.checked_at.isoformat(),
                "brands": sorted(part.brands),
            })

        brands = _refresh(brands, lambda: _fetch_brands(safe, now), save_brands, "brand list", warnings, now, outage)

    catalog = SchemaCatalog(
        tables=schema.tables, brands=brands.brands, latest_date=schema.latest_date,
        table_bytes=schema.table_bytes, checked_at=schema.checked_at, brands_checked_at=brands.checked_at,
    )
    return CatalogLoad(catalog, tuple(warnings))
