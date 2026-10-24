"""The glossary and the startup loader of the Golden Bucket (plan 10)."""
import logging

import pytest

from src.agent.prompts.locked_rules import LOCKED_DEFINITIONS
from src.config import load_settings
from src.golden.glossary import MAX_DEFINITIONS, Glossary, GlossaryEntry, load_glossary
from src.golden.load import open_golden
from src.guards.allowlist import load_allowlist
from tests.golden_support import REPO_ROOT, SEED_DIR, write_trio

ALL_BRANDS = frozenset({"Maple & Co", "Nordic Peak", "O'Hara Outfitters"})


def entry(term, definition="A definition that is long enough to count.", **options):
    return GlossaryEntry(term=term, definition=definition, **options)


# lookup -------------------------------------------------------------------------------------------------------------------

def test_a_term_or_an_alias_in_the_question_gives_its_definition_in_the_form_term_colon_definition():
    glossary = Glossary((entry("average order value", aliases=["aov", "average basket"]),))
    assert glossary.lookup("What is our AOV?") == ["average order value: A definition that is long enough to count."]
    assert glossary.lookup("Show the Average   Order\nValue by month") != []
    assert glossary.lookup("What is the average basket?") != []


def test_only_whole_words_match():
    glossary = Glossary((entry("churn"), entry("aov")))
    assert glossary.lookup("Why is churning customers' revenue down?") == []
    assert glossary.lookup("the aovs of last year") == [] and glossary.lookup("Churn, by region") != []


def test_a_locked_entry_is_never_returned_because_the_code_already_says_it():
    assert Glossary((entry("churn", locked=True),)).lookup("what is churn") == []


def test_definitions_come_in_file_order_and_at_most_five():
    glossary = Glossary(tuple(entry(f"term{n}") for n in range(1, 9)))
    found = glossary.lookup(" ".join(f"term{n}" for n in range(8, 0, -1)))
    assert [line.split(":")[0] for line in found] == [f"term{n}" for n in range(1, MAX_DEFINITIONS + 1)]


def test_a_question_without_a_term_finds_nothing_and_regex_characters_are_plain_text():
    glossary = Glossary((entry("a.b+c", aliases=["(x)"]),))
    assert glossary.lookup("what is revenue?") == [] and glossary.lookup("aXb+c") == []
    assert glossary.lookup("what is a.b+c here") != [] and glossary.lookup("see (x) there") != []


# the file ------------------------------------------------------------------------------------------------------------------

def test_the_shipped_glossary_loads_clean():
    glossary, problems = load_glossary(REPO_ROOT / "glossary.yaml")
    assert problems == []
    terms = [e.term for e in glossary.entries]
    assert {"churn", "last month", "revenue", "active customer", "average order value", "repeat customer", "top customers",
            "underspending", "return rate"} <= set(terms)


def test_a_locked_entry_is_one_the_code_really_has():
    """`locked: true` is a promise that locked_rules.py says it. A locked entry the code does not have would silently never be sent."""
    glossary, _ = load_glossary(REPO_ROOT / "glossary.yaml")
    keywords = {word for words, _ in LOCKED_DEFINITIONS for word in words}
    locked = [e for e in glossary.entries if e.locked]
    assert {e.term for e in locked} == {"churn", "last month"}
    assert all(e.term in keywords for e in locked)


def test_the_shipped_glossary_gives_the_underspending_reading_when_asked():
    glossary, _ = load_glossary(REPO_ROOT / "glossary.yaml")
    (line,) = glossary.lookup("Which state is underspending?")
    assert "below the comparison group's average" in line and "states that this is the reading used" in line


def test_a_missing_file_is_an_empty_glossary_with_a_problem(tmp_path):
    glossary, problems = load_glossary(tmp_path / "glossary.yaml")
    assert glossary.entries == () and "was not found" in problems[0]


@pytest.mark.parametrize("text,fragment", [
    ("term: churn\n", "must be a list"), ("- [unclosed", "glossary.yaml line"), ("", "must be a list"),
])
def test_a_file_that_is_not_a_list_of_entries_is_an_empty_glossary_with_a_problem(tmp_path, text, fragment):
    path = tmp_path / "glossary.yaml"
    path.write_text(text, encoding="utf-8")
    glossary, problems = load_glossary(path)
    assert glossary.entries == () and fragment in problems[0]


def test_one_bad_entry_is_skipped_and_the_rest_are_kept(tmp_path):
    path = tmp_path / "glossary.yaml"
    path.write_text(
        "- term: revenue\n  definition: Revenue is the sum of item sale prices.\n"
        "- term: x\n  definition: too short a term\n"
        "- term: churn\n  definition: no order in 90 days\n  surprise: true\n"
        "- term: Revenue\n  definition: The same term again, in another case.\n"
        "- term: margin\n  definition: Margin is the sale price minus the cost.\n",
        encoding="utf-8",
    )
    glossary, problems = load_glossary(path)
    assert [e.term for e in glossary.entries] == ["revenue", "margin"]
    assert len(problems) == 3 and "defined twice" in problems[2]


# the loader ----------------------------------------------------------------------------------------------------------------

@pytest.fixture
def settings(project):
    (project / "config.yaml").write_text(f'golden:\n  enabled: true\n  seed_dir: "{SEED_DIR.as_posix()}"\n  glossary_file: "{(REPO_ROOT / "glossary.yaml").as_posix()}"\n', encoding="utf-8")
    return load_settings(project / "config.yaml", load_env_file=False)


def test_every_seed_trio_loads_when_the_brands_are_in_the_data(settings):
    bundle = open_golden(settings, load_allowlist(settings), ALL_BRANDS)
    assert bundle.skipped == 0 and bundle.warnings == () and len(bundle.index) == 10  # 12 files, 10 approved
    assert len(bundle.glossary.entries) == 9


def test_trios_of_brands_that_are_not_in_the_data_are_left_out_and_named_in_the_log(settings, caplog):
    """Running on the live dataset: the two brand-specific seed trios name brands of the sample data."""
    with caplog.at_level(logging.WARNING, logger="src.golden.load"):
        bundle = open_golden(settings, load_allowlist(settings), frozenset({"Nike", "Levi's"}))
    assert bundle.skipped == 2 and len(bundle.index) == 8
    assert "2 of 12 Golden Bucket trio(s) were not loaded" in bundle.warnings[0] and "golden validate" in bundle.warnings[0]
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "golden trio g-009 is not loaded: Brands" in logged and "g-010" in logged


def test_a_trio_that_fails_the_sql_guard_is_not_loaded_even_when_its_file_is_fine(settings, tmp_path):
    folder = tmp_path / "trios"
    folder.mkdir()
    write_trio(folder, "g-100")
    write_trio(folder, "g-101", id="g-101", question="What do the customers say in their e-mail?", sql="SELECT u.email FROM users u", tables=["users"])
    (tmp_path / "config.yaml").write_text(f'golden:\n  enabled: true\n  seed_dir: "{folder.as_posix()}"\n', encoding="utf-8")
    settings = load_settings(tmp_path / "config.yaml", load_env_file=False)
    bundle = open_golden(settings, load_allowlist(settings), ALL_BRANDS)
    assert bundle.skipped == 1 and [t.id for t in bundle.index.trios] == ["g-100"]


def test_a_missing_seed_folder_is_a_warning_and_an_empty_index_not_a_failure(project):
    (project / "config.yaml").write_text("golden:\n  enabled: true\n  seed_dir: nowhere\n  glossary_file: nothing.yaml\n", encoding="utf-8")
    settings = load_settings(project / "config.yaml", load_env_file=False)
    bundle = open_golden(settings, load_allowlist(settings), ALL_BRANDS)
    assert len(bundle.index) == 0 and any("folder was not found" in w for w in bundle.warnings)
    assert any("glossary has problems" in w for w in bundle.warnings)


def test_the_settings_of_the_index_come_from_the_config(project):
    (project / "config.yaml").write_text(f'golden:\n  enabled: true\n  top_k: 1\n  min_similarity: 0.5\n  seed_dir: "{SEED_DIR.as_posix()}"\n', encoding="utf-8")
    settings = load_settings(project / "config.yaml", load_env_file=False)
    bundle = open_golden(settings, load_allowlist(settings), ALL_BRANDS)
    assert bundle.index.top_k == 1 and bundle.index.min_similarity == 0.5
