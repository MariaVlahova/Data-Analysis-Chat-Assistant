"""Judge a finished run with the rubrics (plan 09):  python -m eval.run_judge RUN_DIR --config config.yaml

A suite (`run_golden`, `run_attacks`, `run_chaos`) plays the conversations and checks them with code. This reads the run it left (`evidence/`) and has a model score
each turn against the six rubrics of `evalkit/rubrics.xlsx`, and writes `RUN_DIR/judge/`. Nothing is replayed, so the same run can be judged again by another model
(`--out` for a second folder).

The judge is a model and must not be the one that answered (decision 3.13): `--config` names the model service that judges. With the local config on a local run it
is the same model, and the command stops, unless you pass `--allow-self-judge`; the result is then marked as not independent. A judge score is advisory: it never
changes the exit code, a baseline or a hard gate. Exit codes: 0 judged, 1 the judge gave no verdict at all, 2 it could not start.

    python -m eval.run_judge data/local/eval/golden-20261025-090828 --config config.yaml           # a Gemini judge for a local run
    python -m eval.run_judge data/local/eval/golden-20261025-090828 --config config.local.yaml --allow-self-judge
    python -m eval.run_judge RUN_DIR --only groundedness safety --repeats 3 --out RUN_DIR/judge-2
"""
from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Callable, Optional

import typer
from rich.table import Table
from rich.text import Text

from eval.judging import RUBRICS_FILE, JudgeRun, judge_run, lowest
from eval.suite import start
from evalkit.rubrics import RubricError, load_rubrics
from src.cli.render import Renderer
from src.errors import ConfigError
from src.llm.gateway import LLMGateway
from src.llm.routing import Step

app = typer.Typer(add_completion=False, pretty_exceptions_enable=False, help=__doc__)


def render(renderer: Renderer, run: JudgeRun) -> None:
    table = Table(title=f"judge: {', '.join(run.models)} on run {run.verdicts.get('run_id')} ({run.verdicts.get('kind')})")
    for column in ("Rubric", "Stage", "Scored", "Mean", "Pass", "Bar", "N/A", "Not exercised"):
        table.add_column(column, justify="left" if column in ("Rubric", "Stage") else "right")
    summary = run.report.summary["metrics"]
    for key, rubric in run.rubrics.items():
        item = summary.get(key, {})
        mean, rate = item.get("mean"), item.get("pass_rate")
        table.add_row(
            key, rubric.stage, str(item.get("n_scored", 0)), "-" if mean is None else f"{mean:.2f}", "-" if rate is None else f"{rate:.0%}", f"{rubric.threshold:.2f}",
            str(item.get("n_na", 0)), str(item.get("n_not_exercised", 0)),
        )
    renderer.console.print(table)
    for row in lowest(run):
        renderer.console.print(Text(f"below the bar: {row['case']} turn {row['turn']}, {row['rubric']} {row['score']:.2f} (judge {row['judge_score']}/4): {row['reason'][:160]}", style="yellow"))
    if run.self_judged:
        renderer.console.print(Text("NOT INDEPENDENT: the judge is the model that answered in this run. Read these scores as a smoke test, not as a grade.", style="bold red"))
    if run.failed_calls:
        renderer.console.print(Text(f"{run.failed_calls} judge call(s) failed ({', '.join(sorted(set(run.gateway_judge.failures)))}); those turns are N/A, not passes.", style="bold red"))
    tokens = run.gateway_judge
    renderer.console.print(Text(
        f"{tokens.verdicts} verdict(s), {tokens.tokens_in + tokens.tokens_out:,} tokens. Advisory: a judge score does not change the exit code.", style="dim"))


def run_judge(
    folder: Path, *, config: Path | None, out: Path | None, rubrics_file: Path, only: list[str], repeats: int, allow_self_judge: bool, skip_preflight: bool,
    gateway_factory: Callable[..., Any] | None = None,
) -> int:
    renderer = Renderer()
    settings = start(config, skip_preflight=skip_preflight, renderer=renderer)
    try:
        rubrics = load_rubrics(rubrics_file)
        gateway = (gateway_factory or LLMGateway)(settings)
        models = gateway.router.chain(Step.JUDGE)
        run = judge_run(
            folder, gateway, settings, models=models, rubrics=rubrics, only=set(only) or None, repeats=repeats, allow_self_judge=allow_self_judge, out=out,
            say=lambda text: renderer.info(text),
        )
    except (ConfigError, RubricError) as exc:
        message, fix = (exc.message, exc.fix) if isinstance(exc, ConfigError) else (str(exc), "Fix the rubric file, or leave --rubrics out.")
        renderer.startup_problem(message, fix)
        return 2
    render(renderer, run)
    renderer.info(f"Judge files in {run.folder}")
    return 0 if run.scored else 1


@app.command()
def main(
    run: Annotated[Path, typer.Argument(help="The run folder to judge: it holds verdicts.json and evidence/.")],
    config: Annotated[Optional[Path], typer.Option("--config", help="Path to the config.yaml whose model judges. It must not be the model that answered.")] = None,
    out: Annotated[Optional[Path], typer.Option("--out", help="Folder for the judge's files. Default: <run>/judge.")] = None,
    rubrics: Annotated[Path, typer.Option("--rubrics", help="The rubric file (.xlsx or .csv).")] = RUBRICS_FILE,
    only: Annotated[Optional[list[str]], typer.Option("--only", help="Judge only these rubrics (repeat the option).")] = None,
    repeats: Annotated[int, typer.Option("--repeats", min=1, help="Judge each sample this many times; the median score is kept and the spread is reported.")] = 1,
    allow_self_judge: Annotated[bool, typer.Option("--allow-self-judge", help="Let the model that answered also judge. The result is marked as not independent.")] = False,
    skip_preflight: Annotated[bool, typer.Option("--skip-preflight", help="Do not check first that the model is reachable.")] = False,
) -> None:
    raise typer.Exit(run_judge(
        run, config=config, out=out, rubrics_file=rubrics, only=only or [], repeats=repeats, allow_self_judge=allow_self_judge, skip_preflight=skip_preflight,
    ))


if __name__ == "__main__":
    app()
