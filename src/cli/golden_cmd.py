"""`golden validate`: the Ingest Job in miniature (plan 10).

    python -m src.cli.main golden validate [PATH] [--offline] [--config config.local.yaml]

Runs the checks a trio passes before it can be retrieved (schema, tables, the SQL Guard, a BigQuery dry run, personal data, brands,
duplicates; see `src/golden/validate.py`) and prints one line per trio. A trio that fails is not loaded by the chat. The exit code
is 1 when any trio fails.

The dry run and the brand list need BigQuery (or the local sample data). When it cannot be read, those checks are skipped and the
output says so; `--offline` skips them on purpose.
"""
from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.text import Text

from src.cli.render import Renderer
from src.config import Settings, load_settings
from src.errors import AgentError, ConfigError, crash_summary
from src.logging_setup import setup_logging, shutdown_logging

golden_app = typer.Typer(
    name="golden", help="Check and inspect the Golden Bucket (solved questions and the glossary).",
    no_args_is_help=True, pretty_exceptions_enable=False,
)

_MARKS = {"ok": ("[ ok ]", "green"), "warn": ("[warn]", "yellow"), "fail": ("[FAIL]", "bold red")}


def _settings(renderer: Renderer, config: Path | None) -> Settings:
    try:
        return load_settings(config)
    except ConfigError as exc:
        renderer.startup_problem(exc.message, exc.fix)
        raise typer.Exit(2) from None


@golden_app.command()
def validate(
    path: Annotated[Optional[Path], typer.Argument(help="A trio file or a folder of them. Default: golden.seed_dir from the config.")] = None,
    config: Annotated[Optional[Path], typer.Option("--config", help="Path to config.yaml.")] = None,
    offline: Annotated[bool, typer.Option("--offline", help="Skip the checks that need the data: the brand list and the dry run.")] = False,
) -> None:
    """Check every trio and the glossary, and say which would be loaded."""
    renderer = Renderer()
    settings = _settings(renderer, config)
    try:
        setup_logging(settings)  # the catalog and runner log to the masked app.log, not to the screen
        _validate(renderer, settings, path, offline)
    finally:
        shutdown_logging()


def _validate(renderer: Renderer, settings: Settings, path: Path | None, offline: bool) -> None:
    from src.golden.glossary import load_glossary
    from src.golden.validate import ValidationContext, validate_location
    from src.guards.allowlist import load_allowlist
    from src.guards.sql_guard import SQLGuard

    location = path or settings.resolve(settings.golden.seed_dir)
    if not location.exists():
        renderer.startup_problem(f"Nothing to check at {location}.", "Pass a trio file or folder, or set golden.seed_dir in the config.")
        raise typer.Exit(2)
    try:
        allowlist = load_allowlist(settings)
    except ConfigError as exc:
        renderer.startup_problem(exc.message, exc.fix)
        raise typer.Exit(2) from None

    brands = None
    dry_run = None
    notes: list[str] = []
    if offline:
        notes.append("Offline: the brand list and the BigQuery dry run were not checked.")
    else:
        try:
            from src.data_access.catalog import load_catalog
            from src.data_access.safe_runner import SafeQueryRunner

            safe = SafeQueryRunner(settings)
            loaded = load_catalog(settings, safe=safe)
            brands, dry_run = loaded.catalog.brands, safe.dry_run
            notes.extend(loaded.warnings)
            if settings.bigquery.backend == "local":
                notes.append("The dry run and the brand list come from the LOCAL SAMPLE DATA, not from BigQuery.")
        except (ConfigError, AgentError) as exc:
            reason = exc.message if isinstance(exc, ConfigError) else exc.kind.value
            notes.append(f"The data could not be read ({reason}), so the brand list and the dry run were skipped.")
        except Exception as exc:  # noqa: BLE001 - a diagnostic still prints its checks
            notes.append(f"The data could not be read ({crash_summary(exc)}), so the brand list and the dry run were skipped.")

    results = validate_location(location, ValidationContext(sql_guard=SQLGuard(settings, allowlist), brands=brands, dry_run=dry_run))
    for note in notes:
        renderer.info(note)
    for result in results:
        status = "fail" if not result.ok else "warn" if result.warnings() else "ok"
        mark, style = _MARKS[status]
        line = Text()
        line.append(mark, style=style)
        line.append(f" {result.name}")
        if result.trio is not None:
            line.append(f": {result.trio.question}", style="dim")
            line.append(f" ({result.trio.status}, brands {', '.join(result.trio.brands)})", style="dim")
        renderer.console.print(line)
        for issue in result.issues:
            renderer.console.print(Text(f"       {issue.level.upper()} {issue.check}: {issue.message}"))

    glossary, problems = load_glossary(settings.resolve(settings.golden.glossary_file))
    locked = sum(1 for entry in glossary.entries if entry.locked)
    renderer.info(f"Glossary: {len(glossary.entries)} entries ({locked} already enforced in code)")
    for problem in problems:
        renderer.console.print(Text(f"       WARN Glossary: {problem}", style="yellow"))

    failed = sum(1 for result in results if not result.ok)
    searchable = sum(1 for result in results if result.ok and result.trio is not None and result.trio.searchable)
    warned = sum(len(result.warnings()) for result in results)
    summary = f"{len(results)} trio(s): {len(results) - failed} pass, {failed} fail, {warned} warning(s); {searchable} would be searchable."
    renderer.console.print(Text(summary, style="bold green" if not failed else "bold red"))
    if failed:
        raise typer.Exit(1)
