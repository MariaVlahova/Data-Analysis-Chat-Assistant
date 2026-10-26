"""`eval/datasets/trajectories.yaml` (plan 09): it loads, it covers both groups of the evaluation, and a mistake in an entry is a clear error at load time and not a case that
quietly checks nothing.
"""
import pytest
from pydantic import ValidationError

from eval.datasets import DATASETS, TRAJECTORY_FILE, TrajectoryCase, load_trajectories
from src.errors import ConfigError
from src.llm_steps import LLM_STEP_NAMES

pytestmark = pytest.mark.trajectory


def case(**changes) -> dict:
    data = {"id": "a-case", "title": "A case", "user": "alice", "turns": [{"say": "What was my revenue in 2025?"}, {"say": "And in 2024?"}]}
    data.update(changes)
    return data


def test_the_shipped_file_loads_and_each_case_has_something_to_check():
    cases = load_trajectories()
    assert len(cases) >= 10 and len({c.id for c in cases}) == len(cases)
    for item in cases:
        expects = [turn for turn in item.turns if turn.path or turn.memory or turn.saved_reports is not None or turn.max_queries is not None]
        invariants_only = item.id.startswith("drift-scope")  # its point is the rules that run on every turn: the scope, and the brands the answers name
        assert expects or invariants_only, f"{item.id}: no turn expects anything of the path or the memory"


def test_both_groups_of_the_evaluation_are_covered_by_name():
    cases = load_trajectories()
    sequence = [c for c in cases if c.id.startswith("path-")]
    memory = [c for c in cases if c.id.startswith(("memory-", "drift-"))]
    assert len(sequence) >= 4 and len(memory) >= 5
    paths = [turn.path for item in sequence for turn in item.turns if turn.path]
    assert any("planner" in p.never for p in paths) and any("planner" in p.through for p in paths)  # a simple turn skips the Planner and a why turn runs it
    assert any("tool.saved_reports.delete" in p.never and "hitl_gate" in p.through for p in paths)  # a delete waits at the gate
    assert any(turn.memory and turn.memory.recalls for item in memory for turn in item.turns)
    assert any(turn.memory and turn.memory.saved_title_of for item in memory for turn in item.turns)
    assert any(turn.memory and turn.memory.same_labels_as for item in memory for turn in item.turns)
    assert any(turn.memory and turn.memory.standalone_has for item in memory for turn in item.turns)
    assert any(item.restart_after for item in memory), "no case starts the app again in the middle"
    assert any(len(item.turns) >= 3 for item in memory)


def test_every_case_that_saves_says_so_and_every_recall_points_at_a_turn_with_figures():
    for item in load_trajectories():
        for turn in item.turns:
            if turn.memory and turn.memory.saved_title_of:
                assert turn.saved_reports is not None, item.id
            if turn.memory and turn.memory.recalls:
                assert item.turns[turn.memory.recalls - 1].compare is not None, item.id


@pytest.mark.parametrize("path", [{"through": ["planer"]}, {"never": ["llm.sql_writer"]}, {"through": ["tool."]}, {"never": ["bq"]}])
def test_a_name_no_trace_can_hold_is_refused_so_a_typo_cannot_pass_by_never_matching(path):
    with pytest.raises(ValidationError, match="not a span a trace can hold"):
        TrajectoryCase.model_validate(case(turns=[{"say": "q", "path": path}]))


def test_every_model_step_can_be_named_as_llm_step():
    for step in LLM_STEP_NAMES:
        TrajectoryCase.model_validate(case(turns=[{"say": "q", "path": {"through": [f"llm.{step}"]}}]))


@pytest.mark.parametrize("field, value", [
    ("says", ["x"]), ("not_says", ["x"]), ("must_state", ["how_i_read"]), ("forbid", ["pii"]), ("exclude_figures_of", ["ceo"]),
    ("hidden", {"sql": "SELECT 1 AS a", "column": "a"}),
])
def test_wording_checks_belong_to_golden_cases_and_are_refused_here(field, value):
    with pytest.raises(ValidationError, match="does not score wording"):
        turn = {"say": "q", "reference_sql": "SELECT 1 AS a", "compare": {"column": "a", "top_n": 1}, field: value}
        TrajectoryCase.model_validate(case(turns=[turn]))


@pytest.mark.parametrize("memory, turn, why", [
    ({"recalls": 2}, 2, "must be an earlier turn"),
    ({"recalls": 1}, 2, "which has no `compare`"),
    ({"same_labels_as": 3}, 2, "must be an earlier turn"),
    ({"saved_title_of": 1}, 2, "needs `saved_reports`"),
])
def test_a_memory_expectation_must_point_at_an_earlier_turn_that_can_answer_it(memory, turn, why):
    turns = [{"say": "first"}, {"say": "second", "memory": memory}]
    with pytest.raises(ValidationError, match=why):
        TrajectoryCase.model_validate(case(turns=turns))


def test_a_recall_of_a_turn_with_a_comparison_is_accepted():
    turns = [
        {"say": "first", "reference_sql": "SELECT 1 AS revenue", "compare": {"column": "revenue", "top_n": 1}},
        {"say": "again", "memory": {"recalls": 1}},
    ]
    assert TrajectoryCase.model_validate(case(turns=turns)).turns[1].memory.recalls == 1


@pytest.mark.parametrize("restart, ok", [([1], True), ([0], False), ([2], False), ([3], False)])
def test_the_app_can_only_be_started_again_between_two_turns(restart, ok):
    if ok:
        assert TrajectoryCase.model_validate(case(restart_after=restart)).restart_after == restart
    else:
        with pytest.raises(ValidationError, match="between two of the 2 turn"):
            TrajectoryCase.model_validate(case(restart_after=restart))


def test_a_broken_file_is_a_config_error_naming_the_problem(tmp_path):
    bad = tmp_path / TRAJECTORY_FILE
    bad.write_text("- id: x-case\n  title: A case\n  user: alice\n  turns:\n    - say: q\n      path: {through: [nope]}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="not a span a trace can hold"):
        load_trajectories(bad)


def test_the_file_is_where_the_loader_looks():
    assert (DATASETS / TRAJECTORY_FILE).is_file()
