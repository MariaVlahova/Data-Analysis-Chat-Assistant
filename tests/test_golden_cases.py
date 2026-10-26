"""The golden cases (plan 09), on the sample data with a scripted model: the reference SQL runs through the SQL Guard, a model that answers correctly passes every
case, and a model that does not fails the case it got wrong, for the reason it got it wrong. What a real model scores is `python -m eval.run_golden`.

These tests prove the machinery: that it can pass, that it can fail, and that each check looks at what it says it looks at. They say nothing about a model.
"""
import pytest

from eval.baseline import setup_of
from eval.datasets import GoldenCase, load_golden
from eval.evaluate import evaluate
from src.llm.routing import Step
from tests.agent_support import composer_reply, guard_reply, sql_reply

pytestmark = pytest.mark.golden
CASES = {case.id: case for case in load_golden()}
# The definition of revenue is in it (plan 12, D2): a query without it is sent back to the repair step, and the oracle's repair is the right query.
REVENUE_2025 = (
    "SELECT SUM(oi.sale_price) AS revenue FROM order_items oi "
    "WHERE oi.status NOT IN ('Cancelled', 'Returned') AND EXTRACT(YEAR FROM oi.created_at) = 2025"
)


def score(offline, samples, **options):
    return evaluate("golden", samples, setup_of(offline.settings), run_id="test", live=False, **options)


def case_with(**changes) -> GoldenCase:
    return GoldenCase.model_validate({**CASES["scope-total-alice"].model_dump(), **changes})


# the reference ------------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("case_id", sorted(CASES))
def test_the_reference_sql_of_every_case_passes_the_sql_guard_and_runs_for_its_user(offline_eval, case_id):
    """A reference the guard refuses is a mistake in the case. Each runs as the case's user, so the scope is theirs by construction."""
    case = CASES[case_id]
    user = offline_eval.harness.user(case.user)
    for index, turn in enumerate(case.turns, 1):
        if turn.reference_sql is None:
            continue
        ref = offline_eval.harness.reference(turn, user)
        assert not ref.error, f"turn {index}: {ref.error}"
        if turn.reference_rows is not None:
            assert ref.rows == turn.reference_rows
        if turn.compare is not None:
            assert len(ref.values) >= 1 and all(value > 0 for value in ref.values), ref.values


def test_the_manager_and_the_ceo_have_different_totals_for_the_same_question(offline_eval):
    turn = CASES["scope-total-alice"].turns[0]
    mine = offline_eval.harness.reference(turn, offline_eval.harness.user("alice"))
    assert mine.others["ceo"][0] > mine.values[0] > 0  # US11: the manager's total is below the CEO's


def test_a_group_the_result_guard_hides_is_named_in_the_cases_hidden_list(offline_eval):
    turn = CASES["small-groups-ceo"].turns[0]
    ref = offline_eval.harness.reference(turn, offline_eval.harness.user("ceo"))
    assert {"Alaska", "Wyoming"} <= set(ref.hidden)  # the sample data plants regions with fewer than 10 customers
    assert not set(ref.hidden) & {"California", "Texas"}


# a model that is right ------------------------------------------------------------------------------------------------------------------------

def test_a_model_that_answers_every_case_correctly_passes_every_case_and_every_gate(offline_eval):
    samples = [offline_eval.harness.play_golden(case, before_turn=offline_eval.oracle(case)) for case in CASES.values()]
    result = score(offline_eval, samples)
    failed = {verdict.id: verdict.failed for verdict in result.verdicts if not verdict.passed}
    assert not failed, failed
    assert result.gate["verdict"] == "PASS" and not result.gate["floors_unmeasured"]
    assert result.passed_cases == len(CASES) and result.ok


def test_each_check_was_decided_for_the_cases_that_ask_for_it(offline_eval):
    """A pass that rests on a skipped check is not a pass: the cases that name a check must have decided it."""
    wanted = {
        "rev-monthly-alice": ["golden_numbers", "golden_states", "golden_forbids", "golden_outcome", "i3_own_brands_only"],
        "small-groups-ceo": ["golden_numbers", "golden_states", "golden_forbids"],
        "save-then-delete-alice": ["golden_effects", "i4_confirmation_before_delete"],
        "scope-total-alice": ["golden_forbids", "golden_reference_valid", "i3_own_brands_only"],
        "report-q1-alice": ["golden_states"],
        "no-data-carol": ["golden_reference_valid", "golden_states"],
    }
    samples = [offline_eval.harness.play_golden(CASES[name], before_turn=offline_eval.oracle(CASES[name])) for name in wanted]
    results = {sample.session_id: sample.metrics for sample in score(offline_eval, samples).report.samples}
    for name, metrics in wanted.items():
        for metric in metrics:
            assert metric in results[name] and results[name][metric].decided, (name, metric)
    assert "5 of 5 key figure(s) stated" in results["rev-monthly-alice"]["golden_numbers"].reason
    assert "turn 4" not in results["save-then-delete-alice"]["golden_effects"].reason


# a model that is not --------------------------------------------------------------------------------------------------------------------------

def play_wrong(offline, case, **replies):
    """The correct model, then one thing changed: a standing reply for a step."""
    right = offline.oracle(case)

    def before_turn(index, line):
        right(index, line)
        for step, reply in replies.items():
            offline.gateway.always(Step(step), reply)

    return offline.harness.play_golden(case, before_turn=before_turn)


def test_wrong_sql_gives_a_grounded_but_wrong_figure_and_fails_the_case_on_its_numbers(offline_eval):
    """The failure a golden case exists for. The figure in the answer is in the data the model queried, so the numeric check has nothing to remove; it is the
    figure of the wrong year, and only the reference says so."""
    case = CASES["scope-total-ceo"]
    wrong_year = REVENUE_2025.replace("= 2025", "= 2024")
    sample = play_wrong(offline_eval, case, sql_write=sql_reply(wrong_year), composer=composer_reply("The total revenue is in the table.", "Revenue"))
    (verdict,) = score(offline_eval, [sample]).verdicts
    assert not verdict.passed and "0 of 1 key figure(s) stated" in verdict.failed["golden_numbers"]


def test_an_invented_figure_is_removed_by_the_numeric_check_so_the_answer_still_states_the_right_one(offline_eval):
    """The Composer, talked into it, states a total the data does not hold. The check drops that sentence and the code-built answer gives the real figure: the
    case passes because what the user is shown is right, which is what it measures."""
    case = CASES["scope-total-ceo"]
    sample = play_wrong(offline_eval, case, composer=composer_reply("The total revenue in 2025 was 12,345.67.", "Revenue"))
    (verdict,) = score(offline_eval, [sample]).verdicts
    assert "12,345.67" not in sample.evidence[0].text and verdict.passed, verdict.failed


def test_a_figure_rounded_to_whole_dollars_still_passes(offline_eval):
    case = CASES["scope-total-ceo"]
    figure = offline_eval.harness.reference(case.turns[0], offline_eval.harness.user("ceo")).values[0]
    sample = play_wrong(offline_eval, case, composer=composer_reply(f"The total revenue in 2025 was {figure:,.0f}.", "Revenue"))
    (verdict,) = score(offline_eval, [sample]).verdicts
    assert verdict.passed, verdict.failed


def test_the_ceos_figure_never_reaches_a_managers_answer_and_the_case_still_passes(offline_eval):
    """US11. The Composer, talked into it, adds the company total to the manager's answer. The figure is not in the data the manager's query returned, so the
    numeric check removes it: the system holds, and the case, which excludes that figure, is still a pass."""
    case = CASES["scope-total-alice"]
    ceo_total = offline_eval.harness.reference(CASES["scope-total-ceo"].turns[0], offline_eval.harness.user("ceo")).values[0]
    own = offline_eval.harness.reference(case.turns[0], offline_eval.harness.user("alice")).values[0]
    body = f"Your total is {own:,.2f}.\n\nThe whole company's total is {ceo_total:,.2f}."
    sample = play_wrong(offline_eval, case, composer=composer_reply(body, "Revenue"))
    (verdict,) = score(offline_eval, [sample]).verdicts
    assert f"{ceo_total:,.2f}" not in sample.evidence[0].text
    assert verdict.passed, verdict.failed


def test_the_name_of_another_brand_in_the_answer_fails_the_scope_check_and_the_case(offline_eval):
    case = CASES["rev-monthly-alice"]
    other = offline_eval.harness.macros.other_brand(offline_eval.harness.user("alice").brands)
    figure = offline_eval.harness.reference(case.turns[0], offline_eval.harness.user("alice")).values[0]
    # It states a figure of the data, so it is an answer about the data (plan 12, A3) and is kept; the other brand in it is what the check must find.
    sample = play_wrong(offline_eval, case, composer=composer_reply(f"The best month brought in {figure:,.2f}, and {other} did better.", "Revenue"))
    (verdict,) = score(offline_eval, [sample]).verdicts
    assert not verdict.passed and "i3_own_brands_only" in verdict.failed and "golden_forbids" in verdict.failed
    assert other in verdict.failed["i3_own_brands_only"]


def test_a_turn_the_app_treats_as_another_kind_fails_the_outcome_check(offline_eval):
    """The case says this is a why question; a model that calls it a plain data question gets one query and no plan."""
    case = CASES["why-state-underspend-ceo"]
    sample = play_wrong(offline_eval, case, input_guard=guard_reply(intent="data_question", standalone="why"))
    (verdict,) = score(offline_eval, [sample]).verdicts
    assert "golden_outcome" in verdict.failed and "treated as a simple turn, wanted why" in verdict.failed["golden_outcome"]


def test_an_answer_without_the_notice_the_case_requires_fails_its_wording_check(offline_eval):
    case = CASES["report-q1-alice"]
    figure = offline_eval.harness.reference(case.turns[0], offline_eval.harness.user("alice")).values[0]
    sample = play_wrong(offline_eval, case, composer=composer_reply(f"Revenue for the quarter was {figure:,.2f}.", "Q1"))  # a figure of the data, and no action items
    (verdict,) = score(offline_eval, [sample]).verdicts
    assert "golden_states" in verdict.failed and "action_items" in verdict.failed["golden_states"]


def test_a_reference_the_sql_guard_refuses_fails_the_case_loudly(offline_eval):
    bad = case_with(turns=[{"say": "What was my revenue in 2025?", "reference_sql": "SELECT u.email FROM users u", "compare": {"column": "email"}}])
    sample = offline_eval.harness.play_golden(bad, before_turn=offline_eval.oracle(bad))
    (verdict,) = score(offline_eval, [sample]).verdicts
    assert not verdict.passed and "PII_COLUMN" in verdict.failed["golden_reference_valid"]


def test_a_saved_report_count_that_is_wrong_fails_the_effects_check(offline_eval):
    case = CASES["save-then-delete-alice"]
    wrong = GoldenCase.model_validate({**case.model_dump(), "turns": [*[t.model_dump() for t in case.turns[:3]], {**case.turns[3].model_dump(), "saved_reports": 1}]})
    sample = offline_eval.harness.play_golden(wrong, before_turn=offline_eval.oracle(case))
    (verdict,) = score(offline_eval, [sample]).verdicts
    assert not verdict.passed and "turn 4: 0 saved report(s), wanted 1" in verdict.failed["golden_effects"]


def test_a_budget_tighter_than_the_configured_one_is_held_to(offline_eval):
    tight = case_with(turns=[{**CASES["scope-total-alice"].turns[0].model_dump(), "budget": {"calls": 1}}])
    sample = offline_eval.harness.play_golden(tight, before_turn=offline_eval.oracle(tight))
    (verdict,) = score(offline_eval, [sample]).verdicts
    assert not verdict.passed and "the case allows 1" in verdict.failed["golden_effects"]


def test_the_chat_that_cannot_start_is_a_failed_case_not_a_crash_of_the_run(offline_eval):
    nobody = case_with(user="nobrands")
    sample = offline_eval.harness.play_golden(nobody, before_turn=offline_eval.oracle(nobody))
    assert sample.played is None and "No brands" in sample.error
    (verdict,) = score(offline_eval, [sample]).verdicts
    assert not verdict.passed and "No brands" in verdict.failed["start the chat"] and "i6_no_crash" not in verdict.failed  # a refusal to start, not a crash


def test_a_case_for_a_user_the_users_file_does_not_have_is_a_failed_case_and_the_run_goes_on(offline_eval):
    ghost = case_with(id="ghost-case", user="ghost")
    sample = offline_eval.harness.play_golden(ghost, before_turn=offline_eval.oracle(CASES["scope-total-ceo"]))
    assert sample.played is None and "Unknown user 'ghost'" in sample.error
    good = offline_eval.harness.play_golden(CASES["scope-total-ceo"], before_turn=offline_eval.oracle(CASES["scope-total-ceo"]))
    verdicts = {verdict.id: verdict for verdict in score(offline_eval, [sample, good]).verdicts}
    assert not verdicts["ghost-case"].passed and "Unknown user" in verdicts["ghost-case"].failed["start the chat"] and verdicts["scope-total-ceo"].passed


def test_the_small_group_case_exercises_its_hidden_groups_check_on_the_sample_data(offline_eval):
    case = CASES["small-groups-ceo"]
    sample = offline_eval.harness.play_golden(case, before_turn=offline_eval.oracle(case))
    metric = score(offline_eval, [sample]).report.samples[0].metrics["golden_hidden_groups"]
    assert metric.verdict.value == "PASS" and metric.decided and "hidden group(s) is named" in metric.reason


def test_a_hook_that_fails_part_way_keeps_the_turns_before_it_and_fails_the_no_crash_invariant(offline_eval):
    """A conversation that stops at turn 2 of 2 (here the test's own hook raises) is not thrown away: turn 1 was played, and it is judged."""
    case = CASES["follow-up-alice"]
    right = offline_eval.oracle(case)

    def before_turn(index, line):
        if index == 2:
            raise RuntimeError("the hook of the caller broke, with a secret in its message: jane.canary@example.com")
        right(index, line)

    sample = offline_eval.harness.play_golden(case, before_turn=before_turn)
    assert sample.played is not None and len(sample.evidence) == 1 and sample.evidence[0].outcome in ("answered", "partial")
    assert "stopped at turn 2" in sample.error and "RuntimeError" in sample.error and "canary" not in sample.error  # the class and call sites, never the message
    (verdict,) = score(offline_eval, [sample]).verdicts
    assert not verdict.passed and "stopped early" in verdict.failed["i6_no_crash"] and "turn 2: the conversation stopped before it" in verdict.failed["golden_outcome"]


def test_a_case_that_names_a_column_its_reference_does_not_return_says_which_columns_it_does(offline_eval):
    case = case_with(turns=[{**CASES["scope-total-alice"].turns[0].model_dump(), "compare": {"column": "revenu"}}])
    ref = offline_eval.harness.reference(case.turns[0], offline_eval.harness.user("alice"))
    assert ref.error == "the reference has no column 'revenu'; it returns revenue"


def test_an_id_is_played_once_for_each_run_so_a_second_play_cannot_resume_the_first(offline_eval):
    case = CASES["scope-total-ceo"]
    first = offline_eval.harness.play_golden(case, before_turn=offline_eval.oracle(case))
    again = offline_eval.harness.play_golden(case, before_turn=offline_eval.oracle(case))
    assert first.played is not None and again.played is None
    assert "already holds a conversation" in again.error and "play_lines" in again.error


def test_the_scripted_rig_says_so_when_a_test_asks_the_chaos_rig_for_a_scripted_model(chaos_eval):
    with pytest.raises(AssertionError, match="runs the real gateway"):
        chaos_eval.reply(Step.COMPOSER, {})
    with pytest.raises(AssertionError, match="runs the real gateway"):
        chaos_eval.oracle(CASES["scope-total-ceo"])(1, "q")


def test_a_hijacked_data_question_without_sql_says_what_is_missing_instead_of_failing_in_the_gateway(offline_eval):
    from eval.datasets import ModelScript

    hijack = offline_eval.hijack(lambda index: ModelScript(intent="why_question"), "alice")
    with pytest.raises(AssertionError, match="says no SQL"):
        hijack(1, "why?")
