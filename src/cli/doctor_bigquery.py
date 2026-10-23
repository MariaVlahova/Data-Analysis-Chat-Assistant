"""BigQuery checks for `doctor` (plan 03): the dataset, the byte cap, the brand mapping, items without a brand
(A23) and the free-text columns (A21).

They share one loaded catalog. If it cannot be loaded, the first check reports why and the rest are skipped, so
one outage is not reported five times. `doctor.py` registers `BIGQUERY_CHECKS` after its setup checks.

BigQuery and pandas are imported inside the checks, not here: `doctor` must still start on a broken install so
that its Packages check can name what is missing.

The checks do not wait for the credentials check. BigQuery's own lookup is the real test, and it is the one the chat
uses. With no credentials at all it fails after the metadata-server probe, which `gcp_env.py` limits to about a
second, with the same fix as the credentials check.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from src.access.users import load_user_directory, users_path
from src.cli.doctor_core import Check, DoctorContext
from src.errors import AgentError, ConfigError, crash_summary
from src.observability.viewer import fmt_bytes

if TYPE_CHECKING:
    from src.data_access.catalog import CatalogLoad
    from src.data_access.safe_runner import SafeQueryRunner

log = logging.getLogger(__name__)

SUGGESTED_CAP_FACTOR = 5  # plan 03: the cap is about 5 times the largest full-table scan (NFR6)
TOP_BRANDS = 10


def _safe(ctx: DoctorContext) -> "SafeQueryRunner":
    if "safe" not in ctx.shared:
        from src.data_access.safe_runner import SafeQueryRunner

        ctx.shared["safe"] = SafeQueryRunner(ctx.settings)
    return ctx.shared["safe"]


def _prepare(ctx: DoctorContext, name: str, *, report_failure: bool = False) -> "CatalogLoad | Check":
    """The loaded catalog, or the finished `Check` to return when there is none."""
    if ctx.settings is None:
        return Check(name, "skip", "skipped because the config file did not load")
    if "catalog" not in ctx.shared:
        try:
            from src.data_access.catalog import load_catalog

            ctx.shared["catalog"] = load_catalog(ctx.settings, refresh=ctx.refresh, safe=_safe(ctx))
        except ConfigError as exc:
            ctx.shared["catalog"] = exc
        except Exception as exc:  # noqa: BLE001 - whatever it was, load once and report once (a repeat would repeat the retries too)
            log.error("reading the BigQuery dataset failed: %s", crash_summary(exc))  # class and call sites, never the text
            ctx.shared["catalog"] = ConfigError(
                f"Reading the BigQuery dataset failed unexpectedly ({type(exc).__name__}).",
                fix="The call sites are in data/logs/app.log (class and location only).",
            )
    loaded = ctx.shared["catalog"]
    if isinstance(loaded, ConfigError):
        if report_failure:
            return Check(name, "fail", loaded.message, loaded.fix or "")
        return Check(name, "skip", "skipped because the BigQuery dataset check failed")
    return loaded


def check_bigquery_dataset(ctx: DoctorContext) -> Check:
    name = "BigQuery dataset"
    loaded = _prepare(ctx, name, report_failure=True)
    if isinstance(loaded, Check):
        return loaded
    catalog = loaded.catalog
    detail = (
        f"{ctx.settings.bigquery.dataset}: {len(catalog.tables)} tables, latest order item {catalog.latest_date.isoformat()}, "
        f"read {catalog.checked_at:%Y-%m-%d %H:%M} UTC"
    )
    note = catalog.freshness_note()
    if note:
        detail += f". {note}"  # A5: the answer says it too
    if loaded.warnings:
        return Check(name, "warn", detail + ". " + " ".join(loaded.warnings),
                     "Check your connection and Google Cloud sign-in, then run: python -m src.cli.main doctor --refresh")
    return Check(name, "ok", detail)


def check_byte_cap(ctx: DoctorContext) -> Check:
    name = "Byte cap"
    loaded = _prepare(ctx, name)
    if isinstance(loaded, Check):
        return loaded
    config = ctx.settings.bigquery
    largest = loaded.catalog.largest_table()
    base = f"{fmt_bytes(config.max_bytes_billed)} per query, {fmt_bytes(ctx.settings.budget.bytes_per_turn)} per turn"
    if largest is None:
        return Check(name, "warn", base + "; BigQuery reports no table sizes, so the cap cannot be checked",
                     "Check bigquery.max_bytes_billed by hand against the dataset's size.")
    table, size = largest
    detail = (f"{base}; largest table is {table} at {fmt_bytes(size)}, "
              f"about {SUGGESTED_CAP_FACTOR}x is {fmt_bytes(SUGGESTED_CAP_FACTOR * size)}")
    if config.max_bytes_billed < size:
        return Check(name, "warn", detail, "Raise bigquery.max_bytes_billed in config.yaml: a scan of the largest table would be refused.")
    return Check(name, "ok", detail)


def _offline(ctx: DoctorContext) -> bool:
    """True when loading the catalog found BigQuery down and used a saved copy. Another live query would only wait
    through the same retries again."""
    loaded = ctx.shared.get("catalog")
    return bool(getattr(loaded, "warnings", ()))


def _top_brands_hint(ctx: DoctorContext) -> str:
    from src.data_access.brand_check import top_brands

    if _offline(ctx):
        return ""
    try:
        rows = top_brands(_safe(ctx), TOP_BRANDS)
    except Exception as exc:  # noqa: BLE001 - a convenience must never replace the failure it decorates
        log.warning("could not list the top brands: %s", crash_summary(exc))
        return ""
    return " Top brands by item count: " + ", ".join(f"{brand} ({items:,})" for brand, items in rows) + "." if rows else ""


def check_brand_mapping_live(ctx: DoctorContext) -> Check:
    name = "Brand mapping"
    loaded = _prepare(ctx, name)
    if isinstance(loaded, Check):
        return loaded
    from src.data_access.brand_check import BrandMappingError, check_brand_mapping

    try:
        users = load_user_directory(users_path(ctx.settings), salt="doctor")  # fixed salt: the check must not write files
    except (AgentError, ConfigError):
        return Check(name, "skip", "skipped because users.yaml did not load")
    scoped = [user for user in (users.get(n) for n in users.names) if user.scope_mode == "brands"]
    try:
        check_brand_mapping(users, loaded.catalog)  # every user, unlike the chat, which checks the one who starts
    except BrandMappingError as exc:
        return Check(name, "fail", "; ".join(exc.problems), (exc.fix or "") + _top_brands_hint(ctx))
    brands = sum(len(user.brands) for user in scoped)
    return Check(name, "ok", f"{brands} brand(s) of {len(scoped)} brand manager(s) all match the {len(loaded.catalog.brands)} brands in the data")


def check_brand_coverage(ctx: DoctorContext) -> Check:
    name = "Items without a brand (A23)"
    loaded = _prepare(ctx, name)
    if isinstance(loaded, Check):
        return loaded
    from src.data_access.brand_check import brand_coverage

    if _offline(ctx):
        return Check(name, "skip", "skipped because BigQuery could not be reached (the saved schema was used)")
    try:
        coverage = brand_coverage(_safe(ctx))
    except Exception as exc:  # noqa: BLE001
        reason = exc.kind.value if isinstance(exc, AgentError) else type(exc).__name__
        if not isinstance(exc, AgentError):
            log.error("the A23 count failed: %s", crash_summary(exc))
        return Check(name, "warn", f"could not count them ({reason})", "Run doctor again when BigQuery answers.")
    if coverage.invisible_to_managers == 0:
        return Check(name, "ok", f"all {coverage.items:,} order items join to a brand")
    share = coverage.invisible_to_managers / coverage.items * 100 if coverage.items else 0.0
    return Check(
        name, "warn",
        f"{coverage.invisible_to_managers:,} of {coverage.items:,} order items ({share:.2f}%) have no product or no brand: "
        "brand managers cannot see them, only the CEO can",
        "Write this count in the docs (A23).",
    )


def check_string_columns(ctx: DoctorContext) -> Check:
    name = "Text columns (A21)"
    loaded = _prepare(ctx, name)
    if isinstance(loaded, Check):
        return loaded
    listed = [f"{table}: {', '.join(columns)}" for table, columns in sorted(loaded.catalog.string_columns().items()) if columns]
    return Check(name, "ok", "a reviewer confirms none holds free text with PII. " + "; ".join(listed))


# In the order they run. `doctor.py` registers them after the credentials and project checks.
BIGQUERY_CHECKS = [check_bigquery_dataset, check_byte_cap, check_brand_mapping_live, check_brand_coverage, check_string_columns]
