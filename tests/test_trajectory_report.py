"""The two rows beyond the PRD's section 9 table (plan 09): execution-path violations, and state-drift and memory violations. Read from what real runs wrote (a trajectory run and an
attack run on the sample data with a scripted model); `not measured` where no run decided the checks, `NOT MET` where one failed, and a table of their own so the PRD's eleven rows
stay as the owner wrote them.
"""
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eval import report as R
from eval.baseline import setup_of
from eval.datasets import load_attacks, load_trajectories
from eval.evaluate import evaluate, write_run
from eval.harness import Harness

pytestmark = pytest.mark.trajectory

runner = CliRunner(env={"COLUMNS": "250"})
CASES = {case.id: case for case in load_trajectories()}
ATTACKS = {attack.id: attack for attack in load_attacks()}


@pytest.fixture
def runs(offline_eval, tmp_path: Path):
    base = tmp_path / "runs"
    folder = base / "trajectory-20261025-100000"
    harness = Harness(offline_eval.settings, folder)
    try:
        samples = [harness.play_trajectory(CASES[name], before_turn=offline_eval.trajectory(CASES[name])) for name in ("memory-save-that-saves-what-was-shown", "path-delete-waits-at-the-gate")]
    finally:
        harness.close()
    write_run(evaluate("trajectory", samples, setup_of(offline_eval.settings), run_id=folder.name, live=False), folder, run_id=folder.name)
    return folder


def rows(folder, tests=None) -> dict[str, R.Row]:
    return {row.metric.split(":")[0]: row for row in R.extra_rows(R.load([folder]), tests)}


def damage(folder: Path, metric: str, **changes) -> None:
    path = folder / "summary.json"
    summary = json.loads(path.read_text(encoding="utf-8"))
    summary["summary"]["metrics"][metric].update(changes)
    path.write_text(json.dumps(summary), encoding="utf-8")


def test_a_run_that_decided_the_checks_reports_no_violation(runs):
    found = rows(runs)
    assert set(found) == {"Execution-path violations", "State drift and memory consistency violations"}
    for item in found.values():
        assert item.target == "0" and item.status == R.MET and item.measured.startswith("0 violation(s) in ") and "sample check(s)" in item.measured


def test_a_violation_in_a_run_is_not_met_and_names_how_many(runs):
    damage(runs, "seq_guards_before_effects", n_fail=1)
    damage(runs, "drift_no_stale_state", n_fail=2)
    found = rows(runs)
    assert found["Execution-path violations"].status == R.NOT_MET and "1 violation(s)" in found["Execution-path violations"].measured
    assert found["State drift and memory consistency violations"].status == R.NOT_MET and "2 violation(s)" in found["State drift and memory consistency violations"].measured


def test_a_run_made_before_the_checks_existed_leaves_the_rows_not_measured_never_met(runs):
    summary = json.loads((runs / "summary.json").read_text(encoding="utf-8"))
    for name in list(summary["summary"]["metrics"]):
        if name.startswith(("seq_", "handoff_", "drift_")):
            del summary["summary"]["metrics"][name]
    (runs / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    for item in rows(runs).values():
        assert item.status == R.NOT_MEASURED and "no run measured it" in item.measured


def test_the_pytest_suites_behind_the_rows_are_named_and_a_failing_one_fails_the_row(runs):
    assert "pytest suites not run" in next(iter(rows(runs).values())).measured
    passed = {"trajectory": R.Counts(passed=120)}
    assert all(item.status == R.MET and "pytest: 120 of 120 passed" in item.measured for item in rows(runs, passed).values())
    failing = {"trajectory": R.Counts(passed=119, failed=1)}
    assert all(item.status == R.NOT_MET for item in rows(runs, failing).values())
    assert set(R.TEST_GROUPS["trajectory"]) == {"tests/test_trajectory_checks.py", "tests/test_trajectory_contract.py", "tests/test_trajectory_offline.py"}


def test_the_command_prints_the_second_table_writes_it_to_json_and_exits_one_when_a_row_is_not_met(runs, local_project, tmp_path: Path):
    config = str(local_project / "config.yaml")
    out = tmp_path / "rows.json"
    result = runner.invoke(R.app, [str(runs), "--config", config, "--baseline", str(tmp_path / "none.json"), "--json", str(out)])
    assert result.exit_code == 0, result.output
    assert "Beyond the PRD" in result.output and "Execution-path violations" in result.output
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert len(payload["rows"]) == 11 and len(payload["beyond_prd"]) == 2  # the PRD's rows are untouched
    damage(runs, "drift_memory_carries_over", n_fail=1)
    result = runner.invoke(R.app, [str(runs), "--config", config, "--baseline", str(tmp_path / "none.json")])
    assert result.exit_code == 1 and "State drift and memory consistency violations" in result.output
