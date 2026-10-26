"""The trajectory suite, offline (plan 09): the real graph, guards, tools and checkpoints on the sample data, with a scripted model that does what each case expects.

Two things are proven here. A model that is right passes every case of `eval/datasets/trajectories.yaml`, with every check required (no advisory metric: a scripted model is the
one that decides the path, so nothing depends on luck). And the checks can fail: each of the breakages below changes the app in one place, and the check that names that
place must say so. A check that has only ever passed has not been shown to look.
"""
import pytest

from eval.baseline import setup_of
from eval.datasets import load_trajectories
from eval.evaluate import evaluate
from src.agent.boundary import node
from src.agent.deps import FINAL_TEXT

pytestmark = pytest.mark.trajectory

CASES = {case.id: case for case in load_trajectories()}
GATES = (
    "seq_valid_path", "seq_guards_before_effects", "seq_route_follows_decision", "seq_loops_bounded", "handoff_plan_is_what_ran", "handoff_approved_was_shown",
    "drift_identity_and_scope", "drift_no_stale_state", "drift_memory_carries_over",
)


def play(rig, case_id):
    case = CASES[case_id]
    sample = rig.harness.play_trajectory(case, before_turn=rig.trajectory(case))
    result = evaluate("trajectory", [sample], setup_of(rig.settings), run_id="test", live=False)
    return sample, result


def metrics(result):
    return result.report.samples[0].metrics


def failed(result):
    return {name: metric.reason for name, metric in metrics(result).items() if metric.verdict.value == "FAIL"}


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_a_model_that_does_what_the_case_expects_passes_every_check(offline_eval, case_id):
    sample, result = play(offline_eval, case_id)
    assert sample.played is not None and not sample.error, sample.error
    assert len(sample.evidence) == len(CASES[case_id].turns)
    (verdict,) = result.verdicts
    assert verdict.passed, verdict.failed
    assert result.gate["verdict"] == "PASS", result.gate["reason"]
    assert not failed(result)


def test_every_rule_of_the_code_was_exercised_across_the_suite_not_only_passed_as_not_applicable(offline_eval):
    decided = set()
    for case_id in CASES:
        _, result = play(offline_eval, case_id)
        decided |= {name for name, metric in metrics(result).items() if metric.decided}
    assert set(GATES) <= decided, sorted(set(GATES) - decided)


def test_the_state_the_checks_read_has_no_result_rows(offline_eval):
    sample, _ = play(offline_eval, "memory-save-that-saves-what-was-shown")
    for ev in sample.evidence:
        assert ev.state, "the checkpoint was not read"
        assert all("guarded" not in result for result in ev.state["results"].values())


def test_the_app_started_again_in_the_middle_comes_back_with_its_memory(offline_eval):
    sample, result = play(offline_eval, "drift-memory-survives-a-restart")
    first, second, third = sample.evidence
    assert third.state["history"][:2] == second.state["history"] and len(third.state["history"]) == 3
    assert sample.report_titles[2] == second.state["last_answer"]["title"] == "Answer"  # the answer of turn 2, which only the checkpoint on disk still had
    assert third.state["conversation_id"] == first.state["conversation_id"]
    assert metrics(result)["drift_memory_carries_over"].verdict.value == "PASS"


# breaking the app in one place -------------------------------------------------------------------------------------------------------------------------

def test_a_result_left_over_from_an_earlier_turn_fails_the_stale_state_check(offline_eval, monkeypatch):
    """`fresh_turn_input` resets every per-turn field. A version that forgets one lets turn 1's results into a turn that ran no query."""
    from src.agent import runtime

    real = runtime.fresh_turn_input
    monkeypatch.setattr(runtime, "fresh_turn_input", lambda **kwargs: {**real(**kwargs), "results": {"q1": {"status": "ok"}}})
    _, result = play(offline_eval, "drift-a-refusal-leaves-the-memory-alone")
    assert "results is set but sql_loop did not run" in failed(result)["drift_no_stale_state"]
    assert result.gate["verdict"] == "FAIL"


def test_a_refusal_that_is_kept_in_the_history_fails_the_memory_check(offline_eval, monkeypatch):
    from src.agent.nodes import finish

    monkeypatch.setattr(finish, "KEPT_IN_HISTORY", frozenset({*finish.KEPT_IN_HISTORY, "refused"}))
    _, result = play(offline_eval, "drift-a-refusal-leaves-the-memory-alone")
    assert "history changed in a turn that is not kept (refused)" in failed(result)["drift_memory_carries_over"]


def test_a_label_that_was_shown_and_not_remembered_fails_the_memory_check(offline_eval, monkeypatch):
    """`shown_labels` is how a new process still refuses a label as an ID. A `finish` that stops recording them leaves the labels it showed unremembered."""
    from src.agent.nodes import finish

    monkeypatch.setattr(finish, "labels_in", lambda text: [])
    _, result = play(offline_eval, "drift-labels-stay-the-same-for-a-session")
    assert "was shown and is not remembered" in failed(result)["drift_memory_carries_over"]


def test_a_why_question_that_loses_its_planner_fails_the_route_check_and_the_case(offline_eval, monkeypatch):
    """If the turn kind of a why question became `simple`, the Planner would be skipped and the question answered as one query."""
    from src.agent.nodes import input_guard

    monkeypatch.setitem(input_guard._DATA_INTENTS, "why_question", "simple")
    _, result = play(offline_eval, "path-why-plans-before-it-queries")
    assert "treated as a simple turn, not why" in failed(result)["seq_route_follows_decision"]
    assert "planner" in failed(result)["trajectory_path_expected"]  # the case's own expectation says the same, from the path it wrote down
    assert result.gate["verdict"] == "FAIL"


def test_an_answer_that_skips_the_output_guard_fails_the_guard_order_check(offline_eval, monkeypatch):
    """A node named `output_guard` that passes the text on without calling the guard still looks right in the node order. Only the span of the guard itself is missing."""
    from src.agent import graph

    def unguarded(deps):
        @node(deps, "output_guard")
        def output_guard(state):
            return {"answer": deps.runtime(state).scratch.pop(FINAL_TEXT), "title": None, "notices": []}

        return output_guard

    monkeypatch.setattr(graph, "make_output_guard", unguarded)
    _, result = play(offline_eval, "path-simple-skips-the-planner")
    assert "the Output Guard step ran without the guard itself" in failed(result)["seq_guards_before_effects"]
    assert result.gate["verdict"] == "FAIL"


def test_a_user_whose_scope_changes_in_the_state_fails_the_identity_check(offline_eval, monkeypatch):
    """The scope in the state is written each turn from the user's own access list. If a question could rewrite it, 'I am the CEO now' would have worked."""
    from src.agent.nodes import policy

    labels = iter(["your brands: Maple & Co, Nordic Peak", "all brands", "all brands"])
    monkeypatch.setattr(policy, "scope_label", lambda user: next(labels))
    _, result = play(offline_eval, "drift-scope-holds-when-asked-to-widen")
    assert "the scope is 'all brands'" in failed(result)["drift_identity_and_scope"]


def test_a_result_left_in_the_state_that_the_query_loop_then_reuses_fails_the_plan_handoff(offline_eval, monkeypatch):
    """`sql_loop` skips the sub-questions that already have a result. If `fresh_turn_input` kept one, the loop would run no query and the Composer would answer from the earlier
    turn's data: the stale-state check cannot see it (the loop is a legitimate maker of results), the plan handoff can."""
    from src.agent import runtime

    real = runtime.fresh_turn_input
    monkeypatch.setattr(runtime, "fresh_turn_input", lambda **kwargs: {**real(**kwargs), "results": {"q1": {"status": "ok", "final_sql": "", "guarded": None}}})
    _, result = play(offline_eval, "path-simple-skips-the-planner")
    assert "q1 has a result (ok) and no sub-question ran for it in this turn" in failed(result)["handoff_plan_is_what_ran"]
    assert result.gate["verdict"] == "FAIL"


def test_a_gate_that_stops_resuming_fails_the_confirmation_handoff(offline_eval, monkeypatch):
    """If the pause were not found again, the typed "yes" would be taken for a new question. No approval would be recorded, so only the pairing of prompt and resume can see it."""
    from src.agent import runtime

    monkeypatch.setattr(runtime, "_gate_interrupt", lambda snapshot: None)
    _, result = play(offline_eval, "path-delete-waits-at-the-gate")
    assert "followed a confirmation prompt but started at policy_context, not at the gate" in failed(result)["handoff_approved_was_shown"]
    assert result.gate["verdict"] == "FAIL"
