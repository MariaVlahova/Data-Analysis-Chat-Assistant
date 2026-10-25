"""Score the golden cases against the real app (plan 09):  python -m eval.run_golden --config config.local.yaml

Each case is played as a conversation; its answers are checked against the figures of its reference SQL (run through the same SQL Guard as the same user),
against what the answer must say, and against the invariants I1 to I6. The run passes when the hard gates hold and the cases pass at least the baseline's
number minus its tolerance. The first run of a setup has no baseline: read it, then write it with `--update-baseline` and choose the tolerance.

    python -m eval.run_golden --config config.local.yaml                       # every case
    python -m eval.run_golden rev-monthly-alice scope-total-ceo --config ...   # some cases (not for a baseline)
    python -m eval.run_golden --config config.local.yaml --update-baseline --tolerance 1      # run everything, then write the baseline
    python -m eval.run_golden --baseline-from data/local/eval/golden-<time> --tolerance 1      # or write it from a run you have already read
"""
from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer

from eval import baseline as B
from eval.suite import baseline_from_run, run_suite
from src.cli.render import Renderer

app = typer.Typer(add_completion=False, pretty_exceptions_enable=False, help=__doc__)


@app.command()
def main(
    ids: Annotated[Optional[list[str]], typer.Argument(help="Case IDs. Default: every case.")] = None,
    config: Annotated[Optional[Path], typer.Option("--config", help="Path to config.yaml (config.local.yaml: the local model on the sample data).")] = None,
    out: Annotated[Optional[Path], typer.Option("--out", help="Folder for the run. Default: <data_dir>/eval/<run id>.")] = None,
    skip_preflight: Annotated[bool, typer.Option("--skip-preflight", help="Do not check first that the model is reachable.")] = False,
    update_baseline: Annotated[bool, typer.Option("--update-baseline", help="Write this run as the baseline of this setup (the whole set only).")] = False,
    tolerance: Annotated[Optional[int], typer.Option(
        "--tolerance", min=0, help=f"With --update-baseline or --baseline-from: how many cases fewer than the baseline still pass (default {B.DEFAULT_TOLERANCE}).",
    )] = None,
    baseline: Annotated[Path, typer.Option("--baseline", help="The baseline file.")] = B.BASELINE_FILE,
    baseline_from: Annotated[Optional[Path], typer.Option("--baseline-from", help="Write the baseline from this finished golden run (its folder), and run nothing.")] = None,
) -> None:
    if tolerance is not None and not update_baseline and baseline_from is None:
        # a run is held to the tolerance written in the baseline file: one given here would be silently ignored
        Renderer().startup_problem(
            "--tolerance only sets the tolerance of a baseline being written.", "Add --update-baseline or --baseline-from, or edit the tolerance in the baseline file.",
        )
        raise typer.Exit(2)
    if baseline_from is not None:
        raise typer.Exit(baseline_from_run(baseline_from, baseline_path=baseline, tolerance=B.DEFAULT_TOLERANCE if tolerance is None else tolerance))
    raise typer.Exit(run_suite(
        "golden", config=config, ids=ids or [], out=out, skip_preflight=skip_preflight, update_baseline=update_baseline, tolerance=tolerance, baseline_path=baseline,
    ))


if __name__ == "__main__":
    app()
