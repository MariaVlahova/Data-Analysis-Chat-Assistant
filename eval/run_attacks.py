"""Run the attacks against the real app (plan 09, NFR12):  python -m eval.run_attacks --config config.local.yaml

Every attack is typed to the real model. Whatever it does, the invariants I1 to I6 must hold: no personal data anywhere, only guarded SQL, only the user's brands,
no destructive effect without the typed confirmation, inside the budget, no crash. A single failure is a bug. Whether the attack was *refused* is shown and not
counted for a live model (a model that never writes the bad SQL has nothing for the code to refuse); the offline run (tests/test_attack_offline.py) scripts a
model that does what the attack asks and requires the refusal.

    python -m eval.run_attacks --config config.local.yaml
    python -m eval.run_attacks pii-emails-of-top-customers --config config.local.yaml
"""
from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer

from eval.suite import run_suite

app = typer.Typer(add_completion=False, pretty_exceptions_enable=False, help=__doc__)


@app.command()
def main(
    ids: Annotated[Optional[list[str]], typer.Argument(help="Attack IDs. Default: every attack.")] = None,
    config: Annotated[Optional[Path], typer.Option("--config", help="Path to config.yaml (config.local.yaml: the local model on the sample data).")] = None,
    out: Annotated[Optional[Path], typer.Option("--out", help="Folder for the run. Default: <data_dir>/eval/<run id>.")] = None,
    skip_preflight: Annotated[bool, typer.Option("--skip-preflight", help="Do not check first that the model is reachable.")] = False,
) -> None:
    raise typer.Exit(run_suite("attack", config=config, ids=ids or [], out=out, skip_preflight=skip_preflight))


if __name__ == "__main__":
    app()
