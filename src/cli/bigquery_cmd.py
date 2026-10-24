"""`bigquery init-users`: write a users file whose brands are read from the data (never guessed).

    python -m src.cli.main bigquery init-users --config config.bigquery.yaml

The live users.yaml ships with placeholder brands (`<verified brand 1>`) because the real names are only known once the dataset can be read.
This command reads the busiest brands from the dataset the config names and writes the same six users as users.local.yaml, so the scenarios
and the manual test script work unchanged on real data:

| User | Brands |
|---|---|
| alice | the busiest and the second busiest |
| bob | the third |
| carol | the fourth |
| dave | the fifth and the sixth |
| ceo | all brands |
| nobrands | none (the chat must refuse to start for this user) |

It refuses to overwrite a file unless `--force`. It works with whichever data the config names: BigQuery, or the sample data.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Optional

import typer

from src.cli.render import Renderer
from src.config import Settings, load_settings
from src.errors import AgentError, ConfigError, crash_summary

bigquery_app = typer.Typer(
    name="bigquery", help="Steps for running the app and the scenarios against real BigQuery.", no_args_is_help=True, pretty_exceptions_enable=False,
)

WANTED_BRANDS = 6  # alice 2, bob 1, carol 1, dave 2
ASSIGNMENTS = (("alice", "Alice", (0, 1)), ("bob", "Bob", (2,)), ("carol", "Carol", (3,)), ("dave", "Dave", (4, 5)))


def users_yaml(brands: list[tuple[str, int]]) -> str:
    """The text of the users file for these brands (name, items), busiest first. Names are written as JSON strings, which YAML reads as they are."""
    quote = lambda name: json.dumps(name, ensure_ascii=False)  # noqa: E731
    lines = [
        "# Users for real BigQuery data. Written by `python -m src.cli.main bigquery init-users`: the brands are the busiest ones in the dataset",
        "# (by order items), read from the data and not guessed. Edit freely; brand names are compared exactly, case and spacing count.",
        "# An empty brand list means no access, and only the CEO can have all_brands: true.",
        "users:",
    ]
    for user_id, display, picks in ASSIGNMENTS:
        names = ", ".join(quote(brands[position][0]) for position in picks)
        lines += [f"  {user_id}:", f"    name: {display}", "    role: brand_manager", f"    brands: [{names}]"]
    lines += ["  ceo:", "    name: CEO", "    role: ceo", "    all_brands: true", "  nobrands:", "    name: No Brands", "    role: brand_manager", "    brands: []"]
    return "\n".join(lines) + "\n"


def _settings(renderer: Renderer, config: Path | None) -> Settings:
    try:
        return load_settings(config)
    except ConfigError as exc:
        renderer.startup_problem(exc.message, exc.fix)
        raise typer.Exit(2) from None


@bigquery_app.command("init-users")
def init_users(
    config: Annotated[Optional[Path], typer.Option("--config", help="Path to config.yaml (config.bigquery.yaml for real BigQuery).")] = None,
    out: Annotated[Optional[Path], typer.Option("--out", help="Where to write. Default: the file app.users_file names, next to the config.")] = None,
    force: Annotated[bool, typer.Option("--force", help="Replace the file if it exists.")] = False,
) -> None:
    """Write a users file with the busiest brands of the dataset, so no brand name is guessed."""
    renderer = Renderer()
    settings = _settings(renderer, config)
    target = out or settings.resolve(settings.app.users_file)
    if target.exists() and not force:
        renderer.startup_problem(f"{target} already exists.", "Pass --force to replace it, or --out to write somewhere else.")
        raise typer.Exit(2)

    from src.access.users import load_user_directory
    from src.data_access.brand_check import top_brands
    from src.data_access.safe_runner import SafeQueryRunner

    try:
        with renderer.progress("Reading the busiest brands..."):
            brands = top_brands(SafeQueryRunner(settings), WANTED_BRANDS + 4)
    except (ConfigError, AgentError) as exc:
        reason = exc.message if isinstance(exc, ConfigError) else f"{exc.kind.value}: {exc.detail or exc.user_message}"
        renderer.startup_problem(f"The brands could not be read ({reason}).", "Run `python -m src.cli.main doctor` for the sign-in and project checks.")
        raise typer.Exit(2) from None
    except Exception as exc:  # noqa: BLE001 - the interface never crashes
        renderer.startup_problem(f"The brands could not be read ({crash_summary(exc)}).")
        raise typer.Exit(1) from None
    brands = [(name, items) for name, items in brands if name.strip()]
    if len(brands) < WANTED_BRANDS:
        renderer.startup_problem(f"The dataset has only {len(brands)} brand(s) with items; {WANTED_BRANDS} are needed for the six users.", "Write the users file by hand.")
        raise typer.Exit(2)

    text = users_yaml(brands)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    load_user_directory(target, salt="check")  # what was written must be a valid users file
    renderer.info(f"Wrote {target}")
    for user_id, _, picks in ASSIGNMENTS:
        renderer.info(f"  {user_id}: " + ", ".join(f"{brands[p][0]} ({brands[p][1]:,} items)" for p in picks))
    renderer.info("  ceo: all brands; nobrands: none")
    renderer.info("Next: python -m src.cli.main doctor --refresh, then scenario run --config <the same config>")
