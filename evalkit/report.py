"""Write an ``EvaluationReport`` to disk as a small set of plain artifacts.

Kept deliberately minimal: a summary JSON (manifest + aggregates, safe to
diff and to attach to a CI run), a full results JSON (every sample, every
metric, with traces), and a flat CSV of per-sample-per-metric rows for
spreadsheet analysis. Swap in your own writer (a database, an S3 client, a
different format) by writing to ``report.to_summary_dict()`` and
``[s.to_dict() for s in report.samples]`` directly -- these are just a
convenient default.
"""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any

from evalkit.runner import EvaluationReport


def _no_nan(obj: Any) -> Any:
    """NaN/inf must never reach JSON -- ``allow_nan=False`` makes that a hard
    error at write time instead of invalid JSON that only fails downstream."""
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        raise ValueError(f"refusing to write non-finite float {obj!r} to a report")
    return obj


def write_summary(report: EvaluationReport, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report.to_summary_dict(), indent=2, ensure_ascii=False, allow_nan=False, default=_no_nan),
        encoding="utf-8",
    )
    return path


def write_results(report: EvaluationReport, path: str | Path, *, include_trace: bool = True) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_manifest": report.run_manifest,
        "samples": [s.to_dict(include_trace=include_trace) for s in report.samples],
    }
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False, default=_no_nan),
        encoding="utf-8",
    )
    return path


def write_csv(report: EvaluationReport, path: str | Path) -> Path:
    """One row per (sample, metric) -- the shape a spreadsheet or a notebook wants."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "sample_id",
        "session_id",
        "pipeline",
        "turn_index",
        "metric",
        "layer",
        "stage",
        "focus",
        "score",
        "verdict",
        "reason",
    ]
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for sample in report.samples:
            for name, result in sample.metrics.items():
                writer.writerow(
                    {
                        "sample_id": sample.sample_id,
                        "session_id": sample.session_id,
                        "pipeline": sample.pipeline,
                        "turn_index": sample.turn_index,
                        "metric": name,
                        "layer": result.layer.value,
                        "stage": result.stage,
                        "focus": result.focus.value,
                        "score": result.score,
                        "verdict": result.verdict.value,
                        "reason": result.reason,
                    }
                )
    return path


def write_all(report: EvaluationReport, output_dir: str | Path, *, run_id: str = "run") -> dict[str, Path]:
    """Write summary + results + CSV into ``output_dir``, named by ``run_id``."""
    output_dir = Path(output_dir)
    return {
        "summary": write_summary(report, output_dir / f"eval_summary_{run_id}.json"),
        "results": write_results(report, output_dir / f"eval_results_{run_id}.json"),
        "csv": write_csv(report, output_dir / f"eval_results_{run_id}.csv"),
    }


def print_gate_line(report: EvaluationReport) -> str:
    """A one-line, CI-friendly rendering of the gate verdict."""
    gate = report.gate
    line = f"GATE {gate.get('verdict', 'N/A')}: {gate.get('reason', '')}"
    return line


__all__ = ["print_gate_line", "write_all", "write_csv", "write_results", "write_summary"]
