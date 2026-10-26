"""`python -m eval.run_trajectory` (plan 09), through its Typer app with a scripted model on the sample data: the same app a `chat` starts, the nine rules of the code as hard
gates, and what a case expects of a path or a memory as a finding that does not fail a run on a live model. Exit codes: 0 passed, 1 did not, 2 could not start.
"""
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eval import run_trajectory
from eval.datasets import load_trajectories
from src.llm.routing import Step
from tests.agent_support import guard_reply

pytestmark = pytest.mark.trajectory

runner = CliRunner(env={"COLUMNS": "250"})
CASES = {case.id: case for case in load_trajectories()}
CASE = "path-simple-skips-the-planner"


def run(offline, *args, case=CASE):
    return runner.invoke(
        run_trajectory.app, [case, "--config", str(Path(offline.settings.base_dir) / "config.yaml"), "--skip-preflight", "--out", str(offline.out_dir / "cli"), *args]
    )


def script(offline, case=CASE):
    offline.trajectory(CASES[case])(1, CASES[case].turns[0].say)


def test_a_trajectory_run_scores_the_case_holds_the_gates_and_writes_the_run(offline_eval):
    script(offline_eval)
    result = run(offline_eval)
    assert result.exit_code == 0, result.output
    assert CASE in result.output and "Hard gates PASS" in result.output and "1 of 1 case(s) passed" in result.output
    folder = offline_eval.out_dir / "cli"
    verdicts = json.loads((folder / "verdicts.json").read_text(encoding="utf-8"))
    assert verdicts["kind"] == "trajectory" and verdicts["passed"] == 1 and verdicts["baseline"] is None
    assert {"seq_valid_path", "drift_memory_carries_over"} <= set(json.loads((folder / "summary.json").read_text(encoding="utf-8"))["summary"]["metrics"])
    assert (folder / "transcripts" / f"{CASE}.md").is_file() and (folder / "evidence" / f"{CASE}.json").is_file()
    assert (folder / CASE / "runs.jsonl").is_file()


def test_a_run_files_hold_no_checkpoint_state(offline_eval):
    """The checks read the state in memory. Nothing of it, the history and the answers in it, goes into the files a run writes beyond the transcript and the evidence."""
    script(offline_eval)
    run(offline_eval)
    written = "\n".join(path.read_text(encoding="utf-8") for path in (offline_eval.out_dir / "cli").glob("*.json"))
    assert '"history"' not in written and '"shown_labels"' not in written and '"last_answer"' not in written


def test_a_path_the_case_did_not_expect_is_a_finding_on_a_live_run_and_does_not_fail_it(offline_eval):
    """The model took the question for a why question: the code kept its own rules (the Planner ran, because the turn kind said so), but the case wanted no Planner."""
    script(offline_eval)
    offline_eval.gateway.always(Step.INPUT_GUARD, guard_reply(intent="why_question", standalone=CASES[CASE].turns[0].say))
    result = run(offline_eval)
    assert result.exit_code == 0, result.output
    assert "advisory trajectory_path_expected" in result.output and "planner is in the path" in result.output
    assert "Hard gates PASS" in result.output


def test_a_rule_of_the_code_that_breaks_fails_the_run_with_one(offline_eval, monkeypatch):
    from src.agent import runtime

    real = runtime.fresh_turn_input
    monkeypatch.setattr(runtime, "fresh_turn_input", lambda **kwargs: {**real(**kwargs), "evidence": {"enough": True}})  # a field the first turn could not have had
    script(offline_eval, case="path-schema-question-never-queries")
    result = run(offline_eval, case="path-schema-question-never-queries")
    assert result.exit_code == 1, result.output
    assert "drift_no_stale_state" in result.output and "evidence is set but evidence_check did not run" in result.output


def test_an_unknown_case_is_an_error_and_not_an_empty_passing_run(offline_eval):
    result = run(offline_eval, case="no-such-case")
    assert result.exit_code == 2 and "no-such-case" in result.output


@pytest.mark.parametrize("module", ["eval.run_golden", "eval.run_attacks", "eval.run_chaos", "eval.run_trajectory"])
def test_the_four_commands_share_one_runner(module):
    import importlib

    assert "run_suite" in vars(importlib.import_module(module))
