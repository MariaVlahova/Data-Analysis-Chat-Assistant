"""`scenario`: run the scripted test scenarios against the real app, with the model of the environment.

    python -m src.cli.main scenario list                         --config config.local.yaml
    python -m src.cli.main scenario run                          --config config.local.yaml   # the smoke scenarios
    python -m src.cli.main scenario run --all                    --config config.local.yaml
    python -m src.cli.main scenario run pii-direct-requests      --config config.local.yaml
    python -m src.cli.main scenario run --tag safety --tag scope --config config.local.yaml

With `config.local.yaml` the environment is `local`: the sample data and `qwen/qwen3-30b-a3b-2507` on this machine. A turn takes minutes at
that speed, so a plain `run` does only the `smoke` scenarios. Each run writes `results.json` and a folder per scenario (its database, traces
and logs) under `<data_dir>/scenarios/<run id>/`. The exit code is 1 when any scenario fails.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.text import Text

from src.cli.render import Renderer
from src.config import Settings, load_settings
from src.errors import ConfigError

scenario_app = typer.Typer(
    name="scenario", help="Run the scripted test scenarios against the app, with the model of the environment.",
    no_args_is_help=True, pretty_exceptions_enable=False,
)

DEFAULT_TAG = "smoke"
ConfigOption = Annotated[Optional[Path], typer.Option("--config", help="Path to config.yaml (config.local.yaml for the local model and sample data).")]
DirOption = Annotated[Optional[Path], typer.Option("--dir", help="A scenario file or a folder of them. Default: scenarios/ next to the config.")]


def _settings(renderer: Renderer, config: Path | None) -> Settings:
    try:
        return load_settings(config)
    except ConfigError as exc:
        renderer.startup_problem(exc.message, exc.fix)
        raise typer.Exit(2) from None


def _load(renderer: Renderer, settings: Settings, location: Path | None):
    from src.scenarios.schema import load_scenarios

    folder = location or settings.resolve("scenarios")
    if not folder.exists():
        renderer.startup_problem(f"No scenarios found at {folder}.", "Pass --dir with a scenario file or folder.")
        raise typer.Exit(2)
    loaded = load_scenarios(folder)
    broken = [item for item in loaded if item.scenario is None]
    for item in broken:
        renderer.console.print(Text(f"[FAIL] {item.path.name}: {item.error}", style="bold red"))
    if broken:
        raise typer.Exit(2)
    return [item.scenario for item in loaded]


@scenario_app.command("list")
def list_scenarios(config: ConfigOption = None, dir: DirOption = None) -> None:
    """List the scenarios: id, user, tags, turns, and what each needs."""
    renderer = Renderer()
    settings = _settings(renderer, config)
    from src.scenarios.runner import unmet_requirement

    for scenario in _load(renderer, settings, dir):
        why = unmet_requirement(scenario, settings)
        line = f"{scenario.id:34} {scenario.user:9} {len(scenario.turns)} turn(s)  [{', '.join(scenario.tags)}]  {scenario.title}"
        renderer.console.print(Text(line + (f"   (skipped here: {why})" if why else ""), style="dim" if why else ""))


@scenario_app.command("run")
def run(
    ids: Annotated[Optional[list[str]], typer.Argument(help="Scenario IDs. Default: the scenarios tagged smoke.")] = None,
    tag: Annotated[Optional[list[str]], typer.Option("--tag", help="Run the scenarios with this tag. Repeat for more.")] = None,
    all_scenarios: Annotated[bool, typer.Option("--all", help="Run every scenario.")] = False,
    config: ConfigOption = None,
    dir: DirOption = None,
    out: Annotated[Optional[Path], typer.Option("--out", help="Folder for the results. Default: <data_dir>/scenarios/<run id>.")] = None,
    skip_preflight: Annotated[bool, typer.Option("--skip-preflight", help="Do not check that the model is reachable first.")] = False,
) -> None:
    """Run scenarios and print what passed. Exit code 1 when any scenario fails."""
    renderer = Renderer()
    settings = _settings(renderer, config)
    scenarios = _load(renderer, settings, dir)
    by_id = {scenario.id: scenario for scenario in scenarios}
    unknown = [name for name in ids or [] if name not in by_id]
    if unknown:
        renderer.startup_problem(f"No scenario called {', '.join(unknown)}.", "Run `scenario list` to see the IDs.")
        raise typer.Exit(2)
    if ids:
        chosen = [by_id[name] for name in ids]
    elif all_scenarios:
        chosen = scenarios
    else:
        wanted = set(tag or [DEFAULT_TAG])
        chosen = [scenario for scenario in scenarios if wanted & set(scenario.tags)]
    if not chosen:
        renderer.startup_problem("No scenario matches.", "Run `scenario list` to see the IDs and tags, or use --all.")
        raise typer.Exit(2)

    if not skip_preflight:
        try:
            _preflight(settings)
        except ConfigError as exc:
            renderer.startup_problem(exc.message, exc.fix)
            raise typer.Exit(2) from None

    from src.scenarios import report as report_module
    from src.scenarios.runner import ScenarioRunner

    run_id = time.strftime("%Y%m%d-%H%M%S")
    folder = out or settings.data_path / "scenarios" / run_id
    backend = settings.llm_backend
    renderer.info(f"Run {run_id}: {len(chosen)} scenario(s), environment {settings.app.environment}, model {backend.model or 'Gemini (llm.models)'}. Results in {folder}")
    try:
        runner = ScenarioRunner(settings, folder, say=lambda text: renderer.console.print(Text(text)))
    except ConfigError as exc:
        renderer.startup_problem(exc.message, exc.fix)
        raise typer.Exit(2) from None
    try:
        result = runner.run(chosen, run_id)
    finally:
        runner.close()
    report_module.write(result)
    report_module.render(renderer.console, result)
    raise typer.Exit(1 if result.failed else 0)


def _preflight(settings: Settings) -> None:
    """Fail early, with the fix, when the model cannot be reached: a scenario run takes minutes and every turn would fail the same way."""
    from src.llm.gateway import LLMGateway

    backend = settings.llm_backend
    if backend.provider == "gemini":
        if settings.secrets.gemini_api_key is None:
            raise ConfigError(
                f"GEMINI_API_KEY is not set, and the environment '{settings.app.environment}' uses Gemini.",
                fix="Put the key in .env, or run with the local model: --config config.local.yaml.",
            )
        return
    gateway = LLMGateway(settings)
    start = "Start the model server (in LM Studio: load the model, then start the local server)"
    try:
        available = gateway.available_models()
    except Exception:  # noqa: BLE001 - whatever stops us from listing the models means the server is not usable
        raise ConfigError(f"The model server did not answer at {backend.base_url}.", fix=f"{start}, or set llm.environments.local.base_url.") from None
    missing = [model for model in gateway.router.models_in_use() if model not in available]
    if missing:
        raise ConfigError(
            f"The model server at {backend.base_url} does not list {', '.join(missing)}.",
            fix=f"Load that model, or set llm.environments.local.model to one of: {', '.join(sorted(available)) or 'none'}.",
        )
