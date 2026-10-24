"""The scenario file format, and the shipped scenarios (`scenarios/*.yaml`): they load, their users exist, and their figures can be computed."""
from datetime import date
from pathlib import Path

import pytest
import yaml

from src.access.users import load_user_directory
from src.scenarios.schema import Scenario, load_scenario, load_scenarios
from src.scenarios.truth import sql_list, sql_literal
from tests.golden_support import REPO_ROOT

SHIPPED = load_scenarios(REPO_ROOT / "scenarios")


def minimal(**changes) -> dict:
    data = {"id": "a-scenario", "title": "A scenario", "user": "alice", "turns": [{"say": "hello"}]}
    data.update(changes)
    return data


def write(folder: Path, name: str, data) -> Path:
    path = folder / f"{name}.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


# the format --------------------------------------------------------------------------------------------------------------

def test_a_minimal_scenario_is_valid_and_has_empty_expectations():
    scenario = Scenario.model_validate(minimal())
    assert scenario.turns[0].expect.is_empty() and scenario.turns[0].advisory.is_empty() and scenario.requires == []


@pytest.mark.parametrize("change", [
    {"id": "Has Capitals"}, {"id": "x"}, {"title": "no"}, {"user": "has space"}, {"turns": []}, {"surprise": 1},
    {"requires": ["a_gpu"]}, {"setup": ["delete_everything"]}, {"chaos": {"level": "extreme"}},
    {"turns": [{"say": ""}]}, {"turns": [{"say": "x", "expect": {"outcome": "exploded"}}]},
    {"turns": [{"say": "x", "expect": {"nonsense": 1}}]}, {"turns": [{"say": "x", "expect": {"saved_reports": -1}}]},
    {"turns": [{"say": "x", "expect": {"max_seconds": 0}}]},
])
def test_bad_scenarios_are_refused(change):
    with pytest.raises(ValueError):
        Scenario.model_validate(minimal(**change))


def test_a_figure_from_the_data_is_allowed_in_any_scenario_because_it_comes_from_whatever_data_the_run_reads():
    number = {"numbers": [{"sql": "SELECT 1"}]}
    Scenario.model_validate(minimal(turns=[{"say": "x", "expect": number}]))
    Scenario.model_validate(minimal(turns=[{"say": "x", "advisory": {"not_contains_sql": ["SELECT 'a'"]}}]))


def test_an_outcome_can_be_one_or_a_list():
    one = Scenario.model_validate(minimal(turns=[{"say": "x", "expect": {"outcome": "answered"}}])).turns[0].expect.outcome
    several = Scenario.model_validate(minimal(turns=[{"say": "x", "expect": {"outcome": ["answered", "partial"]}}])).turns[0].expect.outcome
    assert one == "answered" and several == ["answered", "partial"]


def test_a_bad_file_is_reported_and_the_others_still_load(tmp_path):
    write(tmp_path, "a-one", minimal(id="a-one"))
    write(tmp_path, "b-two", minimal(id="b-two", turns=[]))
    (tmp_path / "c-three.yaml").write_text("id: [unclosed", encoding="utf-8")
    (tmp_path / "d-four.yaml").write_text("- a list\n", encoding="utf-8")
    loaded = load_scenarios(tmp_path)
    assert [item.scenario is not None for item in loaded] == [True, False, False, False]
    assert "turns" in loaded[1].error and loaded[2].error and "one mapping" in loaded[3].error


def test_a_repeated_id_fails_the_later_file(tmp_path):
    write(tmp_path, "a-one", minimal(id="same-id"))
    write(tmp_path, "b-two", minimal(id="same-id"))
    first, second = load_scenarios(tmp_path)
    assert first.scenario is not None and second.scenario is None and "already used by a-one.yaml" in second.error


def test_one_file_and_a_missing_folder(tmp_path):
    path = write(tmp_path, "a-one", minimal(id="a-one"))
    assert load_scenario(path).scenario.id == "a-one" and [i.scenario.id for i in load_scenarios(path)] == ["a-one"]
    assert load_scenarios(tmp_path / "nowhere") == []


# the shipped scenarios -----------------------------------------------------------------------------------------------------------

def test_every_shipped_scenario_loads():
    assert [item.error for item in SHIPPED if item.scenario is None] == []
    assert len(SHIPPED) >= 18


def test_the_ids_are_the_file_names_and_unique():
    ids = [item.scenario.id for item in SHIPPED]
    assert ids == [item.path.stem for item in SHIPPED] and len(set(ids)) == len(ids)


def test_the_default_run_is_a_small_smoke_set_that_covers_each_kind_of_check():
    smoke = [item.scenario for item in SHIPPED if "smoke" in item.scenario.tags]
    assert 4 <= len(smoke) <= 8
    covered = {tag for scenario in smoke for tag in scenario.tags}
    assert {"scope", "pii", "hitl", "golden", "schema", "privacy"} <= covered


def test_every_scenario_uses_a_user_of_users_local_yaml():
    users = load_user_directory(REPO_ROOT / "users.local.yaml", salt="test")
    for item in SHIPPED:
        assert item.scenario.user in users.names, item.scenario.id


def test_every_scenario_says_what_it_is_for():
    for item in SHIPPED:
        assert len(item.scenario.description.split()) >= 12 and item.scenario.tags, item.scenario.id


def test_every_scenario_that_acts_on_reports_seeds_them():
    for item in SHIPPED:
        text = " ".join(turn.say.lower() for turn in item.scenario.turns)
        if "delete" in text or "undo" in text:
            assert "seed_reports" in item.scenario.setup, item.scenario.id


def figures(scenario, brands):
    """(sql, check, brand) for every figure of a scenario: a check with `each_brand` once per brand of the user."""
    for turn in scenario.turns:
        for block in (turn.expect, turn.advisory):
            for check in block.numbers:
                for brand in (brands if check.each_brand else (None,)):
                    yield check.sql, check, brand
            for sql in block.not_contains_sql:
                yield sql, None, None


def test_every_figure_of_every_scenario_can_be_computed_from_the_sample_data(truth):
    users = load_user_directory(REPO_ROOT / "users.local.yaml", salt="test")
    checked = 0
    for item in SHIPPED:
        brands = users.get(item.scenario.user).brands
        for sql, check, brand in figures(item.scenario, brands):
            checked += 1
            if check is not None:
                value = truth.number(sql, brands, brand)
                assert value >= 0, (item.scenario.id, sql)
                assert value > 0 or not check.present, f"{item.scenario.id}: the figure to state is zero"
            else:
                assert truth.strings(sql, brands), f"{item.scenario.id}: the list of values to avoid is empty"
    assert checked >= 12


def test_the_figures_that_must_not_be_stated_differ_from_the_ones_that_must(truth):
    """If a forbidden figure were equal to a required one the scenario could never pass."""
    users = load_user_directory(REPO_ROOT / "users.local.yaml", salt="test")
    for item in SHIPPED:
        brands = users.get(item.scenario.user).brands
        for turn in item.scenario.turns:
            required = [truth.number(c.sql, brands, brand) for c in turn.expect.numbers if c.present for brand in (brands if c.each_brand else (None,))]
            forbidden = [truth.number(c.sql, brands) for c in turn.expect.numbers if not c.present]
            for must in required:
                assert all(abs(must - never) > 0.02 * abs(must) for never in forbidden), item.scenario.id


SAMPLE_BRANDS = ("Maple & Co", "Nordic Peak", "O'Hara", "Urban Thread", "Blue Harbor", "Zephyr", "Casa Lino", "Iron Oak", "Luna Basics", "Quill Denim", "Tessa Rae", "Wren")


def test_a_scenario_that_names_a_sample_brand_says_it_needs_the_sample_data():
    """What makes a scenario portable to BigQuery: it names no brand, and gets the user's own from the users file. A scenario that does name
    one (in what is typed or what is expected; the description may talk about it) must say it needs the sample data, or a run on BigQuery
    would ask about brands that do not exist there."""
    for item in SHIPPED:
        scenario = item.scenario
        typed = " ".join(turn.model_dump_json() for turn in scenario.turns)
        names = [brand for brand in SAMPLE_BRANDS if brand in typed]
        if "sample_data" not in scenario.requires:
            assert names == [], f"{scenario.id} names {names} but does not require sample_data"


def test_most_scenarios_are_portable_and_the_ones_that_are_not_are_the_planted_effects():
    portable = [item.scenario.id for item in SHIPPED if "sample_data" not in item.scenario.requires]
    sample_only = [item.scenario.id for item in SHIPPED if "sample_data" in item.scenario.requires]
    assert len(portable) >= 15
    assert set(sample_only) <= {"why-zephyr-decline", "why-casa-lino-returns", "golden-brand-filter"}


def test_a_brand_with_an_apostrophe_is_a_quoted_sql_literal():
    assert sql_list(("Maple & Co", "O'Hara Outfitters")) == "('Maple & Co', 'O\\'Hara Outfitters')"  # BigQuery's escape, not a doubled quote


def test_the_truth_knows_the_brands_the_last_day_and_fills_in_the_macros(truth, local_db):
    assert truth.latest_date == local_db.end or (local_db.end - truth.latest_date).days <= 3
    assert {"Maple & Co", "O'Hara Outfitters", "nordic peak"} <= truth.brands and None not in truth.brands
    assert truth.expand_sql("{latest_date} {brands}", ("A", "O'B")) == f"{truth.latest_date.isoformat()} ('A', 'O\\'B')"
    assert truth.expand_sql("x = {brand}", (), "O'Hara Outfitters") == "x = 'O\\'Hara Outfitters'"
    assert truth.expand_text("{brands_text} / {latest_date}", ("A", "B")) == f"A, B / {truth.latest_date.isoformat()}"


def test_last_month_is_the_month_before_the_reference_date(truth):
    first = truth.reference_date.replace(day=1)
    assert truth.expand_sql("{this_month_start}", ()) == first.isoformat()
    last = truth.expand_sql("{last_month_start}", ())
    assert last < first.isoformat() and last.endswith("-01")
    assert truth.expand_text("{last_month}", ()) == date.fromisoformat(last).strftime("%B %Y")


def test_the_other_brand_is_the_best_selling_one_that_is_not_the_users(truth):
    alice = truth.other_brand(("Maple & Co", "Nordic Peak"))
    bob = truth.other_brand(("O'Hara Outfitters",))
    assert alice == "Urban Thread" and bob == "Maple & Co"  # by items sold in the sample data
    assert truth.expand_text("revenue of {other_brand}?", ("Maple & Co", "Nordic Peak")) == "revenue of Urban Thread?"
    assert truth.expand_sql("p.brand = {other_brand}", ("Maple & Co",)) == "p.brand = 'Urban Thread'"


def test_a_brand_with_a_quote_is_escaped_the_way_bigquery_reads_it_and_the_sample_data_agrees(truth):
    """`'O''Hara'` is not valid in BigQuery; a backslash before the quote is, and the local stand-in reads it the same way."""
    assert sql_literal("O'Hara Outfitters") == "'O\\'Hara Outfitters'"
    assert truth.number("SELECT COUNT(*) FROM products WHERE brand = " + sql_literal("O'Hara Outfitters")) > 0


def test_the_truth_runs_a_select_and_nothing_else(truth):
    with pytest.raises(Exception) as caught:
        truth.number("DELETE FROM orders WHERE TRUE")
    assert "statement type 'DELETE' is not allowed" in repr(caught.value)  # the dry run reports it and the runner refuses
