"""Judge a finished run with the rubrics of `evalkit/rubrics.xlsx` (plan 09).

The free checks (`eval/metrics.py`) decide the hard gates. This is the other half: a model reads each turn of a finished run and scores it against a rubric
(tool use, answer quality, groundedness, safety, completeness, confirmation flow) on a 1 to 4 scale. It reads the files `write_run` left in the run's folder
(`evidence/<id>.json`) and replays nothing, so a run that took hours is judged in a second pass, and again by another judge.

The judge itself (`GatewayJudge`, the sample, the applicability rules) lives in `src/evaluation/judging.py`, because the chat's `/eval` judges real turns with the
same code. What stays here is reading a run's folder and writing the judge's files.

- **Advisory.** A judge score never changes a run's exit code, a baseline or a hard gate (see `src/evaluation/judging.py`).
- **Never its own model.** A judge whose model is the run's is refused unless `allow_self_judge` is set, and then the result says it is not independent.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from evalkit.config import EvaluationConfig
from evalkit.judge import JudgeClient
from evalkit.report import write_csv, write_results, write_summary
from evalkit.rubrics import Rubric, load_rubrics
from evalkit.runner import EvaluationReport, EvaluationRunner
from evalkit.schema import Verdict
from src.config import Settings
from src.errors import ConfigError
from src.evaluation.judging import (  # noqa: F401 - the judge moved to src/evaluation; these names stay importable from here
    ANSWERED, APPLICABILITY, CALLS_PER_VERDICT, CONFIRMATION_LEVELS, CONFIRMATION_OUTCOMES, JUDGE_LAYERS, JUDGE_SYSTEM, RUBRICS_FILE, TURN_STATE_KEYS, GatewayJudge,
    JudgeError, answer_completeness, final_response_quality, groundedness, hitl_appropriateness, is_self_judged, safety, sample_block, session_from_evidence,
    tool_use_quality,
)
from src.llm.gateway import Gateway
from src.llm.routing import Step

JUDGE_DIR = "judge"


def read_run(folder: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """(verdicts, the evidence of each conversation) of a finished run. A run made before the judge existed has no `evidence/`: it must be run again."""
    try:
        verdicts = json.loads((folder / "verdicts.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{folder} is not a run folder of the evaluation ({type(exc).__name__}).", fix="Pass a folder that holds verdicts.json.") from None
    files = sorted((folder / "evidence").glob("*.json")) if (folder / "evidence").is_dir() else []
    if not files:
        raise ConfigError(
            f"{folder} has no evidence/ folder, so there is nothing for a judge to read.",
            fix="Runs made before the judge was wired do not have it. Run the suite again (python -m eval.run_golden, run_attacks or run_chaos).",
        )
    try:
        return verdicts, [json.loads(path.read_text(encoding="utf-8")) for path in files]
    except ValueError as exc:
        raise ConfigError(f"An evidence file in {folder / 'evidence'} is not valid JSON ({type(exc).__name__}).", fix="Run the suite again.") from None


# the pass -------------------------------------------------------------------------------------------------------------------------------------

@dataclass
class JudgeRun:
    report: EvaluationReport
    verdicts: dict[str, Any]
    rubrics: dict[str, Rubric]
    judge: JudgeClient
    gateway_judge: GatewayJudge
    models: tuple[str, ...]
    self_judged: bool
    folder: Path | None = None
    rows: list[dict[str, Any]] = field(default_factory=list)

    @property
    def scored(self) -> int:
        return sum(summary.n_scored for summary in self.report.summaries.values())

    @property
    def failed_calls(self) -> int:
        return len(self.gateway_judge.failures)


def judge_run(
    folder: Path, gateway: Gateway, settings: Settings, *, models: Sequence[str], rubrics: dict[str, Rubric] | None = None, only: set[str] | None = None,
    repeats: int = 1, allow_self_judge: bool = False, out: Path | None = None, say: Callable[[str], None] = lambda text: None,
) -> JudgeRun:
    """Score every turn of the run in `folder` against the rubrics; write `<folder>/judge/` (or `out`). Raises `ConfigError` for a run that cannot be
    judged, a judge that is the run's own model, or an `out` folder that already has files."""
    verdicts, evidence = read_run(folder)
    rubrics = rubrics if rubrics is not None else load_rubrics(RUBRICS_FILE)
    unknown = sorted((only or set()) - set(rubrics))
    if unknown:
        raise ConfigError(f"No rubric called {', '.join(unknown)}.", fix=f"The rubrics are: {', '.join(rubrics)}.")
    chosen = {key: rubric for key, rubric in rubrics.items() if only is None or key in only}
    run_model = str(verdicts.get("setup", {}).get("model", ""))
    self_judged = is_self_judged(run_model, models)
    if self_judged and not allow_self_judge:
        raise ConfigError(
            f"The judge would be {', '.join(models)}, the model that answered in this run ({run_model}). A judge must not grade its own model (decision 3.13).",
            fix="Judge with another model: --config with a model service of its own (for Gemini, llm.pro_enabled: true gives the judge the Pro model). "
                "Or pass --allow-self-judge; the result is then marked as not independent.",
        )
    target = out or folder / JUDGE_DIR
    if target.is_dir() and any(target.iterdir()):
        raise ConfigError(f"The folder {target} already has files in it.", fix="Give --out a new folder (a second judge, or a second pass, is its own folder).")

    gateway_judge = GatewayJudge(gateway, settings)
    judge = JudgeClient(repeats=repeats, judge_factory=gateway_judge)
    config = EvaluationConfig(
        layers=JUDGE_LAYERS, mode="live", judge_provider=settings.llm_backend.provider, judge_model=", ".join(models), judge_repeats=repeats,
        judge_temperature=settings.llm.steps[Step.JUDGE.value].temperature, gate_judge_on_static=False, bootstrap_resamples=0,
    )
    runner = EvaluationRunner(
        config=config, judge=judge, rubrics=chosen, applicability=APPLICABILITY, sample_formatter=sample_block,
        manifest_extra={
            "suite": "judge", "judged_run": verdicts.get("run_id"), "judged_kind": verdicts.get("kind"), "app_setup": verdicts.get("setup"),
            "judge_models": list(models), "self_judged": self_judged, "judge_independent": not self_judged, "rubrics_file": RUBRICS_FILE.name,
        },
    )
    sessions = [session_from_evidence(item) for item in evidence]
    # one sample for each turn: a conversation is judged turn by turn, so a report says which turn fell short and not that the conversation did
    references = {session.session_id: {"turn_references": [{} for _ in session.turns]} for session in sessions}
    say(f"Judging {sum(len(session.turns) for session in sessions)} turn(s) of {len(sessions)} conversation(s) against {len(chosen)} rubric(s), judge {', '.join(models)}.")
    report = runner.run(sessions, references=references, run_id=f"judge-{verdicts.get('run_id', folder.name)}")
    report.run_manifest.update({
        "judge_verdicts": gateway_judge.verdicts, "judge_failed_calls": len(gateway_judge.failures),
        "judge_tokens_reported": {"input_tokens": gateway_judge.tokens_in, "output_tokens": gateway_judge.tokens_out},
    })
    result = JudgeRun(report, verdicts, chosen, judge, gateway_judge, tuple(models), self_judged, target, rows_of(report))
    write_judge(result, target)
    return result


def rows_of(report: EvaluationReport) -> list[dict[str, Any]]:
    """One row for each turn and rubric that a judge scored or that was decided without one (N/A, not exercised)."""
    rows = []
    for sample in report.samples:
        for key, metric in sample.metrics.items():
            rows.append({
                "case": sample.session_id, "turn": sample.turn_index + 1, "rubric": key, "verdict": metric.verdict.value,
                "score": metric.score, "judge_score": metric.details.get("judge_score_1_4"), "reason": metric.reason,
            })
    return rows


def write_judge(run: JudgeRun, folder: Path) -> dict[str, Path]:
    folder.mkdir(parents=True, exist_ok=True)
    summary = run.report.summary["metrics"]
    payload = {
        "run_id": run.verdicts.get("run_id"), "kind": run.verdicts.get("kind"), "app_setup": run.verdicts.get("setup"),
        "judge": {
            "models": list(run.models), "self_judged": run.self_judged, "independent": not run.self_judged, "repeats": run.judge.repeats,
            "verdicts": run.gateway_judge.verdicts, "failed_calls": run.failed_calls,
            "tokens": {"input_tokens": run.gateway_judge.tokens_in, "output_tokens": run.gateway_judge.tokens_out},
        },
        "advisory": "A judge score never fails a run, a baseline or a hard gate.",
        "rubrics": {key: {"threshold": rubric.threshold, "stage": rubric.stage, **{name: summary.get(key, {}).get(name) for name in (
            "n_scored", "mean", "pass_rate", "n_pass", "n_fail", "n_na", "n_not_exercised")}} for key, rubric in run.rubrics.items()},
        "turns": run.rows,
    }
    paths = {
        "judge": folder / "judge.json",
        "summary": write_summary(run.report, folder / "summary.json"),
        "results": write_results(run.report, folder / "results.json", include_trace=False),
        "csv": write_csv(run.report, folder / "results.csv"),
    }
    paths["judge"].write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    return paths


def lowest(run: JudgeRun, limit: int = 8) -> list[dict[str, Any]]:
    """The turns a judge scored below the rubric's bar, worst first."""
    failed = [row for row in run.rows if row["verdict"] == Verdict.FAIL.value]
    return sorted(failed, key=lambda row: (row["score"] if row["score"] is not None else 1.0, row["case"], row["turn"]))[:limit]
