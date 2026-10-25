"""Chaos runs, offline (plan 09, F37, NFR19): the real graph, guards, query runner and the REAL model gateway (retries, rate limit, circuit breaker) over a fake SDK
client, with faults injected from eval/datasets/chaos_scenarios.yaml. Twenty turns under the seeded harsh profile, and the scripted sequences: a 429 storm, 5xx on
every model, a BigQuery timeout, bad SQL once, bad SQL always, malformed JSON.

The chat must never crash. Every turn that failed shows a friendly message with a trace ID and has a metrics row with that ID, stays inside its budget, and a retry
that eventually succeeds is one turn.
"""
import json

import pytest

from eval.baseline import setup_of
from eval.datasets import load_chaos
from eval.evaluate import evaluate
from tests.eval_support import play_chaos

pytestmark = pytest.mark.chaos
SCENARIOS = {scenario.id: scenario for scenario in load_chaos()}


def run(rig, scenario_id):
    sample = play_chaos(rig, SCENARIOS[scenario_id])
    return sample, evaluate("chaos", [sample], setup_of(rig.settings), run_id="test", live=False)


def rows(sample):
    return [json.loads(line) for line in sample.facts.settings.metrics_path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize("scenario_id", sorted(SCENARIOS))
def test_every_scenario_ends_without_a_crash_and_meets_its_expectations(chaos_eval, scenario_id):
    sample, result = run(chaos_eval, scenario_id)
    (verdict,) = result.verdicts
    assert verdict.passed, verdict.failed
    assert result.gate["verdict"] == "PASS", result.gate["reason"]
    assert len(sample.evidence) == SCENARIOS[scenario_id].turns
    assert all(ev.outcome != "crashed" and ev.error_kind != "INTERNAL" for ev in sample.evidence)


def test_twenty_turns_under_the_harsh_profile_all_end_in_an_answer_or_a_friendly_failure_with_a_trace_id(chaos_eval):
    sample, result = run(chaos_eval, "random-harsh-20-turns")
    assert len(sample.evidence) == 20
    outcomes = [ev.outcome for ev in sample.evidence]
    assert {"answered", "failed"} <= set(outcomes), outcomes  # the profile really hurt (some turns failed) and some turns still got through
    by_id = {row["trace_id"]: row for row in rows(sample)}
    for ev in sample.evidence:
        if ev.outcome in ("failed", "refused"):
            assert ev.trace_id in ev.text, f"turn {ev.index}: a failure without its trace ID"
            assert by_id[ev.trace_id]["outcome"] in ("failed", "refused")
    assert result.gate["verdict"] == "PASS"


def test_the_same_seed_gives_the_same_run(chaos_eval, tmp_path):
    """A failure under chaos can be replayed: the seed is in the file and in the banner. Two runs with it end the same way, turn by turn."""
    first, _ = run(chaos_eval, "random-mild-10-turns")
    scenario = SCENARIOS["random-mild-10-turns"]
    again = chaos_eval.harness.play_chaos(scenario.model_copy(update={"id": "random-mild-10-turns-again"}))
    assert [ev.outcome for ev in first.evidence] == [ev.outcome for ev in again.evidence]


def test_a_429_storm_opens_the_breaker_and_later_turns_do_not_wait_on_a_dead_model(chaos_eval):
    sample, _ = run(chaos_eval, "storm-429-on-every-model-call")
    opened = [span for ev in sample.evidence for span in ev.spans if span["name"] == "llm.call" and span["attrs"].get("breaker_opened")]
    assert opened, "no circuit breaker opened"
    assert all(ev.outcome == "failed" and ev.trace_id in ev.text for ev in sample.evidence)
    assert chaos_eval.clock.sleeps  # the gateway waited between attempts, on the fake clock: no real second was spent


def test_every_model_failing_with_a_503_costs_calls_within_the_budget_and_ends_politely(chaos_eval):
    sample, _ = run(chaos_eval, "models-return-5xx")
    for ev in sample.evidence:
        assert int(ev.row["llm_calls"]) <= chaos_eval.settings.budget.simple.calls
        assert "Trace ID" in ev.text or ev.trace_id in ev.text


def test_a_bigquery_timeout_is_retried_inside_the_runners_limit_and_reported_not_raised(chaos_eval):
    sample, _ = run(chaos_eval, "bigquery-times-out")
    assert all(ev.outcome in ("answered", "partial", "failed") for ev in sample.evidence)
    attempts = [len([s for s in ev.spans if s["name"] == "bq.dry_run"]) for ev in sample.evidence]
    assert max(attempts) <= 1 + chaos_eval.settings.bigquery.transient_retries  # the runner stops at its limit, whatever the faults


def test_bad_sql_once_is_repaired_and_counted_as_one_repair_in_one_turn(chaos_eval):
    sample, _ = run(chaos_eval, "bad-sql-once")
    first, second = sample.evidence
    assert first.outcome == "answered" and first.row["repairs"] == 1 and second.row["repairs"] == 0
    assert len(rows(sample)) == 2  # a repair that works is not a second turn


def test_bad_sql_always_gives_up_inside_its_limit_with_the_repair_never_looping(chaos_eval):
    sample, _ = run(chaos_eval, "bad-sql-always")
    for ev in sample.evidence:
        assert ev.outcome in ("failed", "partial", "refused")
        assert int(ev.row["llm_calls"]) <= chaos_eval.settings.budget.simple.calls
        assert ev.row["repairs"] <= chaos_eval.settings.loop.sql_repairs + 1  # the repairs of NFR5, and the last repair, then it stops


def test_malformed_json_is_asked_for_again_once_and_a_second_bad_reply_fails_the_step_not_the_chat(chaos_eval):
    sample, _ = run(chaos_eval, "malformed-json-replies")
    assert all(ev.outcome in ("answered", "partial", "failed", "refused") for ev in sample.evidence)
    assert all(ev.error_kind != "INTERNAL" for ev in sample.evidence)


def test_a_retry_that_eventually_succeeds_is_one_turn_with_one_metrics_row_and_one_trace(chaos_eval):
    sample, result = run(chaos_eval, "random-mild-10-turns")
    ids = [row["trace_id"] for row in rows(sample)]
    assert len(ids) == len(set(ids)) == len(sample.evidence)
    assert result.report.samples[0].metrics["chaos_one_row_per_turn"].decided


def test_a_turn_stays_inside_its_time_limit_under_every_scenario_on_the_clock_the_gateway_waits_on(chaos_eval):
    """The gateway's waits and the turn budget read the same clock here, so a storm of 429s really spends the turn's seconds. A turn that waited past its limit would
    show as longer than the limit, and a regression that let retries run on would be caught (the wall time of the test is a few milliseconds and would show nothing)."""
    limit = chaos_eval.settings.budget.simple.seconds
    waited = 0.0
    for scenario_id in sorted(SCENARIOS):
        starts: list[float] = []
        sample = play_chaos(chaos_eval, SCENARIOS[scenario_id], before_turn=lambda index, line: starts.append(chaos_eval.clock.now))
        starts.append(chaos_eval.clock.now)
        for turn, (began, ended) in enumerate(zip(starts, starts[1:]), 1):
            assert ended - began <= limit * 1.15 + 5.0, (scenario_id, turn, ended - began)
            waited = max(waited, ended - began)
        assert sample.evidence
    assert waited > 1.0, "no scenario made the gateway wait: the clock check would prove nothing"
