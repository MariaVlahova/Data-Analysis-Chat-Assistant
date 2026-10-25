"""The PRD section 9 table (plan 09): read from what real runs wrote (a golden run and an attack run on the sample data with a scripted model), row by row, with
`not measured` where nothing measured and `NOT MET` where a check failed. The offline suites behind some rows are counted from a junit file.
"""
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eval import report as R
from eval.baseline import setup_of
from eval.datasets import load_attacks, load_golden
from eval.evaluate import evaluate, write_run
from eval.harness import Harness
from src.config import load_settings

runner = CliRunner(env={"COLUMNS": "250"})
CASES = {case.id: case for case in load_golden()}
ATTACKS = {attack.id: attack for attack in load_attacks()}


def make_run(offline, folder: Path, kind: str, ids: list[str]) -> Path:
    """Play `ids` of a suite into `folder` (each conversation gets its own folder in it, as a real run does) and write the run."""
    harness = Harness(offline.settings, folder)
    try:
        if kind == "golden":
            samples = [harness.play_golden(CASES[name], before_turn=offline.oracle(CASES[name])) for name in ids]
        else:
            samples = [harness.play_attack(ATTACKS[name], before_turn=offline.hijack(ATTACKS[name].script, ATTACKS[name].user)) for name in ids]
    finally:
        harness.close()
    result = evaluate(kind, samples, setup_of(offline.settings), run_id=folder.name, live=False)
    write_run(result, folder, run_id=folder.name)
    return folder


@pytest.fixture
def runs(offline_eval, tmp_path: Path):
    base = tmp_path / "runs"
    golden = make_run(offline_eval, base / "golden-20261025-100000", "golden", ["scope-total-ceo", "scope-total-alice", "small-groups-ceo", "no-data-carol", "top-customers-alice"])
    attack = make_run(offline_eval, base / "attack-20261025-100500", "attack", ["pii-emails-of-top-customers", "inject-through-a-result-row", "scope-i-am-the-ceo-now", "destroy-everything"])
    return base, golden, attack


def rows_of(runs, settings, tests=None, baseline=None) -> dict[str, R.Row]:
    _, golden, attack = runs
    return {row.metric: row for row in R.table(R.load([golden, attack]), settings, tests, baseline)}


def row(rows, start: str) -> R.Row:
    return next(item for name, item in rows.items() if name.startswith(start))


# reading the runs ------------------------------------------------------------------------------------------------------------------------------------

def test_the_newest_run_of_each_suite_is_found_by_when_it_was_written_not_by_what_the_folder_is_called(tmp_path: Path):
    import os

    for name, kind, when in (("golden-20261001", "golden", 2000), ("zz-my-test", "golden", 1000), ("attack-20261001", "attack", 1500)):
        (tmp_path / name).mkdir()
        verdicts = tmp_path / name / "verdicts.json"
        verdicts.write_text(json.dumps({"kind": kind}), encoding="utf-8")
        os.utime(verdicts, (when, when))
    (tmp_path / "notes").mkdir()
    assert {path.name for path in R.find_runs(tmp_path)} == {"golden-20261001", "attack-20261001"}  # `zz-my-test` sorts last and is older
    assert R.find_runs(tmp_path / "missing") == []


def test_two_runs_of_one_suite_are_refused_so_their_turns_are_not_added_up_under_one_suites_counts(runs):
    from src.errors import ConfigError

    _, golden, attack = runs
    with pytest.raises(ConfigError, match="Two golden runs"):
        R.load([golden, golden])


def test_an_answer_built_in_code_has_been_through_the_numeric_check_even_though_that_path_records_no_event():
    data = R.Data()
    spans = [{"name": "composer", "status": "ok"}, {"name": "numeric_check", "status": "ok", "events": []}]
    R._read_trace(data, spans)
    assert data.answered_without_numeric_check == 0
    R._read_trace(data, [{"name": "composer", "status": "ok"}])  # a composed answer that never met the node: that is a skip
    assert data.answered_without_numeric_check == 1
    R._read_trace(data, [{"name": "composer", "status": "error"}])  # a Composer that failed wrote nothing to check
    assert data.answered_without_numeric_check == 1


def test_the_small_groups_row_fails_when_the_answer_named_a_hidden_group_and_is_not_exercised_when_none_was_hidden(runs, local_settings):
    _, golden, attack = runs
    path = golden / "verdicts.json"
    verdicts = json.loads(path.read_text(encoding="utf-8"))
    case = next(case for case in verdicts["cases"] if case["id"].startswith("small-groups"))
    case["failed"] = {"golden_hidden_groups": "turn 1: names a hidden group, Alaska"}
    path.write_text(json.dumps(verdicts), encoding="utf-8")
    failed = row(rows_of(runs, local_settings), "Groups below k")
    assert failed.status == R.NOT_MET and "FAILED" in failed.measured
    case["failed"] = {"golden_numbers": "1 of 5 key figure(s) stated"}  # a model that lists too few states has not shown a small group
    path.write_text(json.dumps(verdicts), encoding="utf-8")
    assert "held" in row(rows_of(runs, local_settings), "Groups below k").measured
    case["failed"], case["undecided"] = {}, ["golden_hidden_groups"]
    path.write_text(json.dumps(verdicts), encoding="utf-8")
    nothing = row(rows_of(runs, local_settings), "Groups below k")
    assert "not exercised" in nothing.measured and nothing.status != R.NOT_MET


def test_every_turn_of_every_conversation_is_read_with_its_suite_and_sample(runs, offline_eval):
    _, golden, attack = runs
    data = R.load([golden, attack])
    assert set(data.suites) == {"golden", "attack"} and data.rows
    assert {row["kind"] for row in data.rows} == {"golden", "attack"} and {"scope-total-ceo", "pii-emails-of-top-customers"} <= {row["sample"] for row in data.rows}
    assert data.result_guard_calls >= 1 and data.groups_hidden >= 1  # the small-groups case hid groups
    assert data.numeric_matched >= 1


def test_a_folder_that_is_not_a_run_is_a_clear_error(tmp_path: Path):
    from src.errors import ConfigError

    with pytest.raises(ConfigError, match="not a run folder"):
        R.load([tmp_path])


# the table ------------------------------------------------------------------------------------------------------------------------------------------------

def test_a_clean_run_measures_every_row_it_can_and_does_not_pretend_to_measure_the_rest(runs, local_settings):
    rows = rows_of(runs, local_settings)
    assert row(rows, "PII leaks").status == R.MET and "no leak" in row(rows, "PII leaks").measured and "stopped 1 draft answer(s)" in row(rows, "PII leaks").measured
    assert row(rows, "Numbers grounded").status == R.MET and "backed by results" in row(rows, "Numbers grounded").measured
    assert row(rows, "Groups below k").status == R.MET and "golden small-groups case: held" in row(rows, "Groups below k").measured
    assert row(rows, "Budget respected").status == R.MET
    assert row(rows, "Brand scope escapes").status == R.MET and row(rows, "Turns that crash").status == R.MET
    assert row(rows, "Failed turns").status == R.MET
    # nothing in a run measures these: pytest does, and it was not run
    assert row(rows, "Duplicate saves").status == R.NOT_MEASURED and row(rows, "Delete flows").status == R.NOT_MEASURED
    # a golden run with no baseline has a number and no bar; a scripted model's speed says nothing about a live one
    assert row(rows, "Golden test cases").status == R.NO_BAR and "5 of 5 case(s) passed" in row(rows, "Golden test cases").measured
    assert row(rows, "Simple turns under 30 s").status == R.NOT_MEASURED and "scripted" in row(rows, "Simple turns under 30 s").measured


def test_with_the_pytest_suites_counted_the_rows_that_rest_on_them_are_measured(runs, local_settings):
    tests = {"duplicates": R.Counts(passed=22), "delete": R.Counts(passed=40), "pii": R.Counts(passed=10), "scope": R.Counts(passed=5),
             "small_groups": R.Counts(passed=30), "crashes": R.Counts(passed=18)}
    rows = rows_of(runs, local_settings, tests)
    assert row(rows, "Duplicate saves").status == R.MET and "22 of 22 passed" in row(rows, "Duplicate saves").measured
    assert row(rows, "Delete flows").status == R.MET
    failing = {**tests, "duplicates": R.Counts(passed=21, failed=1)}
    assert row(rows_of(runs, local_settings, failing), "Duplicate saves").status == R.NOT_MET


def test_a_baseline_the_run_missed_makes_the_golden_row_not_met(runs, local_settings, tmp_path: Path):
    _, golden, attack = runs
    verdicts = json.loads((golden / "verdicts.json").read_text(encoding="utf-8"))
    verdicts["baseline"] = {"ok": False, "summary": "3 of 5 cases passed; the bar is 4: NOT met"}
    (golden / "verdicts.json").write_text(json.dumps(verdicts), encoding="utf-8")
    assert row(rows_of(runs, local_settings), "Golden test cases").status == R.NOT_MET


def test_the_golden_row_is_held_to_the_baseline_as_it_is_now_even_if_it_was_written_after_the_run(runs, local_settings, offline_eval, tmp_path: Path):
    from eval.baseline import record, setup_of

    setup = setup_of(offline_eval.settings)
    path = tmp_path / "baseline.json"
    case_ids = [case["id"] for case in json.loads((runs[1] / "verdicts.json").read_text(encoding="utf-8"))["cases"]]
    assert row(rows_of(runs, local_settings, None, path), "Golden test cases").status == R.NO_BAR  # no baseline yet
    record(path, setup, dict.fromkeys(case_ids, True), run_id="another-run", tolerance=0)
    met = row(rows_of(runs, local_settings, None, path), "Golden test cases")
    assert met.status == R.MET and "the bar is 5" in met.measured and "this run is the baseline" not in met.measured
    record(path, setup, dict.fromkeys(case_ids, True), run_id=json.loads((runs[1] / "verdicts.json").read_text(encoding="utf-8"))["run_id"], tolerance=0)
    assert "this run is the baseline" in row(rows_of(runs, local_settings, None, path), "Golden test cases").measured
    stricter = {**dict.fromkeys(case_ids, True), "a-case-the-run-lacks": True}
    record(path, setup, stricter, run_id="another-run", tolerance=0)
    assert row(rows_of(runs, local_settings, None, path), "Golden test cases").status == R.MET  # compared over the cases both have


def test_a_run_that_passed_fewer_cases_than_the_baseline_makes_the_golden_row_not_met(runs, local_settings, offline_eval, tmp_path: Path):
    from eval.baseline import record, setup_of

    verdicts = json.loads((runs[1] / "verdicts.json").read_text(encoding="utf-8"))
    verdicts["cases"][0]["passed"] = False
    verdicts["cases"][1]["passed"] = False
    (runs[1] / "verdicts.json").write_text(json.dumps(verdicts), encoding="utf-8")
    path = tmp_path / "baseline.json"
    record(path, setup_of(offline_eval.settings), {case["id"]: True for case in verdicts["cases"]}, run_id="before", tolerance=1)
    below = row(rows_of(runs, local_settings, None, path), "Golden test cases")
    assert below.status == R.NOT_MET and "NOT met" in below.measured


def test_a_leak_in_the_attack_run_makes_the_pii_row_not_met_and_the_command_exit_with_one(offline_eval, tmp_path: Path, monkeypatch, local_project):
    from src.guards.output_guard import OutputGuard

    monkeypatch.setattr(OutputGuard, "check", lambda self, text: text)
    attack = make_run(offline_eval, tmp_path / "runs" / "attack-20261025-110000", "attack", ["inject-through-a-result-row"])
    data = R.load([attack])
    settings = load_settings(local_project / "config.yaml", load_env_file=False)
    pii = next(item for item in R.table(data, settings, None) if item.metric.startswith("PII leaks"))
    assert pii.status == R.NOT_MET and "failed leak check(s)" in pii.measured
    result = runner.invoke(R.app, [str(attack), "--config", str(local_project / "config.yaml"), "--baseline", str(tmp_path / "none.json")])
    assert result.exit_code == 1 and "NOT MET" in result.output


def test_the_command_prints_the_table_the_per_kind_stats_and_writes_json(runs, local_project, tmp_path: Path):
    _, golden, attack = runs
    out = tmp_path / "rows.json"
    result = runner.invoke(R.app, [str(golden), str(attack), "--config", str(local_project / "config.yaml"), "--baseline", str(tmp_path / "none.json"), "--json", str(out)])
    assert result.exit_code == 0, result.output
    assert "PRD section 9" in result.output and "Per kind of turn" in result.output and "Simple turns under 30 s" in result.output
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert len(payload["rows"]) == 11 and payload["per_kind"]


def test_with_no_run_to_read_the_command_says_what_to_run_first(local_project):
    result = runner.invoke(R.app, ["--config", str(local_project / "config.yaml")])
    assert result.exit_code == 2 and "No evaluation run was found" in result.output


def test_the_table_has_the_eleven_rows_of_the_prd_in_the_prds_order(runs, local_settings):
    names = [item.metric for item in R.table(R.load(list(runs[1:])), local_settings, None)]
    prefixes = [
        "PII leaks", "Numbers grounded", "Groups below k = 10 suppressed", "Duplicate saves and deletes on retry", "Budget respected", "Brand scope escapes",
        "Golden test cases answered correctly", "Turns that crash the CLI", "Delete flows that remove a report not in the preview", "Simple turns under 30 s",
        "Failed turns that produce a trace ID",
    ]
    assert len(names) == len(prefixes) and all(name.startswith(prefix) for name, prefix in zip(names, prefixes))


# the parts ------------------------------------------------------------------------------------------------------------------------------------------------

def test_a_junit_file_is_counted_for_each_group_by_the_test_files_it_names(tmp_path: Path):
    xml = tmp_path / "junit.xml"
    xml.write_text(
        '<testsuites><testsuite>'
        '<testcase classname="tests.test_a" name="one"/><testcase classname="tests.test_a" name="two"><failure/></testcase>'
        '<testcase classname="tests.test_b" name="one"/><testcase classname="tests.test_b" name="two"><skipped/></testcase>'
        '<testcase classname="tests.test_c" name="x"><error/></testcase>'
        '</testsuite></testsuites>', encoding="utf-8",
    )
    counts = R.count_junit(xml, {"first": ("tests/test_a.py", "tests/test_b.py"), "second": ("tests/test_c.py",), "none": ("tests/test_zzz.py",)})
    assert (counts["first"].passed, counts["first"].failed, counts["first"].skipped) == (2, 1, 1)
    assert counts["second"].failed == 1 and counts["none"].total == 0
    assert R.count_junit(tmp_path / "missing.xml", {}) == {}


def test_the_offline_suites_behind_the_rows_can_be_run_and_counted():
    counts = R.run_tests({"words": ("tests/test_textutil.py",), "missing": ("tests/test_no_such_file.py",)})
    assert counts["words"].passed > 0 and counts["words"].failed == 0 and counts["missing"].total == 0


def test_a_turn_is_held_to_the_configured_budget_for_its_kind_and_the_bytes_per_turn(local_settings):
    ok = {"turn_kind": "simple", "llm_calls": 3, "latency_ms": 5000, "bytes_billed": 1000}
    assert R.budget_check(ok, local_settings) == (True, "")
    assert R.budget_check({**ok, "llm_calls": 99}, local_settings) == (False, "calls")
    assert R.budget_check({**ok, "latency_ms": 10_000_000}, local_settings) == (False, "seconds")
    assert R.budget_check({**ok, "bytes_billed": 10**12}, local_settings) == (False, "bytes")
    assert R.budget_check({"turn_kind": None, "llm_calls": 99}, local_settings)[0] is True  # a turn with no data kind has no calls budget


def test_calls_and_time_are_summed_for_each_kind_of_turn_for_q3_and_q30(runs):
    data = R.load(list(runs[1:]))
    stats = {item["kind"]: item for item in R.kind_stats(data)}
    assert "simple" in stats and stats["simple"]["turns"] >= 4 and stats["simple"]["calls_mean"] > 0 and stats["simple"]["seconds_p95"] >= stats["simple"]["seconds_p50"]


def test_a_damaged_baseline_file_is_a_clear_error_with_exit_two_not_a_traceback(runs, local_project, tmp_path: Path):
    _, golden, _ = runs
    damaged = tmp_path / "baseline.json"
    damaged.write_text("{ half an edit", encoding="utf-8")
    result = runner.invoke(R.app, [str(golden), "--config", str(local_project / "config.yaml"), "--baseline", str(damaged)])
    assert result.exit_code == 2 and "is not valid JSON" in result.output  # an uncaught ConfigError would exit 1 with a traceback
