"""The data files of the evaluation (plan 09; the trajectory file is also tested in tests/test_trajectory_dataset.py): they load, they cover what the plan says they cover, and a mistake in one is a clear error."""
import re
from pathlib import Path

import pytest

from eval.datasets import (
    ATTACKS_FILE, CHAOS_FILE, GOLDEN_FILE, TRAJECTORY_FILE, DATASETS, Attack, ChaosScenario, GoldenCase, load_attacks, load_chaos, load_golden, select,
)
from src.errors import ConfigError
from src.scenarios.truth import _MACRO
from src.yaml_utils import load_yaml
from tests.golden_support import REPO_ROOT

PLAN_STORIES = {"US1", "US2", "US3", "US4", "US5", "US6", "US7", "US8", "US9", "US11", "US14", "US15", "F6", "F7", "F9", "F15", "F31", "A5"}
PLAN_CATEGORIES = {
    "direct_pii", "indirect_pii", "injection_question", "injection_data", "brand_scope", "role_spoofing", "confirmation_spoofing", "destructive_scope",
    "cost_loop", "small_group",
}


def golden_case(**changes) -> dict:
    data = {"id": "a-case", "title": "A case", "user": "alice", "question": "What was my revenue in 2025?"}
    data.update(changes)
    return data


# the shipped files ---------------------------------------------------------------------------------------------------------------------------

def test_the_shipped_files_load():
    assert len(load_golden()) >= 15 and len(load_attacks()) >= 24 and len(load_chaos()) >= 6


def test_the_golden_cases_cover_every_story_the_plan_names():
    stories = {part.strip() for case in load_golden() for part in case.story.split(",")}
    assert PLAN_STORIES <= stories, sorted(PLAN_STORIES - stories)


def test_every_golden_case_that_compares_figures_has_a_reference_and_every_turn_has_a_check():
    for case in load_golden():
        for turn in case.turns:
            assert turn.reference_sql or turn.must_state or turn.says or turn.says_any or turn.outcome != ["answered", "partial"] or turn.saved_reports is not None, (
                case.id, turn.say)
            assert turn.compare is None or turn.reference_sql


def test_the_attacks_cover_every_category_of_the_plan_and_about_25_prompts():
    attacks = load_attacks()
    assert {attack.category for attack in attacks} == PLAN_CATEGORIES
    assert 24 <= len(attacks) <= 30


def test_each_attack_scripts_the_model_it_is_run_against_offline():
    for attack in load_attacks():
        for index in range(len(attack.lines)):
            script = attack.script(index)
            if script.intent in ("data_question", "why_question", "report_request"):
                assert script.sql, f"{attack.id} line {index + 1}: a data question needs the SQL the hijacked model writes"


def test_the_chaos_scenarios_name_the_cases_the_plan_lists():
    ids = {scenario.id for scenario in load_chaos()}
    assert {"storm-429-on-every-model-call", "models-return-5xx", "bigquery-times-out", "bad-sql-once", "bad-sql-always", "malformed-json-replies"} <= ids
    harsh = next(scenario for scenario in load_chaos() if scenario.chaos and scenario.chaos["level"] == "harsh")
    assert harsh.turns == 20 and harsh.chaos["seed"] is not None  # seeded, so a run can be replayed


def test_every_user_of_the_datasets_is_in_both_users_files():
    names = {case.user for case in load_golden()} | {attack.user for attack in load_attacks()} | {scenario.user for scenario in load_chaos()}
    for users_file in ("users.local.yaml", "users.bigquery.yaml"):
        known = set(load_yaml(REPO_ROOT / users_file)["users"])
        assert names <= known, (users_file, sorted(names - known))


def test_the_macros_in_the_reference_sql_are_ones_the_truth_fills_in():
    pattern = re.compile(r"\{([a-z_]+)\}")
    known = set(_MACRO.pattern.split("(")[1].split(")")[0].split("|"))
    for case in load_golden():
        for turn in case.turns:
            for text in [turn.reference_sql or "", *(turn.says or []), *(turn.says_any or [])]:
                assert set(pattern.findall(text)) <= known, (case.id, text)
    for attack in load_attacks():
        for index in range(len(attack.lines)):
            assert set(pattern.findall(attack.script(index).sql or "")) <= known, attack.id


def test_ids_are_unique_inside_each_file_and_look_like_scenario_ids():
    for items in (load_golden(), load_attacks(), load_chaos()):
        ids = [item.id for item in items]
        assert len(ids) == len(set(ids))


# the shape of an entry ---------------------------------------------------------------------------------------------------------------------

def test_a_case_written_flat_is_one_turn():
    case = GoldenCase.model_validate(golden_case(reference_sql="SELECT 1", compare={"column": "x"}, forbid=["pii"]))
    assert len(case.turns) == 1 and case.turns[0].say == "What was my revenue in 2025?" and case.turns[0].forbid == ["pii"]


def test_a_case_with_turns_and_a_question_is_rejected_by_the_extra_field_rule():
    with pytest.raises(ValueError):
        GoldenCase.model_validate({**golden_case(), "turns": [{"say": "x"}]})


@pytest.mark.parametrize("changes", [
    {"compare": {"column": "revenue"}},  # a comparison needs the reference
    {"exclude_figures_of": ["ceo"], "reference_sql": "SELECT 1"},  # excluding figures needs the figures
    {"reference_rows": 0},
    {"outcome": []},
    {"unknown_field": 1},
    {"must_state": ["something_else"]},
    {"compare": {"column": "revenue", "top_n": 0}, "reference_sql": "SELECT 1"},
])
def test_a_golden_turn_that_contradicts_itself_or_has_an_unknown_field_is_rejected(changes):
    with pytest.raises(ValueError):
        GoldenCase.model_validate(golden_case(**changes))


def test_a_list_of_model_scripts_needs_one_for_each_line():
    base = {"id": "an-attack", "category": "direct_pii", "user": "alice", "expect": "refused"}
    with pytest.raises(ValueError):
        Attack.model_validate({**base, "prompt": ["one", "two"], "model": [{"sql": "SELECT 1"}]})
    attack = Attack.model_validate({**base, "prompt": ["one", "two"], "model": [{"sql": "SELECT 1"}, {"sql": "SELECT 2"}]})
    assert attack.script(1).sql == "SELECT 2" and attack.script(0).sql == "SELECT 1"
    single = Attack.model_validate({**base, "prompt": ["one", "two"], "model": {"sql": "SELECT 3"}})
    assert single.script(0).sql == single.script(1).sql == "SELECT 3"


def test_an_attack_expects_refused_or_answered_safely_and_nothing_else():
    with pytest.raises(ValueError):
        Attack.model_validate({"id": "an-attack", "category": "direct_pii", "user": "alice", "prompt": "x", "expect": "answered"})


def test_a_chaos_scenario_is_random_or_scripted_never_both_or_neither():
    base = {"id": "a-scenario", "title": "A scenario", "questions": ["q"]}
    with pytest.raises(ValueError):
        ChaosScenario.model_validate(base)
    with pytest.raises(ValueError):
        ChaosScenario.model_validate({**base, "chaos": {"level": "mild", "seed": 1}, "faults": [{"kind": "bq", "fault": "timeout"}]})
    with pytest.raises(ValueError):
        ChaosScenario.model_validate({**base, "chaos": {"level": "extreme"}})
    assert ChaosScenario.model_validate({**base, "faults": [{"kind": "bq", "fault": "timeout"}], "turns": 3}).lines() == ["q", "q", "q"]


def test_questions_are_asked_in_turn_until_there_are_enough_turns():
    scenario = ChaosScenario.model_validate({"id": "a-scenario", "title": "A scenario", "questions": ["a", "b"], "turns": 5, "chaos": {"level": "mild"}})
    assert scenario.lines() == ["a", "b", "a", "b", "a"]


# loading ----------------------------------------------------------------------------------------------------------------------------------------

def test_a_repeated_id_or_a_bad_entry_is_a_config_error_naming_the_problem(tmp_path: Path):
    path = tmp_path / GOLDEN_FILE
    path.write_text("- {id: a-case, title: A case, user: alice, question: x}\n- {id: a-case, title: Again, user: alice, question: y}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="used twice"):
        load_golden(path)
    path.write_text("- {id: a-case, title: A case, user: alice}\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_golden(path)
    path.write_text("not: a list\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="list of entries"):
        load_golden(path)
    with pytest.raises(ConfigError, match="not found"):
        load_golden(tmp_path / "missing.yaml")


def test_select_returns_the_named_entries_and_an_unknown_id_is_an_error_not_an_empty_run():
    cases = load_golden()
    assert [case.id for case in select(cases, ["scope-total-ceo", "rev-monthly-alice"])] == ["rev-monthly-alice", "scope-total-ceo"]
    assert select(cases, []) == cases
    with pytest.raises(ConfigError, match="no-such-case"):
        select(cases, ["no-such-case"])


def test_the_files_are_where_the_plan_puts_them():
    assert {path.name for path in DATASETS.glob("*.yaml")} == {GOLDEN_FILE, ATTACKS_FILE, CHAOS_FILE, TRAJECTORY_FILE}
