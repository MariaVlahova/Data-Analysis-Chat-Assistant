"""Show and store the result of a run: a table for the screen, and `results.json` in the run's folder for the record."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table
from rich.text import Text

from src.scenarios import checks as C
from src.scenarios.runner import RunReport, ScenarioResult

_STYLE = {"pass": "green", "fail": "bold red", "skip": "dim", "warn": "yellow"}


def render(console: Console, report: RunReport) -> None:
    table = Table(title=f"Scenarios: environment {report.environment}, model {report.model}, data {report.data}", show_lines=False)
    for column in ("Scenario", "User", "Result", "Turns", "Passed", "Failed", "Warnings", "Seconds"):
        table.add_column(column, justify="right" if column in ("Turns", "Passed", "Failed", "Warnings", "Seconds") else "left")
    for result in report.scenarios:
        table.add_row(
            result.id, result.user, Text(result.status.upper(), style=_STYLE[result.status]), str(len(result.turns)),
            str(result.count(C.PASS)), str(result.count(C.FAIL)), str(result.count(C.WARN)), f"{result.seconds:.0f}",
        )
    console.print(table)
    for result in report.scenarios:
        _details(console, result)
    totals = {status: sum(1 for r in report.scenarios if r.status == status) for status in ("pass", "fail", "skip")}
    console.print(Text(
        f"{len(report.scenarios)} scenario(s): {totals['pass']} passed, {totals['fail']} failed, {totals['skip']} skipped, in {report.seconds:.0f} s. "
        f"Results: {Path(report.folder) / 'results.json'}", style="bold red" if totals["fail"] else "bold green",
    ))


def _details(console: Console, result: ScenarioResult) -> None:
    """The failed and warned checks of a scenario, and why a scenario was skipped."""
    if result.status == "skip":
        console.print(Text(f"{result.id}: skipped, {result.skip_reason}", style="dim"))
        return
    lines = [(f"turn {turn.index}", check) for turn in result.turns for check in turn.checks if check.status in (C.FAIL, C.WARN)]
    lines += [("scenario", check) for check in result.checks if check.status in (C.FAIL, C.WARN)]
    if not lines:
        return
    console.print(Text(f"{result.id}", style="bold"))
    for where, check in lines:
        console.print(Text(f"  {check.status.upper():4} {where}: {check.name}" + (f" - {check.detail}" if check.detail else ""), style=_STYLE[check.status]))


def to_dict(report: RunReport) -> dict[str, Any]:
    return {
        "run_id": report.run_id, "environment": report.environment, "model": report.model, "data": report.data, "seconds": round(report.seconds, 1),
        "scenarios": [
            {
                "id": r.id, "title": r.title, "user": r.user, "status": r.status, "skip_reason": r.skip_reason, "seconds": round(r.seconds, 1),
                "turns": [
                    {
                        "index": t.index, "say": t.say, "outcome": t.outcome, "trace_id": t.trace_id, "seconds": round(t.seconds, 1), "answer": t.text,
                        "checks": [{"name": c.name, "status": c.status, "detail": c.detail} for c in t.checks],
                    } for t in r.turns
                ],
                "checks": [{"name": c.name, "status": c.status, "detail": c.detail} for c in r.checks],
            } for r in report.scenarios
        ],
    }


def write(report: RunReport) -> Path:
    path = Path(report.folder) / "results.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_dict(report), indent=1, ensure_ascii=False), encoding="utf-8")
    return path
