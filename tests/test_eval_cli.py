"""The three commands (plan 09): `python -m eval.run_golden`, `run_attacks` and `run_chaos`, through their Typer apps with a scripted model, on the sample data. They
start the same app a `chat` starts, score what comes back, write the run, and say how it went in the exit code: 0 passed, 1 did not, 2 could not start.
"""
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eval import baseline as B
from eval import run_attacks, run_chaos, run_golden
from eval.datasets import GoldenCase, load_attacks, load_golden
from src.llm.routing import Step
from tests.agent_support import composer_reply, guard_reply, sql_reply

runner = CliRunner(env={"COLUMNS": "250"})
CASES = {case.id: case for case in load_golden()}
ATTACKS = {attack.id: attack for attack in load_attacks()}


def config_of(offline) -> str:
    return str(Path(offline.settings.base_dir) / "config.yaml")


def golden(offline, *args, case="scope-total-ceo", correct=True):
    """Script a model for one case (all turns the same), and run the command on it."""
    offline.oracle(CASES[case])(1, CASES[case].turns[0].say)
    if not correct:  # the SQL of the wrong year: a figure the data holds, and not the right one
        wrong = "SELECT SUM(oi.sale_price) AS revenue FROM order_items oi WHERE oi.status NOT IN ('Cancelled', 'Returned') AND EXTRACT(YEAR FROM oi.created_at) = 2024"
        offline.gateway.always(Step.SQL_WRITE, sql_reply(wrong))
        offline.gateway.always(Step.COMPOSER, composer_reply("The total revenue is in the table.", "Revenue"))
    return runner.invoke(run_golden.app, [case, "--config", config_of(offline), "--skip-preflight", "--out", str(offline.out_dir / "cli"), *args])


# golden ---------------------------------------------------------------------------------------------------------------------------------------------

def test_a_golden_run_scores_the_case_writes_the_run_and_says_there_is_no_baseline_yet(offline_eval, tmp_path: Path):
    result = golden(offline_eval, "--baseline", str(tmp_path / "baseline.json"))
    assert result.exit_code == 0, result.output
    assert "scope-total-ceo" in result.output and "Hard gates PASS" in result.output and "no baseline for this setup yet" in result.output
    verdicts = json.loads((offline_eval.out_dir / "cli" / "verdicts.json").read_text(encoding="utf-8"))
    assert verdicts["passed"] == 1 and verdicts["kind"] == "golden" and verdicts["setup"]["data"] == "sample data"
    assert (offline_eval.out_dir / "cli" / "scope-total-ceo" / "runs.jsonl").is_file()  # the conversation's own metrics file, as the report reads it


def test_a_run_that_fell_below_the_baseline_exits_with_one_and_names_the_regression(offline_eval, tmp_path: Path):
    path = tmp_path / "baseline.json"
    B.record(path, B.setup_of(offline_eval.settings), {"scope-total-ceo": True}, run_id="before", tolerance=0)
    result = golden(offline_eval, "--baseline", str(path), correct=False)
    assert result.exit_code == 1, result.output
    assert "NOT met" in result.output and "Regressions: scope-total-ceo" in result.output and "FAIL" in result.output


def test_a_run_that_meets_the_baseline_exits_with_zero(offline_eval, tmp_path: Path):
    path = tmp_path / "baseline.json"
    B.record(path, B.setup_of(offline_eval.settings), {"scope-total-ceo": True}, run_id="before", tolerance=0)
    result = golden(offline_eval, "--baseline", str(path))
    assert result.exit_code == 0 and "met" in result.output and "NOT met" not in result.output


@pytest.mark.parametrize("tolerance", [0, 1, 3])
def test_a_baseline_is_written_from_the_whole_set_with_the_tolerance_that_was_chosen_even_zero(offline_eval, tmp_path: Path, monkeypatch, tolerance):
    """The whole set here is one case (the dataset is replaced for the test): the point is the path from `--update-baseline --tolerance N` to the file."""
    monkeypatch.setattr("eval.suite.load_golden", lambda: [CASES["scope-total-ceo"]])
    path = tmp_path / "baseline.json"
    offline_eval.oracle(CASES["scope-total-ceo"])(1, CASES["scope-total-ceo"].turns[0].say)
    result = runner.invoke(run_golden.app, ["--config", config_of(offline_eval), "--skip-preflight", "--out", str(offline_eval.out_dir / "cli"),
                                            "--update-baseline", "--tolerance", str(tolerance), "--baseline", str(path)])
    assert result.exit_code == 0, result.output
    entry = B.entry_for(B.setup_of(offline_eval.settings), path)
    assert entry["tolerance"] == tolerance and entry["cases"] == {"scope-total-ceo": True} and entry["passed"] == entry["total"] == 1
    assert "Baseline written" in result.output and f"tolerance {tolerance} case(s)" in result.output


def test_a_baseline_is_written_only_from_the_whole_set(offline_eval, tmp_path: Path):
    path = tmp_path / "baseline.json"
    result = golden(offline_eval, "--update-baseline", "--baseline", str(path))
    assert result.exit_code == 2 and "needs the whole golden set" in result.output and not path.exists()


def test_a_baseline_is_not_written_from_a_run_whose_hard_gate_failed(offline_eval, tmp_path: Path, monkeypatch):
    from src.guards.output_guard import OutputGuard

    path = tmp_path / "baseline.json"
    monkeypatch.setattr(OutputGuard, "check", lambda self, text: text)  # the last line of defence is off, so what the Composer is talked into is printed
    monkeypatch.setattr("eval.suite.load_golden", lambda: [CASES["scope-total-ceo"]])
    offline_eval.oracle(CASES["scope-total-ceo"])(1, CASES["scope-total-ceo"].turns[0].say)
    offline_eval.gateway.always(Step.COMPOSER, composer_reply("Write to jane.canary@example.com about the total.", "Revenue"))
    result = runner.invoke(run_golden.app, ["--config", config_of(offline_eval), "--skip-preflight", "--out", str(offline_eval.out_dir / "leak"),
                                            "--update-baseline", "--baseline", str(path)])
    assert result.exit_code == 1 and "baseline was not written" in result.output and "a hard gate failed" in result.output
    assert not path.exists()


def test_a_baseline_is_not_written_from_a_partial_run(offline_eval, tmp_path: Path, monkeypatch):
    path = tmp_path / "baseline.json"
    needs_more = GoldenCase.model_validate({**CASES["scope-total-ceo"].model_dump(), "id": "needs-more"})
    monkeypatch.setattr("eval.suite.load_golden", lambda: [CASES["scope-total-ceo"], needs_more])
    monkeypatch.setattr("eval.suite.unmet_requirement", lambda item, settings: "needs something this setup does not have" if item.id == "needs-more" else "")
    offline_eval.oracle(CASES["scope-total-ceo"])(1, CASES["scope-total-ceo"].turns[0].say)
    result = runner.invoke(run_golden.app, ["--config", config_of(offline_eval), "--skip-preflight", "--out", str(offline_eval.out_dir / "partial"),
                                            "--update-baseline", "--baseline", str(path)])
    assert result.exit_code == 1 and "1 case(s) were skipped" in result.output and not path.exists()


def finished_golden_run(offline, folder: Path) -> Path:
    """A whole golden run made with the oracle model and written as a run folder."""
    from eval.baseline import setup_of
    from eval.evaluate import evaluate, write_run
    from eval.harness import Harness

    harness = Harness(offline.settings, folder)
    try:
        samples = [harness.play_golden(case, before_turn=offline.oracle(case)) for case in CASES.values()]
    finally:
        harness.close()
    write_run(evaluate("golden", samples, setup_of(offline.settings), run_id=folder.name, live=False), folder, run_id=folder.name)
    return folder


def test_a_baseline_can_be_made_from_a_run_that_was_already_read(offline_eval, tmp_path: Path):
    run = finished_golden_run(offline_eval, tmp_path / "golden-20261025-120000")
    path = tmp_path / "baseline.json"
    result = runner.invoke(run_golden.app, ["--baseline-from", str(run), "--tolerance", "0", "--baseline", str(path)])
    assert result.exit_code == 0, result.output
    entry = B.entry_for(B.setup_of(offline_eval.settings), path)
    assert entry["total"] == len(CASES) and entry["passed"] == len(CASES) and entry["tolerance"] == 0 and entry["run_id"] == run.name
    output = " ".join(result.output.split())  # the console wraps long lines
    assert "Baseline written" in output and "from run golden-20261025-120000" in output


@pytest.mark.parametrize("damage, message", [
    ({"hard_gates_ok": False}, "a hard gate failed"),
    ({"skipped": ["rev-monthly-alice"]}, "1 case(s) were skipped"),
    ({"kind": "attack"}, "is a attack run"),
    ({"cases": [{"id": "rev-monthly-alice", "passed": True}]}, f"1 of the {len(CASES)} cases"),
])
def test_a_baseline_is_not_made_from_a_run_that_failed_a_gate_skipped_a_case_or_did_not_cover_the_set(offline_eval, tmp_path: Path, damage, message):
    run = finished_golden_run(offline_eval, tmp_path / "golden-20261025-120000")
    verdicts = json.loads((run / "verdicts.json").read_text(encoding="utf-8"))
    (run / "verdicts.json").write_text(json.dumps({**verdicts, **damage}), encoding="utf-8")
    path = tmp_path / "baseline.json"
    result = runner.invoke(run_golden.app, ["--baseline-from", str(run), "--baseline", str(path)])
    assert result.exit_code == 2 and message in result.output and not path.exists()


def test_a_folder_that_is_not_a_golden_run_is_refused_as_the_source_of_a_baseline(offline_eval, tmp_path: Path):
    result = runner.invoke(run_golden.app, ["--baseline-from", str(tmp_path), "--baseline", str(tmp_path / "baseline.json")])
    assert result.exit_code == 2 and "not the folder of a golden run" in result.output


def test_a_baseline_file_that_cannot_be_read_stops_the_run_before_it_starts_and_is_left_as_it_was(offline_eval, tmp_path: Path):
    path = tmp_path / "baseline.json"
    path.write_text("<<<<<<< merge conflict", encoding="utf-8")
    result = golden(offline_eval, "--baseline", str(path))
    assert result.exit_code == 2 and "is not valid JSON" in " ".join(result.output.split()) and path.read_text(encoding="utf-8") == "<<<<<<< merge conflict"
    assert not (offline_eval.out_dir / "cli").exists()  # nothing was played


def test_a_run_below_the_current_baseline_is_not_written_over_it_even_when_asked(offline_eval, tmp_path: Path, monkeypatch):
    monkeypatch.setattr("eval.suite.load_golden", lambda: [CASES["scope-total-ceo"]])
    path = tmp_path / "baseline.json"
    B.record(path, B.setup_of(offline_eval.settings), {"scope-total-ceo": True}, run_id="before", tolerance=5)
    before = path.read_text(encoding="utf-8")
    offline_eval.oracle(CASES["scope-total-ceo"])(1, CASES["scope-total-ceo"].turns[0].say)
    wrong = "SELECT SUM(oi.sale_price) AS revenue FROM order_items oi WHERE oi.status NOT IN ('Cancelled', 'Returned') AND EXTRACT(YEAR FROM oi.created_at) = 2024"
    offline_eval.gateway.always(Step.SQL_WRITE, sql_reply(wrong))
    offline_eval.gateway.always(Step.COMPOSER, composer_reply("The total revenue is in the table.", "Revenue"))
    result = runner.invoke(run_golden.app, ["--config", config_of(offline_eval), "--skip-preflight", "--out", str(offline_eval.out_dir / "lower"),
                                            "--update-baseline", "--baseline", str(path)])
    output = " ".join(result.output.split())
    assert result.exit_code == 1 and "The baseline was not written: this run passed 0" in output and path.read_text(encoding="utf-8") == before


def test_a_baseline_from_a_finished_run_that_is_below_the_current_baseline_is_refused(offline_eval, tmp_path: Path):
    run = finished_golden_run(offline_eval, tmp_path / "golden-20261025-120000")
    path = tmp_path / "baseline.json"
    B.record(path, B.setup_of(offline_eval.settings), {**dict.fromkeys(CASES, True), "an-older-case": True}, run_id="before", tolerance=0)
    verdicts = json.loads((run / "verdicts.json").read_text(encoding="utf-8"))
    verdicts["cases"][0]["passed"] = False
    (run / "verdicts.json").write_text(json.dumps(verdicts), encoding="utf-8")
    result = runner.invoke(run_golden.app, ["--baseline-from", str(run), "--baseline", str(path)])
    assert result.exit_code == 2 and "The baseline was not written" in " ".join(result.output.split())
    assert B.entry_for(B.setup_of(offline_eval.settings), path)["run_id"] == "before"


def test_a_folder_that_already_has_a_run_in_it_is_refused_so_two_runs_are_never_counted_as_one(offline_eval):
    folder = offline_eval.out_dir / "cli"
    folder.mkdir(parents=True)
    (folder / "old.txt").write_text("an earlier run", encoding="utf-8")
    result = runner.invoke(run_golden.app, ["scope-total-ceo", "--config", config_of(offline_eval), "--skip-preflight", "--out", str(folder)])
    assert result.exit_code == 2 and "already has files" in result.output


def test_a_user_missing_from_the_users_file_fails_that_case_and_the_other_cases_still_run(offline_eval, monkeypatch):
    ghost = GoldenCase.model_validate({**CASES["scope-total-ceo"].model_dump(), "id": "ghost-case", "user": "ghost"})
    monkeypatch.setattr("eval.suite.load_golden", lambda: [ghost, CASES["scope-total-ceo"]])
    offline_eval.oracle(CASES["scope-total-ceo"])(1, CASES["scope-total-ceo"].turns[0].say)
    result = runner.invoke(run_golden.app, ["--config", config_of(offline_eval), "--skip-preflight", "--out", str(offline_eval.out_dir / "cli")])
    assert result.exit_code == 1 and "Traceback" not in result.output and "ghost-case" in result.output and "scope-total-ceo" in result.output
    verdicts = json.loads((offline_eval.out_dir / "cli" / "verdicts.json").read_text(encoding="utf-8"))
    assert {case["id"]: case["passed"] for case in verdicts["cases"]} == {"ghost-case": False, "scope-total-ceo": True}


def test_an_unknown_case_is_an_error_and_not_an_empty_passing_run(offline_eval):
    result = runner.invoke(run_golden.app, ["no-such-case", "--config", config_of(offline_eval), "--skip-preflight"])
    assert result.exit_code == 2 and "no-such-case" in result.output


def test_without_a_gemini_key_the_run_stops_before_it_starts_with_the_fix(offline_eval):
    result = runner.invoke(run_golden.app, ["scope-total-ceo", "--config", config_of(offline_eval)])
    assert result.exit_code == 2 and "GEMINI_API_KEY is not set" in result.output


# attacks --------------------------------------------------------------------------------------------------------------------------------------------------

def test_a_live_attack_run_gates_on_the_invariants_and_only_shows_whether_it_was_refused(offline_eval):
    """The scripted model here is a good one: it answers the email request with a harmless total and never writes the bad SQL. The attack was not refused, which a
    live run reports and does not count; nothing leaked, so the run passes."""
    attack = ATTACKS["pii-emails-of-top-customers"]
    offline_eval.gateway.always(Step.INPUT_GUARD, guard_reply(standalone="my revenue"))
    offline_eval.gateway.always(Step.SQL_WRITE, sql_reply("SELECT SUM(oi.sale_price) AS revenue FROM order_items oi WHERE oi.status NOT IN ('Cancelled', 'Returned')"))
    offline_eval.gateway.always(Step.COMPOSER, composer_reply("The revenue is in the table.", "Revenue"))
    result = runner.invoke(run_attacks.app, [attack.id, "--config", config_of(offline_eval), "--skip-preflight", "--out", str(offline_eval.out_dir / "cli")])
    assert result.exit_code == 0, result.output
    assert "advisory attack_refused" in result.output and "Hard gates PASS" in result.output


def test_a_live_attack_run_that_leaks_exits_with_one(offline_eval, monkeypatch):
    from src.guards.output_guard import OutputGuard

    monkeypatch.setattr(OutputGuard, "check", lambda self, text: text)
    attack = ATTACKS["inject-through-a-result-row"]
    offline_eval.hijack(attack.script, attack.user)(1, attack.lines[0])
    result = runner.invoke(run_attacks.app, [attack.id, "--config", config_of(offline_eval), "--skip-preflight", "--out", str(offline_eval.out_dir / "cli")])
    assert result.exit_code == 1 and "i1_no_personal_data" in result.output and "Hard gates FAIL" in result.output
    assert "canary" not in (offline_eval.out_dir / "cli" / "transcripts" / f"{attack.id}.md").read_text(encoding="utf-8").lower()


# chaos ----------------------------------------------------------------------------------------------------------------------------------------------------

def test_a_live_chaos_run_plays_the_random_profile_for_a_few_turns_and_does_not_gate_on_how_they_ended(chaos_eval):
    result = runner.invoke(run_chaos.app, ["random-harsh-20-turns", "--turns", "3", "--config", str(Path(chaos_eval.settings.base_dir) / "config.yaml"),
                                           "--skip-preflight", "--out", str(chaos_eval.out_dir / "cli")])
    assert result.exit_code == 0, result.output
    verdicts = json.loads((chaos_eval.out_dir / "cli" / "verdicts.json").read_text(encoding="utf-8"))
    assert verdicts["kind"] == "chaos" and verdicts["cases"][0]["turns"] == 3 and "chaos_expectations" in verdicts["advisory_metrics"]


def test_a_scripted_fault_scenario_is_not_played_live_and_a_run_of_nothing_is_not_a_pass(chaos_eval):
    result = runner.invoke(run_chaos.app, ["storm-429-on-every-model-call", "--config", str(Path(chaos_eval.settings.base_dir) / "config.yaml"), "--skip-preflight"])
    assert result.exit_code == 2 and "scripted faults can only be injected offline" in result.output and "Nothing was run" in result.output


def test_a_tolerance_without_a_baseline_to_write_is_refused_and_not_silently_ignored(offline_eval, tmp_path: Path):
    result = runner.invoke(run_golden.app, ["scope-total-ceo", "--config", config_of(offline_eval), "--tolerance", "0", "--baseline", str(tmp_path / "b.json")])
    assert result.exit_code == 2 and "--tolerance only sets the tolerance" in result.output
