"""`local-data`: build, inspect and seed the sample data that lets the whole app run with no Google sign-in.

    python -m src.cli.main local-data build           # writes the DuckDB file named by bigquery.local_db
    python -m src.cli.main local-data info            # what is in it
    python -m src.cli.main local-data seed-reports    # a few saved reports in the app database, to try delete and undo

They work on the file the config names, so run them with `--config config.local.yaml` (or set OPSFLEET_CONFIG). DuckDB loads only
when one of them runs, so a live run never needs it.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Annotated, Optional

import typer

from src.cli.render import Renderer
from src.config import Settings, load_settings
from src.errors import ConfigError, crash_summary

local_data_app = typer.Typer(
    name="local-data", help="Build and inspect generated sample data (a stand-in for BigQuery, for development and demos).",
    no_args_is_help=True, pretty_exceptions_enable=False,
)

ConfigOption = Annotated[Optional[Path], typer.Option("--config", help="Path to config.yaml (use config.local.yaml for the sample data).")]


def _settings(renderer: Renderer, config: Path | None) -> Settings:
    try:
        return load_settings(config)
    except ConfigError as exc:
        renderer.startup_problem(exc.message, exc.fix)
        raise typer.Exit(2) from None


def _need_duckdb(renderer: Renderer) -> None:
    try:
        import duckdb  # noqa: F401
    except ImportError:
        renderer.startup_problem("DuckDB is not installed, and the sample data needs it.", "Run: pip install -r requirements.txt")
        raise typer.Exit(2) from None


def _forget_cached_schema(settings: Settings) -> None:
    """The catalog keeps the schema and brand list for 24 hours. New data must not be read through the old copy."""
    if settings.bigquery.backend != "local":
        return  # a cache in a live data folder belongs to the live dataset
    for name in ("schema.json", "brands.json"):
        (settings.data_path / "cache" / name).unlink(missing_ok=True)


@local_data_app.command()
def build(
    config: ConfigOption = None,
    seed: Annotated[int, typer.Option("--seed", help="Random seed. The same seed and end date give the same rows.")] = 7,
    users: Annotated[int, typer.Option("--users", min=100, help="How many customers to generate.")] = 6000,
    end_date: Annotated[Optional[str], typer.Option("--end-date", help="Last day of data, YYYY-MM-DD. Default: yesterday (UTC).")] = None,
) -> None:
    """Generate the sample data, replacing any file that is there."""
    renderer = Renderer()
    _need_duckdb(renderer)
    from src.localdata.generate import BuildOptions, generate

    settings = _settings(renderer, config)
    try:
        end = date.fromisoformat(end_date) if end_date else None
    except ValueError:
        renderer.startup_problem(f"--end-date must be a date like 2026-10-22, not '{end_date}'.")
        raise typer.Exit(2) from None
    path = settings.resolve(settings.bigquery.local_db)
    try:
        with renderer.progress("Generating the sample data..."):
            summary = generate(path, BuildOptions(seed=seed, users=users, end=end))
    except ValueError as exc:
        renderer.startup_problem(str(exc))
        raise typer.Exit(2) from None
    except Exception as exc:  # noqa: BLE001 - the interface never crashes
        renderer.startup_problem(f"The sample data could not be built ({crash_summary(exc)}).")
        raise typer.Exit(1) from None
    _forget_cached_schema(settings)
    renderer.info(f"Built {summary.path}")
    renderer.info("Rows: " + ", ".join(f"{name} {count:,}" for name, count in summary.rows.items()))
    renderer.info(f"Dates: {summary.start.isoformat()} to {summary.end.isoformat()} (seed {summary.seed})")
    for note in summary.notes:
        renderer.info(f"  planted: {note}")
    renderer.info("Next: python -m src.cli.main local-data seed-reports --config config.local.yaml, then chat --user alice --config config.local.yaml")


@local_data_app.command()
def info(config: ConfigOption = None) -> None:
    """Show what is in the sample data: rows, dates, brands and the effects it was built to contain."""
    renderer = Renderer()
    _need_duckdb(renderer)
    from src.localdata.info import read_info

    settings = _settings(renderer, config)
    try:
        data = read_info(settings.resolve(settings.bigquery.local_db))
    except ConfigError as exc:
        renderer.startup_problem(exc.message, exc.fix)
        raise typer.Exit(2) from None
    renderer.info(f"{data.path}")
    renderer.info(f"Built with seed {data.meta.get('seed')} for {data.meta.get('users')} users, {data.meta.get('start')} to {data.meta.get('end')}")
    renderer.info("Tables: " + ", ".join(f"{name} {rows:,} rows" for name, rows in data.rows.items()))
    renderer.info(f"Latest order item: {data.latest_item}")
    renderer.info("Brands (products, order items):")
    for name, products, items in data.brands:
        renderer.info(f"  {name!r}: {products} products, {items:,} items")
    renderer.info("Planted:")
    for note in data.notes:
        renderer.info(f"  {note}")


@local_data_app.command()
def export(
    to: Annotated[Path, typer.Option("--to", help="Folder for the Parquet files, one per table.")],
    config: ConfigOption = None,
) -> None:
    """Write the sample data as Parquet files, to load into a real BigQuery dataset with `bq load` (docs/local-testing.md)."""
    renderer = Renderer()
    _need_duckdb(renderer)
    from src.localdata.export import export_parquet

    settings = _settings(renderer, config)
    try:
        written = export_parquet(settings.resolve(settings.bigquery.local_db), to)
    except ConfigError as exc:
        renderer.startup_problem(exc.message, exc.fix)
        raise typer.Exit(2) from None
    except Exception as exc:  # noqa: BLE001 - the interface never crashes
        renderer.startup_problem(f"The files could not be written ({crash_summary(exc)}).")
        raise typer.Exit(1) from None
    for table, rows in written.items():
        renderer.info(f"{to / (table + '.parquet')}: {rows:,} rows")
    renderer.info("Load each into a dataset of your own, for example: bq load --source_format=PARQUET --replace MY_DATASET.orders orders.parquet")


@local_data_app.command("seed-reports")
def seed_reports(config: ConfigOption = None) -> None:
    """Add a few saved reports for the users in users.yaml (alice, bob, ceo), to try the delete and undo flow."""
    renderer = Renderer()
    settings = _settings(renderer, config)
    from src.localdata.reports import seed_saved_reports

    try:
        result = seed_saved_reports(settings)
    except ConfigError as exc:
        renderer.startup_problem(exc.message, exc.fix)
        raise typer.Exit(2) from None
    except Exception as exc:  # noqa: BLE001 - the interface never crashes
        renderer.startup_problem(f"The reports could not be added ({crash_summary(exc)}).")
        raise typer.Exit(1) from None
    for user_id, added in result.saved.items():
        renderer.info(f"{user_id}: {added} report(s) added" + ("" if added else " (already there)"))
    if result.skipped_users:
        renderer.info("Not in users.yaml, so skipped: " + ", ".join(result.skipped_users))
