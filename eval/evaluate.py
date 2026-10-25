"""Score played samples with `evalkit`, apply the hard gates, and write the run (plan 09).

`evaluate()` turns each `Sample` into an `EvalSession`, runs the free layers (`eval/metrics.py`) and gets back an `evalkit` report. A sample passes when every
metric that decided something passed, except the advisory ones the caller names. The hard gates are `evalkit` floors of 1.0: one failure of a gated metric
fails the run, and no baseline excuses it. A gate that no sample could measure is reported as unmeasured, never as clean.

`write_run()` puts the run in a folder: `summary.json` and `results.json` and `results.csv` (`evalkit`), `verdicts.json` (each case's verdict, the gate and the
comparison with the baseline, which `eval/report.py` reads), a transcript of each conversation, and `evidence/<id>.json` (what a judge model may read of it, which
`eval/run_judge.py` judges later). A conversation in which personal data was found is not written out: the transcript, the evidence and the results say it was
withheld.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from rich.console import Console
from rich.table import Table
from rich.text import Text

from eval import baseline as B
from eval.datasets import GoldenCase
from eval.harness import Sample
from eval.metrics import COMPUTATION, HARD_GATES, STATIC
from evalkit.config import EvaluationConfig
from evalkit.report import write_csv, write_results, write_summary
from evalkit.runner import EvaluationReport, EvaluationRunner
from evalkit.schema import Layer, SampleResult, Verdict
from evalkit.session import EvalEvent, EvalSession
from src.scenarios import checks as C

WITHHELD = "[withheld: personal data was found in this turn]"
# Metrics that are findings to read and not gates, for a run with a real model: a model that never writes the bad SQL has nothing for the code to refuse.
ADVISORY_LIVE = frozenset({"attack_refused"})


@dataclass
class CaseVerdict:
    id: str
    kind: str
    user: str
    passed: bool
    turns: int
    seconds: float
    failed: dict[str, str] = field(default_factory=dict)  # metric -> why
    advisory: dict[str, str] = field(default_factory=dict)  # failed advisory metrics: shown, never counted
    undecided: list[str] = field(default_factory=list)  # metrics that were N/A for this case
    score: float = 0.0  # the share of the decided metrics that passed

    def to_dict(self) -> dict[str, Any]:
        return dict(vars(self))


@dataclass
class Evaluation:
    kind: str
    report: EvaluationReport
    verdicts: list[CaseVerdict]
    samples: list[Sample]
    setup: dict[str, str]
    advisory: frozenset[str] = frozenset()
    comparison: B.Comparison | None = None

    @property
    def gate(self) -> dict[str, Any]:
        return self.report.gate

    @property
    def passed_cases(self) -> int:
        return sum(1 for verdict in self.verdicts if verdict.passed)

    @property
    def hard_gates_ok(self) -> bool:
        return self.gate.get("verdict") != "FAIL"

    @property
    def unplayed(self) -> list[str]:
        """The samples whose chat never started (an unknown user, a startup check). Nothing of them was measured, so the run is incomplete."""
        return [sample.id for sample in self.samples if sample.played is None]

    @property
    def ok(self) -> bool:
        """The run passes. The hard gates held, every sample was played, and then: a golden run met its baseline bar where there is a baseline (the first run of
        a setup has no bar to miss); an attack or chaos run passed every one of its cases (NFR12: a single failure is a bug, not a flaky test)."""
        return (
            self.hard_gates_ok and not self.unplayed and self.comparison_ok
            and (self.kind == "golden" or all(verdict.passed for verdict in self.verdicts))
        )

    @property
    def comparison_ok(self) -> bool:
        return self.comparison is None or self.comparison.ok is not False


def gates_for(kind: str) -> tuple[str, ...]:
    """The hard-gate metrics that this kind of run can measure."""
    applicable = {*STATIC.applicable(kind), *COMPUTATION.applicable(kind)}
    return tuple(name for name in HARD_GATES if name in applicable)


def session_for(sample: Sample) -> EvalSession:
    """The conversation as `evalkit` reads it: what the user typed and what they saw. A turn with personal data in it is not copied."""
    events: list[EvalEvent] = []
    leaked_files = sample.played is not None and sample.played.files_check.status == C.FAIL
    last = ""
    for index, ev in enumerate(sample.evidence):
        leaked = leaked_files or sample.invariants(index)["I1"].status == C.FAIL
        last = WITHHELD if leaked else ev.text
        events.append(EvalEvent(type="user", author=sample.user.user_id, content=ev.say))
        events.append(EvalEvent(type="model", author="agent", content=last))
    return EvalSession(
        session_id=sample.id, pipeline=sample.kind, user_id=sample.user.user_id, events=events, state={"final_answer": last},
    )


NODE_SPANS = (
    "input_guard", "context_assembly", "planner", "sql_loop", "evidence_check", "composer", "tool_router", "prepare_action", "hitl_gate", "execute_action",
    "compose_failure",
)


def evidence_of(sample: Sample) -> dict[str, Any]:
    """What a judge model may read of a played conversation, for `eval/judging.py` (it judges a finished run from these files and replays nothing).

    The words of the user and of the app, the path through the graph, the SQL of the queries (as the trace stores it, masked), how many rows each returned,
    the confirmation events, and for a golden turn the reference figures. Never a result row. A turn in which personal data was found is not copied (as in
    `session_for`): its text is withheld and so is its SQL, and the judge scores nothing of it.
    """
    facts = sample.facts
    leaked_files = sample.played is not None and sample.played.files_check.status == C.FAIL
    case = sample.spec if isinstance(sample.spec, GoldenCase) else None
    turns: list[dict[str, Any]] = []
    for index, ev in enumerate(sample.evidence):
        leaked = leaked_files or sample.invariants(index)["I1"].status == C.FAIL
        spec = case.turns[index] if case is not None and index < len(case.turns) else None
        ref = sample.references[index] if index < len(sample.references) else None
        queries = [] if leaked else [
            {"sql": str(attrs.get("sql", "")), "rows": attrs.get("rows"), "status": span.get("status")}
            for span in ev.spans if span.get("name") == "bq.query" for attrs in [span.get("attrs") or {}]
        ]
        confirmations = [
            {"event": item["name"].split(".", 1)[1], "level": (item.get("attrs") or {}).get("level"), "count": (item.get("attrs") or {}).get("count")}
            for span in ev.spans for item in span.get("events") or [] if str(item.get("name", "")).startswith("hitl.")
        ]
        turns.append({
            "say": ev.say, "outcome": ev.outcome, "turn_kind": ev.row.get("turn_kind"), "withheld": leaked, "text": WITHHELD if leaked else ev.text,
            "seconds": round(ev.seconds, 1), "path": [name for name in dict.fromkeys(str(span.get("name")) for span in ev.spans) if name in NODE_SPANS],
            "queries": queries, "confirmations": confirmations, "repairs": int(ev.row.get("repairs") or 0), "replans": int(ev.row.get("replans") or 0),
            "subquestions": int(ev.row.get("subquestions") or 0), "llm_calls": int(ev.row.get("llm_calls") or 0),
            "reference": {"column": spec.compare.column, "values": ref.values} if spec is not None and spec.compare is not None and ref is not None and ref.values else None,
            # what the entry promised, so a rubric that did not fire is "not exercised" and not "does not apply" (never shown to the judge)
            "expects": {"figures": bool(spec and spec.compare), "confirmation": bool(spec and "awaiting_confirmation" in spec.outcome)},
        })
    return {
        "id": sample.id, "kind": sample.kind, "title": getattr(sample.spec, "title", sample.id), "user": sample.user.user_id,
        "scope_mode": sample.user.scope_mode, "brands": list(sample.user.brands),
        "other_brands": list(facts.other_brands) if facts is not None else [], "turns": turns,
    }


def evaluate(
    kind: str, samples: list[Sample], setup: dict[str, str], *, run_id: str, advisory: frozenset[str] = frozenset(), live: bool = True,
) -> Evaluation:
    gates = gates_for(kind)
    config = EvaluationConfig(
        layers=frozenset({Layer.STATIC, Layer.COMPUTATION}), mode="live" if live else "offline", gate_metrics=gates, gate_threshold=1.0,
        gate_floors={name: 1.0 for name in gates}, bootstrap_resamples=0,
    )
    runner = EvaluationRunner(
        config=config, static_metrics=STATIC, computation_metrics=COMPUTATION, extras={"samples": {sample.id: sample for sample in samples}},
        manifest_extra={"suite": kind, **setup},
    )
    report = runner.run([session_for(sample) for sample in samples], run_id=run_id)
    by_id = {result.session_id: result for result in report.samples}
    verdicts = [verdict_of(sample, by_id[sample.id], advisory) for sample in samples]
    return Evaluation(kind, report, verdicts, samples, setup, advisory)


def verdict_of(sample: Sample, result: SampleResult, advisory: frozenset[str]) -> CaseVerdict:
    decided = {name: metric for name, metric in result.metrics.items() if metric.decided}
    failed = {name: metric.reason for name, metric in decided.items() if metric.verdict is Verdict.FAIL and name not in advisory}
    shown = {name: metric.reason for name, metric in decided.items() if metric.verdict is Verdict.FAIL and name in advisory}
    counted = [metric for name, metric in decided.items() if name not in advisory]
    if sample.played is None and not failed:  # every metric was N/A: the case still fails, and says why
        failed = {"start the chat": sample.error or "the conversation could not be played"}
    return CaseVerdict(
        id=sample.id, kind=sample.kind, user=sample.user.user_id, passed=not failed and bool(counted), turns=len(sample.evidence), seconds=round(sample.seconds, 1),
        failed=failed, advisory=shown, undecided=sorted(name for name, metric in result.metrics.items() if not metric.decided),
        score=round(sum(1 for metric in counted if metric.verdict is Verdict.PASS) / len(counted), 4) if counted else 0.0,
    )


# the run on disk -------------------------------------------------------------------------------------------------------------------------------

def new_run_id(kind: str) -> str:
    return f"{kind}-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"


def write_run(evaluation: Evaluation, folder: Path, *, run_id: str, skipped: Sequence[str] = ()) -> dict[str, Path]:
    """The files of a run. Nothing is written for a conversation with personal data in it except that it was withheld."""
    folder.mkdir(parents=True, exist_ok=True)
    paths = {
        "summary": write_summary(evaluation.report, folder / "summary.json"),
        "results": write_results(evaluation.report, folder / "results.json", include_trace=False),
        "csv": write_csv(evaluation.report, folder / "results.csv"),
    }
    payload = {
        "run_id": run_id, "kind": evaluation.kind, "setup": evaluation.setup, "passed": evaluation.passed_cases, "total": len(evaluation.verdicts),
        "gate": evaluation.gate, "hard_gates_ok": evaluation.hard_gates_ok, "advisory_metrics": sorted(evaluation.advisory),
        "baseline": evaluation.comparison.to_dict() if evaluation.comparison else None, "skipped": list(skipped), "unplayed": evaluation.unplayed,
        "cases_hash": B.cases_hash() if evaluation.kind == "golden" else None, "cases": [verdict.to_dict() for verdict in evaluation.verdicts],
    }
    paths["verdicts"] = folder / "verdicts.json"
    paths["verdicts"].write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    transcripts = folder / "transcripts"
    transcripts.mkdir(exist_ok=True)
    evidence = folder / "evidence"
    evidence.mkdir(exist_ok=True)
    for sample in evaluation.samples:
        (transcripts / f"{sample.id}.md").write_text(transcript(sample), encoding="utf-8")
        if sample.played is not None:  # a chat that never started has no conversation for a judge
            (evidence / f"{sample.id}.json").write_text(json.dumps(evidence_of(sample), indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    return paths


def transcript(sample: Sample) -> str:
    title = getattr(sample.spec, "title", sample.id)
    lines = [f"# {sample.id}: {title}", "", f"As `{sample.user.user_id}`. {sample.seconds:.0f} s.", ""]
    if sample.played is None:
        return "\n".join([*lines, f"The conversation could not be played: {sample.error}", ""])
    leaked_files = sample.played.files_check.status == C.FAIL
    for index, ev in enumerate(sample.evidence):
        withheld = leaked_files or sample.invariants(index)["I1"].status == C.FAIL
        lines += [f"## Turn {ev.index}: {ev.outcome}, {ev.seconds:.0f} s, trace {ev.trace_id}", "", f"> {ev.say}", "", WITHHELD if withheld else ev.text, ""]
    return "\n".join(lines)


# the run on screen -----------------------------------------------------------------------------------------------------------------------------

_STYLE = {True: "green", False: "bold red"}


def render(console: Console, evaluation: Evaluation) -> None:
    table = Table(title=f"{evaluation.kind}: {B.setup_key(evaluation.setup)}, model {evaluation.setup['model']}")
    for column in ("Case", "User", "Result", "Score", "Seconds", "What failed"):
        table.add_column(column, justify="right" if column in ("Score", "Seconds") else "left")
    for verdict in evaluation.verdicts:
        why = "; ".join(f"{name}: {reason}" for name, reason in verdict.failed.items())
        table.add_row(
            verdict.id, verdict.user, Text("PASS" if verdict.passed else "FAIL", style=_STYLE[verdict.passed]), f"{verdict.score:.2f}", f"{verdict.seconds:.0f}",
            why[:160] + ("..." if len(why) > 160 else ""),
        )
    console.print(table)
    for verdict in evaluation.verdicts:
        for name, reason in verdict.advisory.items():
            console.print(Text(f"{verdict.id}: advisory {name}: {reason}", style="yellow"))
    gate = evaluation.gate
    console.print(Text(f"Hard gates {gate.get('verdict', 'N/A')}: {gate.get('reason', '')}", style=_STYLE[evaluation.hard_gates_ok]))
    if evaluation.comparison is not None:
        comparison = evaluation.comparison
        style = "yellow" if comparison.ok is None else _STYLE[comparison.ok]
        console.print(Text(f"Baseline: {comparison.summary}", style=style))
        if comparison.regressions:
            console.print(Text("Regressions: " + ", ".join(comparison.regressions), style="bold red"))
        for note in comparison.notes:
            console.print(Text(f"Note: {note}", style="yellow"))
    if evaluation.unplayed:
        console.print(Text("The chat did not start for: " + ", ".join(evaluation.unplayed) + ". The run is incomplete.", style="bold red"))
    console.print(Text(f"{evaluation.passed_cases} of {len(evaluation.verdicts)} case(s) passed.", style=_STYLE[evaluation.ok]))
