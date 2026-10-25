"""The golden baseline and the pass bar (plan 09, NFR26, Q20).

The first full run of the golden cases is written to `eval/baseline.json`. Later runs pass when they pass at least as many cases as the baseline minus a
tolerance, counted in cases. The tolerance is not guessed up front: it is chosen after the first run and written in the file next to the numbers it
applies to; the proposal is one case. The hard gates (`eval/metrics.HARD_GATES`) are separate and absolute, and no baseline excuses them.

A baseline belongs to a setup: the environment, the model and the data. A run on the sample data with the local model is compared with the baseline of that
setup, never with the one of Gemini on BigQuery. The file keeps one entry for each setup, so a new setup adds an entry and does not replace another.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from eval.datasets import DATASETS, GOLDEN_FILE
from src.config import Settings
from src.errors import ConfigError

BASELINE_FILE = Path(__file__).resolve().parent / "baseline.json"
DEFAULT_TOLERANCE = 1  # cases: the proposal of plan 09, to be settled by the owner after the first run
FORMAT = 1


def setup_of(settings: Settings) -> dict[str, str]:
    """What a baseline depends on: the environment, the model and the data."""
    backend = settings.llm_backend
    models = settings.llm.models
    return {
        "environment": settings.app.environment,
        "model": backend.model if backend.provider == "openai_compatible" and backend.model else f"{models.main} (light: {models.light})",
        "data": "sample data" if settings.bigquery.backend == "local" else "BigQuery",
    }


def setup_key(setup: dict[str, str]) -> str:
    """The key of a baseline: all three things it depends on, so another model, or the same one on other data, is a setup of its own."""
    return f"{setup['environment']} / {setup['data']} / {setup['model']}"


def cases_hash(path: Path | None = None) -> str:
    """Which version of the golden cases a baseline was made with. A change is a note in the comparison, not an error."""
    return hashlib.sha256((path or DATASETS / GOLDEN_FILE).read_bytes()).hexdigest()[:12]


def read(path: Path = BASELINE_FILE) -> dict[str, Any]:
    """The baseline file. A file that is not there is an empty baseline; one that is there and cannot be read is an error, never an empty baseline: the next write
    would replace every other setup's entry with it."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"format": FORMAT, "setups": {}}
    except OSError as exc:
        raise ConfigError(f"The baseline file could not be read: {path} ({exc.strerror or exc}).") from None
    try:
        data = json.loads(text)
    except ValueError:
        raise ConfigError(
            f"{path} is not valid JSON, so no baseline is read from it or written over it.", fix="Fix the file (git can show what it was), or delete it to start again.",
        ) from None
    if not isinstance(data, dict) or not isinstance(data.get("setups"), dict):
        raise ConfigError(f"{path} does not have the shape of a baseline file (a `setups` mapping).", fix="Fix the file, or delete it to start again.")
    return data


def entry_for(setup: dict[str, str], path: Path = BASELINE_FILE) -> dict[str, Any] | None:
    return read(path)["setups"].get(setup_key(setup))


def record(
    path: Path, setup: dict[str, str], cases: dict[str, bool], *, run_id: str, tolerance: int = DEFAULT_TOLERANCE, cases_file: Path | None = None,
    cases_hash_of_run: str | None = None,
) -> dict[str, Any]:
    """Write (or replace) the entry of this setup. `cases` is each golden case's pass or fail in the run that becomes the baseline. `cases_hash_of_run` is the
    version of the cases that run used, when the run is older than this call."""
    data = read(path)
    passed = sum(cases.values())
    entry = {
        "setup": setup, "run_id": run_id, "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cases_hash": cases_hash_of_run or cases_hash(cases_file), "total": len(cases), "passed": passed, "score": round(passed / len(cases), 4) if cases else 0.0,
        "tolerance": tolerance, "cases": dict(sorted(cases.items())),
    }
    data["format"] = FORMAT
    data["setups"][setup_key(setup)] = entry
    temporary = path.with_name(path.name + ".tmp")  # written whole, then put in place: a crash in the middle leaves the old file
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return entry


def refuse_to_lower(path: Path, setup: dict[str, str], cases: dict[str, bool]) -> None:
    """A run that passes fewer cases than the baseline of its setup does not become the baseline, even inside the tolerance: the bar would slide down one
    case at a time. Lowering it on purpose is a visible edit of the file. Raises `ConfigError`."""
    entry = entry_for(setup, path)
    if entry is None:
        return
    comparison = compare(entry, cases)
    if comparison.ok is not None and comparison.passed < comparison.baseline_passed:
        raise ConfigError(
            f"The baseline was not written: this run passed {comparison.passed} of the cases the baseline passed {comparison.baseline_passed} of.",
            fix=f"To lower the bar on purpose, delete the entry of this setup from {path} (it is under version control) and write it again.",
        )


def record_from_run(path: Path, run_dir: Path, *, tolerance: int, expected: set[str]) -> dict[str, Any]:
    """Make the baseline of a setup from a golden run that was already made and read (`verdicts.json` of its folder), instead of running everything again.

    The same refusals as writing one at the end of a run: a baseline is the bar later runs are held to, so it is not made from a run that failed a hard gate,
    that skipped a case, or that did not cover every case there is. Raises `ConfigError` with the reason."""
    try:
        run = json.loads((run_dir / "verdicts.json").read_text(encoding="utf-8"))
        cases = {case["id"]: bool(case["passed"]) for case in run["cases"]}
    except (OSError, ValueError, KeyError, TypeError):
        raise ConfigError(f"{run_dir} is not the folder of a golden run (no readable verdicts.json).", fix="Pass the folder a run wrote: <data_dir>/eval/golden-<time>.") from None
    if run.get("kind") != "golden":
        raise ConfigError(f"{run_dir} is a {run.get('kind')} run, and a baseline is made from a golden run.")
    if not run.get("hard_gates_ok"):
        raise ConfigError("The baseline was not written: a hard gate failed in that run.", fix="Fix that and run again.")
    if run.get("skipped"):
        raise ConfigError(f"The baseline was not written: {len(run['skipped'])} case(s) were skipped in that run, so it is partial.", fix="Run the whole set.")
    if run.get("unplayed"):
        raise ConfigError(f"The baseline was not written: the chat did not start for {len(run['unplayed'])} case(s) in that run.", fix="Fix that and run the whole set.")
    if set(cases) != expected:
        raise ConfigError(f"The baseline was not written: that run has {len(cases)} of the {len(expected)} cases there are.", fix="Run the whole set.")
    refuse_to_lower(path, run["setup"], cases)
    return record(path, run["setup"], cases, run_id=run["run_id"], tolerance=tolerance, cases_hash_of_run=run.get("cases_hash") or "unknown")


@dataclass
class Comparison:
    """A run against its baseline. `ok` is None when there is nothing to compare with (no baseline for this setup)."""

    ok: bool | None
    summary: str
    passed: int = 0
    baseline_passed: int = 0
    tolerance: int = 0
    regressions: list[str] = field(default_factory=list)  # passed in the baseline, fail now
    fixed: list[str] = field(default_factory=list)  # failed in the baseline, pass now
    new: list[str] = field(default_factory=list)  # not in the baseline
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dict(vars(self))


def compare(entry: dict[str, Any] | None, cases: dict[str, bool], *, cases_file: Path | None = None) -> Comparison:
    """The run passes when, over the cases both have, it passes at least `baseline passed - tolerance` of them.

    The tolerance is counted over the whole set, so it applies only to a run of the whole set. A run of some of the cases gets none: every case of it that the
    baseline passed must pass, or one named case that regressed would always be inside a tolerance of one."""
    if entry is None:
        return Comparison(None, "no baseline for this setup yet: run with --update-baseline once, and choose the tolerance after reading the result")
    base: dict[str, bool] = entry.get("cases", {})
    tolerance = int(entry.get("tolerance", DEFAULT_TOLERANCE))
    common = sorted(set(base) & set(cases))
    if not common:  # a bar over no case is no bar: it would be met by anything
        return Comparison(None, "no case of this run is in the baseline of this setup, so there is nothing to compare with", new=sorted(cases))
    passed, baseline_passed = sum(cases[name] for name in common), sum(base[name] for name in common)
    notes = []
    if entry.get("cases_hash") != cases_hash(cases_file):
        notes.append("the golden cases changed since the baseline was made (or the run it was made from did not record their version)")
    gone = sorted(set(base) - set(cases))
    if gone:
        notes.append(f"{len(gone)} baseline case(s) were not run: {', '.join(gone)}")
        if tolerance:
            notes.append(f"the tolerance of {tolerance} is for the whole set, so this partial run is held to no tolerance")
            tolerance = 0
    ok = passed >= baseline_passed - tolerance
    summary = (
        f"{passed} of {len(common)} cases passed; the baseline passed {baseline_passed} with a tolerance of {tolerance}, so the bar is {max(baseline_passed - tolerance, 0)}: "
        + ("met" if ok else "NOT met")
    )
    return Comparison(
        ok, summary, passed, baseline_passed, tolerance,
        regressions=[name for name in common if base[name] and not cases[name]], fixed=[name for name in common if not base[name] and cases[name]],
        new=sorted(set(cases) - set(base)), notes=notes,
    )
