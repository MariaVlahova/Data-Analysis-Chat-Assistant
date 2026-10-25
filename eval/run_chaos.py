"""Run the chaos scenarios against the real app (plan 09, F37, NFR19):  python -m eval.run_chaos --config config.local.yaml

A live run plays the random profiles (`chat --chaos mild|harsh`, with the seed of the scenario, so a run can be replayed) for a few turns each and reports how
they came out. Crashes, leaks and a missing trace ID on a failed turn are still failures; how many turns ended answered is a finding and not a gate. The scripted
faults (a 429 storm, 5xx on every model, a BigQuery timeout, bad SQL, malformed JSON) can only be aimed offline: tests/test_chaos_offline.py runs them.

    python -m eval.run_chaos --config config.local.yaml                 # 5 turns of each random profile
    python -m eval.run_chaos random-mild-10-turns --turns 5 --config config.local.yaml
"""
from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer

from eval.suite import LIVE_CHAOS_TURNS, run_suite

app = typer.Typer(add_completion=False, pretty_exceptions_enable=False, help=__doc__)


@app.command()
def main(
    ids: Annotated[Optional[list[str]], typer.Argument(help="Scenario IDs. Default: the random ones.")] = None,
    config: Annotated[Optional[Path], typer.Option("--config", help="Path to config.yaml (config.local.yaml: the local model on the sample data).")] = None,
    out: Annotated[Optional[Path], typer.Option("--out", help="Folder for the run. Default: <data_dir>/eval/<run id>.")] = None,
    turns: Annotated[int, typer.Option("--turns", min=1, help="At most this many turns of each scenario.")] = LIVE_CHAOS_TURNS,
    skip_preflight: Annotated[bool, typer.Option("--skip-preflight", help="Do not check first that the model is reachable.")] = False,
) -> None:
    raise typer.Exit(run_suite("chaos", config=config, ids=ids or [], out=out, skip_preflight=skip_preflight, turns=turns))


if __name__ == "__main__":
    app()
