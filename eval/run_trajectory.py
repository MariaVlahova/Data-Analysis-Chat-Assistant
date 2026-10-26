"""Run the trajectory cases against the real app (plan 09):  python -m eval.run_trajectory --config config.local.yaml

Scripted conversations that ask two things of the app. Sequence and handoff accuracy: did each turn take the path through the graph that the design says, with every guard
before what it guards, and did each step hand the next what it should? State drift and memory consistency: after each turn, is the checkpoint what the design says
(nothing stale from an earlier turn, the history, the last answer and the shown customer labels changed only as they should), also after the app is started again?

The rules of the code (`eval/trajectory.py`: the nine checks that run on every turn of every suite) are the hard gates here too: one violation fails the run. What a case
expects of a path or of the memory depends on how the model classified and answered, so a live run shows it as a finding and does not fail on it; the offline run
(tests/test_trajectory_offline.py) scripts the model and requires all of it.

    python -m eval.run_trajectory --config config.local.yaml
    python -m eval.run_trajectory path-simple-skips-the-planner --config config.local.yaml
"""
from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer

from eval.suite import run_suite

app = typer.Typer(add_completion=False, pretty_exceptions_enable=False, help=__doc__)


@app.command()
def main(
    ids: Annotated[Optional[list[str]], typer.Argument(help="Case IDs. Default: every trajectory case.")] = None,
    config: Annotated[Optional[Path], typer.Option("--config", help="Path to config.yaml (config.local.yaml: the local model on the sample data).")] = None,
    out: Annotated[Optional[Path], typer.Option("--out", help="Folder for the run. Default: <data_dir>/eval/<run id>.")] = None,
    skip_preflight: Annotated[bool, typer.Option("--skip-preflight", help="Do not check first that the model is reachable.")] = False,
) -> None:
    raise typer.Exit(run_suite("trajectory", config=config, ids=ids or [], out=out, skip_preflight=skip_preflight))


if __name__ == "__main__":
    app()
