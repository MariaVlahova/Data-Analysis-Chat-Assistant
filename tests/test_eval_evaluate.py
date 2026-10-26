"""Scoring, the files of a run, and the baseline (plan 09): what `evaluate`, `write_run` and `eval/baseline.py` do with samples played on the sample data."""
import json
from pathlib import Path

import pytest

from eval import baseline as B
from eval.baseline import compare, entry_for, record, setup_key, setup_of
from eval.datasets import load_attacks, load_golden
from eval.evaluate import WITHHELD, evaluate, render, write_run
from src.config import Settings

CASES = {case.id: case for case in load_golden()}
ATTACKS = {attack.id: attack for attack in load_attacks()}
SETUP = {"environment": "local", "model": "qwen/test", "data": "sample data"}


def play(offline, *ids):
    return [offline.harness.play_golden(CASES[name], before_turn=offline.oracle(CASES[name])) for name in ids]


def leaking_attack(offline, monkeypatch):
    """The injection through a result row, with the Output Guard switched off: the answer with e-mail addresses in it is printed."""
    from src.guards.output_guard import OutputGuard

    monkeypatch.setattr(OutputGuard, "check", lambda self, text: text)
    attack = ATTACKS["inject-through-a-result-row"]
    return offline.harness.play_attack(attack, before_turn=offline.hijack(attack.script, attack.user))


# the baseline -----------------------------------------------------------------------------------------------------------------------------------

def test_a_baseline_is_kept_for_each_setup_and_a_new_setup_does_not_replace_another(tmp_path: Path):
    path = tmp_path / "baseline.json"
    other = {"environment": "default", "model": "gemini", "data": "BigQuery"}
    record(path, SETUP, {"a": True, "b": False}, run_id="r1", tolerance=1)
    record(path, other, {"a": True, "b": True}, run_id="r2", tolerance=0)
    assert entry_for(SETUP, path)["passed"] == 1 and entry_for(other, path)["passed"] == 2
    assert entry_for(SETUP, path)["tolerance"] == 1 and entry_for(other, path)["tolerance"] == 0
    record(path, SETUP, {"a": True, "b": True}, run_id="r3")
    assert entry_for(SETUP, path)["run_id"] == "r3" and entry_for(other, path)["run_id"] == "r2"
    assert json.loads(path.read_text(encoding="utf-8"))["format"] == 1


def test_the_default_tolerance_is_the_plans_proposal_of_one_case_and_the_file_records_what_it_applies_to(tmp_path: Path):
    entry = record(tmp_path / "b.json", SETUP, {"a": True}, run_id="r")
    assert entry["tolerance"] == B.DEFAULT_TOLERANCE == 1 and entry["setup"] == SETUP and entry["cases_hash"]
    assert setup_key(SETUP) == "local / sample data / qwen/test"


@pytest.mark.parametrize("now, ok", [
    ({"a": True, "b": True, "c": True, "d": True}, True),  # as good as the baseline
    ({"a": True, "b": True, "c": True, "d": False}, True),  # one case fewer: inside the tolerance of one
    ({"a": True, "b": True, "c": False, "d": False}, False),  # two fewer: below the bar
])
def test_a_run_passes_when_it_passes_the_baseline_number_minus_the_tolerance(tmp_path: Path, now, ok):
    path = tmp_path / "b.json"
    record(path, SETUP, {"a": True, "b": True, "c": True, "d": True}, run_id="r", tolerance=1)
    comparison = compare(entry_for(SETUP, path), now)
    assert comparison.ok is ok and "the bar is 3" in comparison.summary


def test_a_run_names_the_cases_that_regressed_and_the_ones_that_got_fixed(tmp_path: Path):
    path = tmp_path / "b.json"
    record(path, SETUP, {"a": True, "b": False}, run_id="r", tolerance=5)
    comparison = compare(entry_for(SETUP, path), {"a": False, "b": True, "c": True})
    assert comparison.regressions == ["a"] and comparison.fixed == ["b"] and comparison.new == ["c"]


def test_without_a_baseline_for_the_setup_there_is_no_bar_and_the_run_says_how_to_make_one(tmp_path: Path):
    comparison = compare(entry_for(SETUP, tmp_path / "missing.json"), {"a": True})
    assert comparison.ok is None and "--update-baseline" in comparison.summary


def test_a_change_in_the_cases_is_a_note_and_the_comparison_is_over_the_cases_both_have(tmp_path: Path):
    path = tmp_path / "b.json"
    other_cases = tmp_path / "cases.yaml"
    other_cases.write_text("- {id: x}\n", encoding="utf-8")
    record(path, SETUP, {"a": True, "gone": True}, run_id="r", tolerance=0, cases_file=other_cases)
    comparison = compare(entry_for(SETUP, path), {"a": True, "new": False})
    assert comparison.ok is True and comparison.new == ["new"]
    assert any("cases changed" in note for note in comparison.notes) and any("gone" in note for note in comparison.notes)


def test_another_model_in_the_same_environment_and_data_is_a_setup_of_its_own(tmp_path: Path):
    path = tmp_path / "b.json"
    other_model = {**SETUP, "model": "qwen/other"}
    record(path, SETUP, {"a": True, "b": True}, run_id="r1")
    assert entry_for(other_model, path) is None  # judged against nothing, not against the other model's bar
    record(path, other_model, {"a": False, "b": False}, run_id="r2")
    assert entry_for(SETUP, path)["passed"] == 2 and entry_for(other_model, path)["passed"] == 0


def test_a_run_that_shares_no_case_with_the_baseline_has_no_bar_and_does_not_pass_by_default(tmp_path: Path):
    path = tmp_path / "b.json"
    record(path, SETUP, {"a": True}, run_id="r", tolerance=0)
    comparison = compare(entry_for(SETUP, path), {"other": False})
    assert comparison.ok is None and "nothing to compare" in comparison.summary and comparison.new == ["other"]


def test_a_missing_baseline_file_is_an_empty_baseline_and_an_unreadable_one_is_an_error_never_overwritten(tmp_path: Path):
    from src.errors import ConfigError

    path = tmp_path / "b.json"
    assert B.read(path)["setups"] == {}
    record(path, SETUP, {"a": True}, run_id="r1")
    good = path.read_text(encoding="utf-8")
    for damaged in ("{not json", json.dumps({"setups": []}), "[]"):
        path.write_text(damaged, encoding="utf-8")
        with pytest.raises(ConfigError, match="baseline"):
            B.read(path)
        with pytest.raises(ConfigError):
            record(path, SETUP, {"a": False}, run_id="r2")  # the other setups in a damaged file are not replaced by one new entry
        assert path.read_text(encoding="utf-8") == damaged
    path.write_text(good, encoding="utf-8")
    assert entry_for(SETUP, path)["run_id"] == "r1"


def test_a_baseline_is_put_in_place_whole_and_leaves_no_temporary_file(tmp_path: Path):
    path = tmp_path / "b.json"
    record(path, SETUP, {"a": True}, run_id="r1")
    record(path, {**SETUP, "model": "other"}, {"a": True}, run_id="r2")
    assert sorted(item.name for item in tmp_path.iterdir()) == ["b.json"] and len(B.read(path)["setups"]) == 2


@pytest.mark.parametrize("now, refused", [
    ({"a": True, "b": True, "c": True}, False),   # as good: the baseline is rewritten
    ({"a": True, "b": True, "c": False}, True),   # one fewer, inside the tolerance of one: still not allowed to slide the bar down
    ({"a": True, "b": False, "c": False}, True),
])
def test_a_run_that_passed_fewer_cases_than_the_baseline_does_not_become_the_baseline(tmp_path: Path, now, refused):
    from src.errors import ConfigError

    path = tmp_path / "b.json"
    record(path, SETUP, {"a": True, "b": True, "c": True}, run_id="before", tolerance=1)
    if refused:
        with pytest.raises(ConfigError, match="the baseline passed 3"):
            B.refuse_to_lower(path, SETUP, now)
    else:
        B.refuse_to_lower(path, SETUP, now)
    B.refuse_to_lower(tmp_path / "none.json", SETUP, {"a": False})  # a setup with no baseline yet has nothing to lower


def test_attacks_and_chaos_pass_only_when_every_case_does_and_a_golden_run_by_its_bar(offline_eval):
    from eval.evaluate import Evaluation, CaseVerdict

    def evaluation(kind, *passed):
        verdicts = [CaseVerdict(f"c{n}", kind, "alice", ok, 1, 1.0) for n, ok in enumerate(passed)]
        return Evaluation(kind, None, verdicts, [], SETUP)  # type: ignore[arg-type]

    for kind in ("attack", "chaos"):
        everything, one_failed = evaluation(kind, True, True), evaluation(kind, True, False)
        for item in (everything, one_failed):
            item.report = type("Report", (), {"gate": {"verdict": "PASS"}})()  # the hard gates held in both
        assert everything.ok and not one_failed.ok  # NFR12: a single failure is a bug
    golden = evaluation("golden", False, False)
    golden.report = type("Report", (), {"gate": {"verdict": "PASS"}})()
    assert golden.ok  # a first run has no bar to miss
    golden.comparison = B.Comparison(False, "below the bar")
    assert not golden.ok


def test_the_setup_names_the_environment_the_model_and_the_data():
    assert setup_of(Settings()) == {"environment": "default", "model": "gemini-3.8-flash (light: gemini-3.5-flash-lite)", "data": "BigQuery"}


# the run ------------------------------------------------------------------------------------------------------------------------------------------

@pytest.mark.golden
def test_a_run_is_written_as_summary_results_csv_verdicts_and_a_transcript_of_each_conversation(offline_eval, tmp_path: Path):
    samples = play(offline_eval, "scope-total-ceo", "no-data-carol")
    result = evaluate("golden", samples, setup_of(offline_eval.settings), run_id="run-1", live=False)
    result.comparison = compare(None, {v.id: v.passed for v in result.verdicts})
    paths = write_run(result, tmp_path / "run", run_id="run-1")
    assert {name: path.is_file() for name, path in paths.items()} == dict.fromkeys(("summary", "results", "csv", "verdicts"), True)
    verdicts = json.loads(paths["verdicts"].read_text(encoding="utf-8"))
    assert verdicts["kind"] == "golden" and verdicts["passed"] == 2 and verdicts["total"] == 2 and verdicts["hard_gates_ok"] is True
    assert [case["id"] for case in verdicts["cases"]] == ["scope-total-ceo", "no-data-carol"] and verdicts["baseline"]["ok"] is None
    transcript = (tmp_path / "run" / "transcripts" / "scope-total-ceo.md").read_text(encoding="utf-8")
    assert "## Turn 1: answered" in transcript and "What was the total revenue in 2025?" in transcript
    summary = json.loads(paths["summary"].read_text(encoding="utf-8"))["summary"]
    assert summary["gate"]["verdict"] == "PASS" and "i1_no_personal_data" in summary["metrics"]


def test_an_answer_with_personal_data_is_not_written_to_the_results_or_the_transcript(offline_eval, tmp_path: Path, monkeypatch):
    sample = leaking_attack(offline_eval, monkeypatch)
    result = evaluate("attack", [sample], setup_of(offline_eval.settings), run_id="leak", live=False)
    assert not result.hard_gates_ok
    write_run(result, tmp_path / "run", run_id="leak")
    everything = "".join(path.read_text(encoding="utf-8") for path in (tmp_path / "run").rglob("*") if path.is_file())
    assert "canary" not in everything.lower() and WITHHELD in everything


def test_the_hard_gates_fail_the_run_even_when_every_case_met_the_baseline(offline_eval, monkeypatch):
    sample = leaking_attack(offline_eval, monkeypatch)
    result = evaluate("attack", [sample], setup_of(offline_eval.settings), run_id="x", live=False)
    result.comparison = B.Comparison(True, "the baseline bar is met")
    assert result.comparison_ok and not result.hard_gates_ok and not result.ok


@pytest.mark.golden
def test_a_gate_nothing_could_measure_is_reported_as_unmeasured_never_as_clean(offline_eval):
    samples = play(offline_eval, "scope-total-ceo")  # the CEO sees every brand, so the brand-scope check has nothing to decide
    result = evaluate("golden", samples, setup_of(offline_eval.settings), run_id="u", live=False)
    assert "i3_own_brands_only" in result.gate["floors_unmeasured"]
    assert result.gate["verdict"] == "PASS" and "not measured" in result.gate["reason"]


@pytest.mark.golden
def test_the_screen_shows_each_case_the_gate_and_the_baseline(offline_eval):
    from rich.console import Console

    samples = play(offline_eval, "scope-total-ceo")
    result = evaluate("golden", samples, setup_of(offline_eval.settings), run_id="s", live=False)
    result.comparison = compare(None, {"scope-total-ceo": True})
    console = Console(width=200, record=True)
    render(console, result)
    text = console.export_text()
    assert "scope-total-ceo" in text and "PASS" in text and "Hard gates PASS" in text
    assert "no baseline for this setup yet" in text and "1 of 1 case(s) passed" in text


@pytest.mark.golden
def test_the_run_reports_the_tokens_the_app_spent_for_each_case_and_in_total(offline_eval, tmp_path: Path):
    from rich.console import Console

    samples = play(offline_eval, "scope-total-ceo", "no-data-carol")
    for sample in samples:  # the scripted model reports no tokens, so the turns' rows are given some, as the gateway's rows have them
        for ev in sample.evidence:
            ev.row.update(llm_calls=4, tokens_in=1500, tokens_out=250)
    result = evaluate("golden", samples, setup_of(offline_eval.settings), run_id="t", live=False)
    turns = [len(sample.evidence) for sample in samples]
    total = sum(turns)

    assert [(v.llm_calls, v.tokens_in, v.tokens_out) for v in result.verdicts] == [(4 * n, 1500 * n, 250 * n) for n in turns]
    assert result.usage == {"llm_calls": 4 * total, "tokens_in": 1500 * total, "tokens_out": 250 * total}

    paths = write_run(result, tmp_path / "run", run_id="t")
    verdicts = json.loads(paths["verdicts"].read_text(encoding="utf-8"))
    assert verdicts["usage"] == result.usage and [case["tokens_in"] for case in verdicts["cases"]] == [1500 * n for n in turns]
    # evalkit's own cost accounting reads what the gateway reported, not an estimate from the length of the text
    manifest = json.loads(paths["summary"].read_text(encoding="utf-8"))["run_manifest"]
    assert manifest["tokens_app"] == {"input_tokens": 1500 * total, "output_tokens": 250 * total}
    assert manifest["token_accounting"]["main"]["basis"] == "provider-reported token usage"

    console = Console(width=220, record=True)
    render(console, result)
    text = console.export_text()
    assert "Tokens in/out" in text and f"{1500 * turns[0]:,}/{250 * turns[0]:,}" in text
    assert f"Model use by the app: {4 * total:,} call(s), {1500 * total:,} tokens in, {250 * total:,} tokens out." in text


def test_a_run_whose_model_reported_no_tokens_says_zero_and_the_manifest_falls_back_to_an_estimate(offline_eval):
    samples = play(offline_eval, "scope-total-ceo")
    result = evaluate("golden", samples, setup_of(offline_eval.settings), run_id="z", live=False)
    assert result.usage["tokens_in"] == 0 and result.usage["tokens_out"] == 0
    assert result.report.run_manifest["token_accounting"]["main"]["basis"] == "estimated from message text (lower bound)"


def test_a_run_of_some_cases_gets_no_tolerance_so_one_named_case_that_regressed_misses_the_bar(tmp_path: Path):
    """The tolerance is counted over the whole set. Without this, `run_golden one-case` could never fail: 0 passed >= 1 - 1."""
    path = tmp_path / "b.json"
    record(path, SETUP, {"a": True, "b": True, "c": True}, run_id="r", tolerance=1)
    comparison = compare(entry_for(SETUP, path), {"a": False})
    assert comparison.ok is False and comparison.regressions == ["a"] and comparison.tolerance == 0
    assert any("partial run is held to no tolerance" in note for note in comparison.notes)
    assert compare(entry_for(SETUP, path), {"a": True}).ok is True
    assert compare(entry_for(SETUP, path), {"a": True, "b": True, "c": False}).ok is True  # the whole set keeps its tolerance
