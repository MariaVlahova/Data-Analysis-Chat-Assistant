"""Startup brand check (plan 03, S1): a typo stops startup with a suggestion, and a brand is never dropped silently."""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from google.api_core import exceptions as api_exc
from typer.testing import CliRunner

from src.access.users import UserContext, UserDirectory
from src.cli import main as cli_main
from src.cli.main import app
from src.cli.render import Renderer
from src.config import load_settings
from src.data_access.brand_check import BrandMappingError, brand_coverage, check_brand_mapping, suggest, top_brands
from src.data_access.catalog import SchemaCatalog, load_catalog
from src.data_access.safe_runner import SafeQueryRunner
from src.errors import ConfigError
from tests.fake_bigquery import FAKE_BRANDS

runner = CliRunner(env={"COLUMNS": "200"})
NOW = datetime(2026, 10, 23, 9, 0, tzinfo=timezone.utc)
BRANDS = frozenset(FAKE_BRANDS)
DISTINCTIVE = [brand for brand in FAKE_BRANDS if len(brand) > 3]  # "A" or "C" would match ordinary words


def catalog_with(brands) -> SchemaCatalog:
    return SchemaCatalog({}, frozenset(brands), NOW.date(), {}, NOW, NOW)


def directory(**users) -> UserDirectory:
    """directory(alice=["Nike"], ceo=None): a list is a brand manager, None is the all-brands CEO."""
    built = {}
    for name, brands in users.items():
        if brands is None:
            built[name] = UserContext(name, name, "ceo", (), True, "h-" + name)
        else:
            built[name] = UserContext(name, name, "brand_manager", tuple(brands), False, "h-" + name)
    return UserDirectory(built)


# the rule ----------------------------------------------------------------------------------------

def test_exact_matches_pass():
    check_brand_mapping(directory(alice=["Nike", "Levi's"], ceo=None), catalog_with(BRANDS))


def test_a_typo_fails_with_the_user_the_brand_and_a_suggestion():
    with pytest.raises(ConfigError) as err:
        check_brand_mapping(directory(alice=["levis"]), catalog_with(BRANDS))
    assert err.value.message.splitlines()[1] == 'alice: "levis" is not a brand in the data. Did you mean "Levi\'s"?'
    assert "doctor" in err.value.fix


def test_a_difference_in_case_only_is_still_a_miss_with_the_right_suggestion():
    with pytest.raises(ConfigError) as err:
        check_brand_mapping(directory(alice=["nike"]), catalog_with(BRANDS))
    assert 'Did you mean "Nike"?' in err.value.message


def test_a_difference_in_spacing_is_a_miss():
    for wrong in ("Nike ", " Nike", "Calvin  Klein"):
        with pytest.raises(ConfigError):
            check_brand_mapping(directory(alice=[wrong]), catalog_with(BRANDS))


def test_every_problem_of_every_user_is_listed_at_once():
    with pytest.raises(ConfigError) as err:
        check_brand_mapping(directory(alice=["Nike", "Nikee"], bob=["Levis", "Calvin Klein"]), catalog_with(BRANDS))
    lines = err.value.message.splitlines()[1:]
    assert len(lines) == 2 and lines[0].startswith('alice: "Nikee"') and lines[1].startswith('bob: "Levis"')


def test_no_suggestion_when_nothing_is_close():
    with pytest.raises(ConfigError) as err:
        check_brand_mapping(directory(alice=["Zzzzzzzz"]), catalog_with(BRANDS))
    assert err.value.message.splitlines()[1] == 'alice: "Zzzzzzzz" is not a brand in the data.'


def test_a_placeholder_is_called_a_placeholder():
    with pytest.raises(ConfigError) as err:
        check_brand_mapping(directory(alice=["<verified brand 1>"]), catalog_with(BRANDS))
    assert err.value.message.splitlines()[1] == 'alice: "<verified brand 1>" is still a placeholder.'


def test_at_most_three_suggestions():
    known = frozenset({"Brand 1", "Brand 2", "Brand 3", "Brand 4", "Brand 5"})
    assert len(suggest("Brand", known)) == 3
    assert suggest("Brand 1", known)[0] == "Brand 1"


def test_only_the_named_user_is_checked_when_one_is_given():
    users = directory(alice=["levis"], bob=["Nike"])
    check_brand_mapping(users, catalog_with(BRANDS), only_user="bob")  # alice's typo is not bob's problem
    with pytest.raises(ConfigError) as err:
        check_brand_mapping(users, catalog_with(BRANDS), only_user="alice")
    assert "alice" in err.value.message and "bob" not in err.value.message
    with pytest.raises(ConfigError):
        check_brand_mapping(users, catalog_with(BRANDS))  # no name: everyone, as doctor does


def test_the_ceo_and_users_without_brands_are_not_checked_here():
    check_brand_mapping(directory(ceo=None, nobody=[]), catalog_with(frozenset()))  # the empty list is refused elsewhere (S14)


# the live facts doctor prints --------------------------------------------------------------------

@pytest.fixture
def safe(project):
    return SafeQueryRunner(load_settings(project / "config.yaml", load_env_file=False))


def test_top_brands_come_back_ordered_with_their_item_counts(safe):
    assert top_brands(safe, 2) == [("Nike", 1200), ("Levi's", 900)]


def test_brand_coverage_counts_items_nobody_but_the_ceo_can_see(safe, fake_bigquery):
    import pandas as pd

    fake_bigquery.add("COUNTIF", df=pd.DataFrame({"items": [1000], "without_product": [7], "without_brand": [3]}))
    coverage = brand_coverage(safe)
    assert (coverage.items, coverage.without_product, coverage.without_brand, coverage.invisible_to_managers) == (1000, 7, 3, 10)


# startup through the CLI -------------------------------------------------------------------------

def chat(config_path, user="alice"):
    return runner.invoke(app, ["chat", "--user", user, "--config", str(config_path)], input="/quit\n")


def conversations(project) -> int:
    path = project / "data" / "db.sqlite3"
    if not path.exists():
        return 0
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    finally:
        conn.close()


def test_a_chat_starts_when_every_brand_matches(project, config_path):
    result = chat(config_path)
    assert result.exit_code == 0, result.output
    assert "You can see brands: Brand A, Brand B" in result.output


def test_the_session_carries_the_catalog_for_the_agent(project, config_path):
    session = cli_main.open_session(Renderer(), "alice", config_path, debug=False, chaos=None, chaos_seed=None, conversation=None)
    try:
        assert session.catalog is not None and session.catalog.brands == BRANDS
    finally:
        session.db.close()


def test_a_typo_stops_startup_with_exit_2_and_no_conversation_and_names_no_other_brand(project, config_path):
    (project / "users.yaml").write_text(
        'users:\n  alice: {name: Alice, role: brand_manager, brands: ["levis"]}\n  ceo: {name: CEO, role: ceo, all_brands: true}\n',
        encoding="utf-8",
    )
    result = chat(config_path)
    assert result.exit_code == 2
    assert 'alice: "levis" is not a brand in the data.' in result.output
    assert "Fix:" in result.output and "administrator can run" in result.output and conversations(project) == 0
    # The person starting the chat is a brand manager: other managers' brand names are not for them.
    assert "Did you mean" not in result.output
    for other in DISTINCTIVE:
        assert other not in result.output.replace("levis", ""), other


def test_one_users_typo_does_not_take_the_chat_offline_for_the_others(project, config_path):
    (project / "users.yaml").write_text(
        'users:\n  alice: {name: Alice, role: brand_manager, brands: ["levis"]}\n'
        '  bob: {name: Bob, role: brand_manager, brands: ["Nike"]}\n  ceo: {name: CEO, role: ceo, all_brands: true}\n',
        encoding="utf-8",
    )
    assert chat(config_path, user="ceo").exit_code == 0
    assert chat(config_path, user="bob").exit_code == 0
    refused = chat(config_path, user="alice")
    assert refused.exit_code == 2 and 'alice: "levis"' in refused.output and "bob" not in refused.output


def test_the_shipped_placeholders_refuse_to_start_for_the_user_who_has_them(project, config_path):
    (project / "users.yaml").write_text(
        'users:\n  alice: {name: Alice, role: brand_manager, brands: ["<verified brand 1>"]}\n  ceo: {name: C, role: ceo, all_brands: true}\n',
        encoding="utf-8",
    )
    result = chat(config_path)
    assert result.exit_code == 2 and "is still a placeholder" in result.output
    assert chat(config_path, user="ceo").exit_code == 0


def test_a_user_without_brands_is_refused_before_any_bigquery_call(project, config_path, fake_bigquery):
    result = chat(config_path, user="nobrands")
    assert result.exit_code == 2 and "No brands are assigned" in result.output
    assert fake_bigquery.jobs == [] and fake_bigquery.table_calls == []


def stale_cache(project, fake_bigquery):
    """A saved copy that is three days old, then BigQuery goes down."""
    settings = load_settings(project / "config.yaml", load_env_file=False)
    load_catalog(settings, now=datetime.now(timezone.utc) - timedelta(days=3))
    fake_bigquery.down = True


def test_bigquery_down_with_a_saved_copy_starts_and_says_so(project, config_path, fake_bigquery):
    stale_cache(project, fake_bigquery)
    result = chat(config_path)
    assert result.exit_code == 0, result.output
    assert "BigQuery could not be reached" in result.output and "is used" in result.output
    assert "You can see brands" in result.output


def test_a_typo_is_still_caught_from_the_saved_copy(project, config_path, fake_bigquery):
    stale_cache(project, fake_bigquery)
    (project / "users.yaml").write_text('users:\n  alice: {name: A, role: brand_manager, brands: ["levis"]}\n', encoding="utf-8")
    result = chat(config_path)
    assert result.exit_code == 2 and 'alice: "levis"' in result.output and "Did you mean" not in result.output


def test_bigquery_down_with_no_saved_copy_refuses_to_start(project, config_path, fake_bigquery):
    fake_bigquery.down = True
    result = chat(config_path)
    assert result.exit_code == 2
    assert "no saved schema" in result.output and "Fix:" in result.output
    assert conversations(project) == 0


# the problems are data, and suggestions are optional -------------------------------------------------------------

def test_the_problems_come_back_as_a_list_and_a_newline_in_a_brand_does_not_split_one_into_two():
    users = directory(alice=["Nike\nAir", "levis"])
    with pytest.raises(BrandMappingError) as err:
        check_brand_mapping(users, catalog_with(BRANDS))
    assert len(err.value.problems) == 2 and err.value.problems[0].startswith('alice: "Nike\nAir"')
    assert err.value.message.startswith("users.yaml has brands that do not match the data:\n")
    assert isinstance(err.value, ConfigError)  # callers that catch ConfigError still work


def test_without_names_nothing_from_the_real_list_appears_in_the_message():
    with pytest.raises(BrandMappingError) as err:
        check_brand_mapping(directory(alice=["levis", "nike"]), catalog_with(BRANDS), suggest_names=False)
    text = err.value.message + (err.value.fix or "")
    assert "Did you mean" not in text
    for real in DISTINCTIVE:
        assert real not in text, real
    assert "administrator" in err.value.fix


def test_the_error_survives_a_copy_and_a_pickle():
    import copy
    import pickle

    original = BrandMappingError(['alice: "x" is not a brand in the data.'], "fix it")
    for clone in (copy.deepcopy(original), pickle.loads(pickle.dumps(original))):
        assert isinstance(clone, BrandMappingError) and clone.problems == original.problems and clone.fix == "fix it"


# order: cheap local checks come before the network ---------------------------------------------------------------

def test_a_wrong_conversation_is_reported_without_waiting_for_bigquery(project, config_path, fake_bigquery):
    result = runner.invoke(app, ["chat", "--user", "alice", "--config", str(config_path), "--conversation", "typo123"], input="/quit\n")
    assert result.exit_code == 2 and "Conversation 'typo123' was not found" in result.output
    assert fake_bigquery.jobs == [] and fake_bigquery.table_calls == []  # nothing was asked of BigQuery


def test_a_failed_brand_check_leaves_no_conversation_even_though_the_database_was_opened(project, config_path):
    (project / "users.yaml").write_text('users:\n  alice: {name: A, role: brand_manager, brands: ["levis"]}\n', encoding="utf-8")
    assert chat(config_path).exit_code == 2
    assert (project / "data" / "db.sqlite3").exists() and conversations(project) == 0
