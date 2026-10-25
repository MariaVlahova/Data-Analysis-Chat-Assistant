"""Run one suite (golden, attacks or chaos) against the app the configuration describes, score it, write it, and say how it went (plan 09).

Shared by `run_golden.py`, `run_attacks.py` and `run_chaos.py`. The model and the data are those of the config, as for `chat`: `--config config.local.yaml` is the
local model on the sample data. A turn takes minutes with a 30B model on a laptop, so a run is slow and is meant to be started and left.

Exit codes: 0 the run passed, 1 it did not (a failed hard gate; an attack or chaos case that failed; a golden run below its baseline bar), 2 it could not start (a bad
config, dataset or baseline file, no model). A golden run with no baseline yet has no bar to miss: it exits 0 and says there is none.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

import typer
from rich.text import Text

from eval import baseline as B
from eval.datasets import ChaosScenario, GoldenCase, load_attacks, load_chaos, load_golden, select
from eval.evaluate import ADVISORY_LIVE, evaluate, new_run_id, render, write_run
from eval.harness import Harness, Sample
from src.cli.render import Renderer
from src.cli.scenario_cmd import _preflight as preflight
from src.config import Settings, load_settings
from src.errors import ConfigError
from src.scenarios.runner import unmet_requirement

# A live chaos run reports how the outcomes came out and does not gate on them (plan 09): a model's luck with injected faults is not a defect.
ADVISORY_LIVE_CHAOS = frozenset({"chaos_expectations"})
LIVE_CHAOS_TURNS = 5  # "5 questions with --chaos mild"

def start(config: Path | None, *, skip_preflight: bool, renderer: Renderer) -> Settings:
    """The settings, and a check that the model can be reached. A problem here is exit code 2, with the fix."""
    try:
        settings = load_settings(config)
        if not skip_preflight:
            preflight(settings)
    except ConfigError as exc:
        renderer.startup_problem(exc.message, exc.fix)
        raise typer.Exit(2) from None
    return settings


def run_suite(
    kind: str, *, config: Path | None, ids: list[str], out: Path | None, skip_preflight: bool = False, update_baseline: bool = False,
    tolerance: int | None = None, baseline_path: Path = B.BASELINE_FILE, turns: int | None = None,
) -> int:
    renderer = Renderer()
    say: Callable[[str], None] = lambda text: renderer.console.print(Text(text))  # noqa: E731
    settings = start(config, skip_preflight=skip_preflight, renderer=renderer)
    try:
        items = {"golden": load_golden, "attack": load_attacks, "chaos": load_chaos}[kind]()
        chosen = select(items, ids)
    except ConfigError as exc:
        renderer.startup_problem(exc.message, exc.fix)
        return 2
    if update_baseline and (kind != "golden" or ids):
        renderer.startup_problem("--update-baseline needs the whole golden set.", "Run without IDs, with `python -m eval.run_golden --update-baseline`.")
        return 2
    if kind == "golden":
        try:
            B.read(baseline_path)  # a baseline file that cannot be read stops the run now, not after half an hour of it
        except ConfigError as exc:
            renderer.startup_problem(exc.message, exc.fix)
            return 2

    run_id = new_run_id(kind)
    folder = out or settings.data_path / "eval" / run_id
    if folder.is_dir() and any(folder.iterdir()):  # a conversation's folder is reused, so an old run's metrics and traces would be counted in this one
        renderer.startup_problem(f"The folder {folder} already has files in it.", "Give --out a new folder, or leave it out to get one named by the run.")
        return 2
    setup = B.setup_of(settings)
    renderer.info(f"Run {run_id}: {len(chosen)} {kind} item(s), {B.setup_key(setup)}, model {setup['model']}. Results in {folder}")
    try:
        harness = Harness(settings, folder, say=say)
    except ConfigError as exc:
        renderer.startup_problem(exc.message, exc.fix)
        return 2
    started = time.perf_counter()
    samples: list[Sample] = []
    skipped: list[str] = []
    try:
        for number, item in enumerate(chosen, 1):
            why = unmet_requirement(item, settings) if isinstance(item, GoldenCase) else ""
            if not why and isinstance(item, ChaosScenario) and not item.chaos:
                why = "scripted faults can only be injected offline (tests/test_chaos_offline.py); a live run plays the random profiles"
            if why:
                say(f"[{number}/{len(chosen)}] {item.id}: skipped, {why}")
                skipped.append(item.id)
                continue
            say(f"[{number}/{len(chosen)}] {item.id}")
            samples.append(play(harness, kind, item, turns))
    finally:
        harness.close()
    if not samples:
        renderer.startup_problem("Nothing was run.", "Every item was skipped for this setup.")
        return 2

    advisory = {"golden": frozenset(), "attack": ADVISORY_LIVE, "chaos": ADVISORY_LIVE_CHAOS}[kind]
    evaluation = evaluate(kind, samples, setup, run_id=run_id, advisory=advisory, live=True)
    if kind == "golden":
        cases = {verdict.id: verdict.passed for verdict in evaluation.verdicts}
        evaluation.comparison = B.compare(B.entry_for(setup, baseline_path), cases)
    paths = write_run(evaluation, folder, run_id=run_id, skipped=skipped)
    render(renderer.console, evaluation)
    renderer.info(f"{time.perf_counter() - started:.0f} s. Verdicts: {paths['verdicts']}")
    if update_baseline:
        # A baseline is the bar later runs are held to, so it is not made from a run that leaked, or from part of the set.
        if not evaluation.hard_gates_ok or skipped or evaluation.unplayed:
            why = (
                "a hard gate failed in this run." if not evaluation.hard_gates_ok
                else f"{len(skipped)} case(s) were skipped, so the run is partial." if skipped
                else f"the chat did not start for {len(evaluation.unplayed)} case(s), so the run is partial."
            )
            renderer.startup_problem("The baseline was not written: " + why, "Fix that and run again with --update-baseline.")
            return 1
        cases = {v.id: v.passed for v in evaluation.verdicts}
        try:
            B.refuse_to_lower(baseline_path, setup, cases)
        except ConfigError as exc:
            renderer.startup_problem(exc.message, exc.fix)
            return 1
        entry = B.record(baseline_path, setup, cases, run_id=run_id, tolerance=B.DEFAULT_TOLERANCE if tolerance is None else tolerance)
        renderer.info(f"Baseline written to {baseline_path}: {entry['passed']} of {entry['total']} cases, tolerance {entry['tolerance']} case(s). Read the result, then set the tolerance.")
    return 0 if evaluation.ok else 1


def baseline_from_run(run_dir: Path, *, baseline_path: Path, tolerance: int) -> int:
    """`run_golden --baseline-from DIR`: the baseline of the run's setup, from a run that was already made and read. Exit code 0 written, 2 refused."""
    renderer = Renderer()
    try:
        entry = B.record_from_run(baseline_path, run_dir, tolerance=tolerance, expected={case.id for case in load_golden()})
    except ConfigError as exc:
        renderer.startup_problem(exc.message, exc.fix)
        return 2
    renderer.info(
        f"Baseline written to {baseline_path} for {B.setup_key(entry['setup'])}: {entry['passed']} of {entry['total']} cases, tolerance {entry['tolerance']} case(s), "
        f"from run {entry['run_id']}."
    )
    return 0


def play(harness: Harness, kind: str, item: Any, turns: int | None) -> Sample:
    if kind == "golden":
        return harness.play_golden(item)
    if kind == "attack":
        return harness.play_attack(item)
    if turns is not None:
        item = item.model_copy(update={"turns": min(item.turns, turns)})
    return harness.play_chaos(item)
