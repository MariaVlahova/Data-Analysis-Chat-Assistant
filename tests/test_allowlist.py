"""The schema allow-list (plan 05): loading, validation, the hand-off to plan 03's catalog and the `doctor` check."""
import re
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from src.cli.main import app
from src.config import Settings, load_settings
from src.data_access.catalog import TABLES, load_catalog
from src.errors import ConfigError
from src.guards.allowlist import ALLOWLIST_FILE_NAME, REQUIRED_TABLES, SCOPE_COLUMNS, Allowlist, fold, load_allowlist
from tests.guard_support import BLOCKED, REPO_ROOT, make_allowlist
from tests.fake_bigquery import FAKE_COLUMNS, make_table

runner = CliRunner(env={"COLUMNS": "250"})


def text() -> dict:
    return yaml.safe_load((REPO_ROOT / ALLOWLIST_FILE_NAME).read_text(encoding="utf-8"))


def write(tmp_path: Path, data: dict | str) -> Path:
    path = tmp_path / ALLOWLIST_FILE_NAME
    path.write_text(data if isinstance(data, str) else yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def load(tmp_path: Path, data: dict | str) -> Allowlist:
    return load_allowlist(Settings(), write(tmp_path, data))


def problem_with(tmp_path: Path, data: dict | str) -> str:
    with pytest.raises(ConfigError) as err:
        load(tmp_path, data)
    return err.value.message


# the shipped file -----------------------------------------------------------------------------------------------

def test_the_shipped_file_loads_and_has_the_four_tables():
    allowlist = make_allowlist()
    assert sorted(allowlist.table_names()) == sorted(REQUIRED_TABLES) and TABLES is REQUIRED_TABLES  # one list of the four
    assert allowlist.dataset == "bigquery-public-data.thelook_ecommerce"


def test_direct_identifiers_are_blocked_and_never_allowed():
    allowlist = make_allowlist()
    assert set(allowlist.blocked("users")) == set(BLOCKED)
    assert not set(allowlist.blocked("users")) & set(allowlist.allowed("users"))
    assert allowlist.blocked_names == frozenset(BLOCKED)


def test_every_allowed_column_has_a_description_and_nothing_else_does():
    data = text()
    for table, entry in data["tables"].items():
        assert set(data["descriptions"][table]) == set(entry["allowed"]), table


def test_the_default_pii_split_is_the_one_in_the_plan():
    allowlist = make_allowlist()
    # created_at is the sign-up timestamp: unique per customer, so it would stand in for the customer ID (code review)
    assert set(allowlist.quasi_identifiers("users")) == {"age", "gender", "state", "city", "country", "traffic_source", "created_at"}
    assert allowlist.quasi_identifiers("orders") == ["gender"]
    assert allowlist.quasi_identifiers("order_items") == [] and allowlist.quasi_identifiers("products") == []
    assert {(t, c) for t, c in make_allowlist().customer_id_columns} == {("users", "id"), ("orders", "user_id"), ("order_items", "user_id")}


def test_it_satisfies_the_catalogs_column_policy_and_an_unknown_table_has_no_columns():
    allowlist = make_allowlist()
    assert allowlist.allowed("payments") == [] and allowlist.blocked("payments") == []
    assert allowlist.allowed("USERS") == allowlist.allowed("users")  # table names match without regard to case
    assert allowlist.describe("products", "brand") == "brand name"
    assert allowlist.describe("products", "no_such_column") == ""


def test_names_are_compared_after_normalizing_case_and_width():
    assert fold("EMAIL") == fold("email") == fold("ｅｍａｉｌ")  # full-width letters
    allowlist = make_allowlist()
    assert ("orders", "user_id") in allowlist.customer_id_set and ("users", "age") in allowlist.quasi_identifier_set
    assert allowlist.blocked_names >= {fold(name) for name in BLOCKED}


def test_the_scope_columns_are_all_allowed_in_the_shipped_file():
    allowlist = make_allowlist()
    for table, columns in SCOPE_COLUMNS.items():
        assert set(columns) <= set(allowlist.allowed(table)), table


# validation -----------------------------------------------------------------------------------------------------

def test_a_column_both_allowed_and_blocked_is_refused(tmp_path):
    data = text()
    data["tables"]["users"]["blocked"].append("age")
    assert "users.age is both allowed and blocked" in problem_with(tmp_path, data)


def test_a_name_blocked_in_one_table_and_allowed_in_another_is_refused(tmp_path):
    """The SQL Guard blocks a name wherever it appears, so the second table's column could never be used."""
    data = text()
    data["tables"]["products"]["allowed"].append("email")
    assert "allowed, but the same name is blocked in another table" in problem_with(tmp_path, data)


def test_a_quasi_identifier_that_is_not_allowed_is_refused(tmp_path):
    data = text()
    data["tables"]["orders"]["quasi_identifiers"] = ["gender", "ghost"]
    assert "orders.ghost is a quasi-identifier but not allowed" in problem_with(tmp_path, data)


def test_a_missing_table_and_an_extra_table_are_refused(tmp_path):
    data = text()
    del data["tables"]["products"]
    del data["descriptions"]["products"]
    data["tables"]["payments"] = {"allowed": ["id"]}
    message = problem_with(tmp_path, data)
    assert "table products is missing" in message and "table payments is not one of" in message


def test_a_scope_column_that_is_not_allowed_is_refused(tmp_path):
    data = text()
    data["tables"]["products"]["allowed"].remove("brand")
    del data["descriptions"]["products"]["brand"]
    assert "products.brand must be allowed" in problem_with(tmp_path, data)


def test_a_customer_id_column_that_is_not_allowed_is_refused(tmp_path):
    data = text()
    data["customer_id_columns"].append("products.id_of_nobody")
    assert "products.id_of_nobody, which is not an allowed column" in problem_with(tmp_path, data)


def test_a_description_for_a_column_that_is_not_allowed_is_refused(tmp_path):
    data = text()
    data["descriptions"]["users"]["email"] = "the email address"
    assert "descriptions names users.email" in problem_with(tmp_path, data)


def test_a_duplicate_column_is_refused_even_in_another_case(tmp_path):
    data = text()
    data["tables"]["users"]["allowed"].append("AGE")
    assert "users.allowed lists a column twice" in problem_with(tmp_path, data)


def test_the_file_must_name_the_configured_dataset(tmp_path):
    data = text()
    data["dataset"] = "some-project.another_dataset"
    assert "bigquery.dataset is bigquery-public-data.thelook_ecommerce" in problem_with(tmp_path, data)


def test_an_unknown_key_and_a_bad_identifier_name_the_file_and_the_key(tmp_path):
    data = text()
    data["tables"]["users"]["alowed"] = ["id"]
    message = problem_with(tmp_path, data)
    assert ALLOWLIST_FILE_NAME in message and "alowed" in message and "unknown key" in message
    data = text()
    data["tables"]["users"]["allowed"].append("drop table; --")
    assert "tables.users.allowed.8" in problem_with(tmp_path, data)  # a name is letters, digits and underscores only


def test_no_customer_id_columns_is_refused(tmp_path):
    data = text()
    data["customer_id_columns"] = []
    assert "customer_id_columns" in problem_with(tmp_path, data)


def test_a_missing_file_is_a_config_error_with_a_fix(tmp_path):
    with pytest.raises(ConfigError) as err:
        load_allowlist(Settings(), tmp_path / ALLOWLIST_FILE_NAME)
    assert "was not found" in err.value.message and err.value.fix


def test_a_repeated_key_is_refused_instead_of_keeping_the_last(tmp_path):
    """Two `blocked:` lists under one table would otherwise silently drop the first one."""
    raw = (REPO_ROOT / ALLOWLIST_FILE_NAME).read_text(encoding="utf-8").replace(
        "    blocked: [first_name", "    blocked: [email]\n    blocked: [first_name", 1
    )
    with pytest.raises(ConfigError) as err:
        load(tmp_path, raw)
    assert "duplicate key" in err.value.message


def test_a_utf16_file_is_refused_with_a_clear_message(tmp_path):
    path = tmp_path / ALLOWLIST_FILE_NAME
    path.write_bytes((REPO_ROOT / ALLOWLIST_FILE_NAME).read_text(encoding="utf-8").encode("utf-16"))
    with pytest.raises(ConfigError) as err:
        load_allowlist(Settings(), path)
    assert "UTF-8" in err.value.message


# plan 03's catalog ----------------------------------------------------------------------------------------------

@pytest.fixture
def settings(project):
    return load_settings(project / "config.yaml", load_env_file=False)


def test_the_catalog_accepts_the_shipped_file_against_the_canned_dataset_and_shows_no_blocked_column(settings, fake_bigquery):
    catalog = load_catalog(settings).catalog.with_policy(make_allowlist())
    assert catalog.reconcile(make_allowlist()) == []  # every live column is classified
    overview = catalog.overview()
    for column in BLOCKED:
        assert column not in overview
    assert "num_of_item" in overview and "number of items in the order" in overview
    assert catalog.prune(["users"]).startswith("users(id INT64, age INT64")


def test_an_allowed_column_missing_live_stops_startup(settings, fake_bigquery):
    live = {name: [c for c in columns if c[0] != "sale_price"] for name, columns in FAKE_COLUMNS.items()}
    fake_bigquery.tables = {name: make_table(name, columns, 1_000) for name, columns in live.items()}
    with pytest.raises(ConfigError) as err:
        load_catalog(settings).catalog.with_policy(make_allowlist())
    assert "order_items.sale_price" in err.value.message


def test_a_new_live_column_is_hidden_from_the_agent_and_reported(settings, fake_bigquery, caplog):
    fake_bigquery.tables["users"] = make_table("users", FAKE_COLUMNS["users"] + [("loyalty_tier", "STRING")], 1_000)
    catalog = load_catalog(settings).catalog
    with caplog.at_level("WARNING"):
        attached = catalog.with_policy(make_allowlist())
    assert "loyalty_tier" not in attached.overview() and "loyalty_tier" not in attached.prune(["users"])
    assert catalog.reconcile(make_allowlist()) == ["users.loyalty_tier"]
    assert "users.loyalty_tier" in caplog.text


# doctor ---------------------------------------------------------------------------------------------------------

@pytest.fixture
def ready(project, monkeypatch):
    import google.auth

    (project / "users.yaml").write_text(
        'users:\n  alice: {name: Alice, role: brand_manager, brands: ["Brand A", "Brand B"]}\n  ceo: {name: CEO, role: ceo, all_brands: true}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(google.auth, "default", lambda *a, **k: (object(), "adc-project"))
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")


def doctor(project, *extra):
    return runner.invoke(app, ["doctor", "--config", str(project / "config.yaml"), *extra])


def line(output: str, name: str) -> str:
    return next(row for row in output.splitlines() if re.match(rf"\[.{{4}}\] {re.escape(name)}", row))


def test_doctor_passes_the_shipped_file(project, ready):
    result = doctor(project)
    assert result.exit_code == 0, result.output
    assert "[ ok ] Column allow-list (Q4, Q11): 4 tables, 37 allowed columns, 8 blocked, 8 quasi-identifiers; every allowed column exists live" in result.output


def test_doctor_fails_on_an_invalid_file_and_names_the_problem(project, ready):
    data = text()
    data["tables"]["users"]["blocked"].append("age")
    write(project, data)
    result = doctor(project)
    assert result.exit_code == 1
    assert line(result.output, "Column allow-list").startswith("[FAIL]") and "users.age is both allowed and blocked" in result.output


def test_doctor_fails_when_an_allowed_column_is_not_in_bigquery(project, ready, fake_bigquery):
    fake_bigquery.tables["products"] = make_table("products", [c for c in FAKE_COLUMNS["products"] if c[0] != "sku"], 1_000)
    result = doctor(project)
    assert result.exit_code == 1 and "products.sku" in line(result.output, "Column allow-list")


def test_doctor_warns_about_a_live_column_in_neither_list(project, ready, fake_bigquery):
    fake_bigquery.tables["orders"] = make_table("orders", FAKE_COLUMNS["orders"] + [("coupon_code", "STRING")], 1_000)
    result = doctor(project)
    row = line(result.output, "Column allow-list")
    assert row.startswith("[warn]") and "orders.coupon_code" in row and "hidden from the agent" in row


def test_doctor_with_bigquery_unreachable_still_checks_the_file(project, ready, monkeypatch):
    from google.auth.exceptions import DefaultCredentialsError

    def no_credentials(settings):
        raise DefaultCredentialsError("none")

    monkeypatch.setattr("src.data_access.safe_runner.make_runner", no_credentials)
    row = line(doctor(project).output, "Column allow-list")
    assert row.startswith("[warn]") and "4 tables" in row and "not compared with BigQuery" in row


# the chat -------------------------------------------------------------------------------------------------------

def test_the_chat_does_not_start_without_a_valid_allow_list(project, ready):
    (project / ALLOWLIST_FILE_NAME).unlink()
    result = runner.invoke(app, ["chat", "--user", "alice", "--config", str(project / "config.yaml")], input="/quit\n")
    assert result.exit_code == 2 and "allow-list" in result.output and "was not found" in result.output


def test_the_chat_session_catalog_lists_only_allowed_columns(project, ready):
    from src.cli.main import open_session
    from src.cli.render import Renderer

    session = open_session(Renderer(), "alice", project / "config.yaml", debug=False, chaos=None, chaos_seed=None, conversation=None)
    try:
        overview = session.catalog.overview()
        assert "email" not in overview and "first_name" not in overview and "brand" in overview
    finally:
        session.db.close()


# the review flag (code review, finding 13) -----------------------------------------------------------------------------
# The file was written from our knowledge of the dataset, not read from the live schema (Q4). Nothing in it can prove it is
# right, so a person says so with `reviewed_against_live: true`, and until then the tools say it is not.

def set_review_flag(project, value: bool) -> None:
    path = project / ALLOWLIST_FILE_NAME
    flag = re.compile(r"^reviewed_against_live: (true|false)$", re.MULTILINE)
    path.write_text(flag.sub(f"reviewed_against_live: {str(value).lower()}", path.read_text(encoding="utf-8")), encoding="utf-8")


def test_the_shipped_file_carries_the_flag_and_says_how_to_set_it():
    """Not its value: that changes the day a person has done the review, and nothing else should break then."""
    shipped = (REPO_ROOT / ALLOWLIST_FILE_NAME).read_text(encoding="utf-8")
    assert re.search(r"^reviewed_against_live: (true|false)$", shipped, re.MULTILINE) and "doctor --refresh" in shipped
    assert isinstance(make_allowlist().reviewed_against_live, bool)


def test_a_loaded_allow_list_carries_the_flag(tmp_path):
    data = text()
    data["reviewed_against_live"] = True
    assert load(tmp_path, data).reviewed_against_live is True
    del data["reviewed_against_live"]
    assert load(tmp_path, data).reviewed_against_live is False  # unreviewed unless someone says otherwise


def test_a_flag_that_is_not_true_or_false_is_refused(tmp_path):
    data = text()
    data["reviewed_against_live"] = "mostly"
    assert "reviewed_against_live" in problem_with(tmp_path, data)


def test_doctor_warns_while_the_allow_list_is_unreviewed_even_when_every_column_matches(project, ready):
    set_review_flag(project, False)
    result = doctor(project)
    row = line(result.output, "Column allow-list")
    assert row.startswith("[warn]") and "NOT yet reviewed" in row and "every allowed column exists live" in row
    assert "reviewed_against_live: true" in result.output  # the fix says what to set
    assert result.exit_code == 0 and "All checks passed." not in result.output


def test_doctor_is_quiet_once_it_is_marked_reviewed(project, ready):
    set_review_flag(project, True)
    result = doctor(project)
    assert line(result.output, "Column allow-list").startswith("[ ok ]") and "All checks passed." in result.output


def test_doctor_still_fails_on_a_wrong_file_whatever_the_flag_says(project, ready, fake_bigquery):
    set_review_flag(project, True)
    fake_bigquery.tables["products"] = make_table("products", [c for c in FAKE_COLUMNS["products"] if c[0] != "sku"], 1_000)
    assert line(doctor(project).output, "Column allow-list").startswith("[FAIL]")


def start_chat(project):
    from src.cli.main import open_session
    from src.cli.render import Renderer

    session = open_session(Renderer(), "alice", project / "config.yaml", debug=False, chaos=None, chaos_seed=None, conversation=None)
    session.db.close()
    return session


def test_the_chat_does_not_start_while_the_allow_list_is_unreviewed(project, ready):
    """The file was written from knowledge of the dataset. A column under the wrong heading would weaken the privacy rules
    with no error, so by default a person must say it was checked before the chat opens."""
    set_review_flag(project, False)
    with pytest.raises(ConfigError) as err:
        start_chat(project)
    assert "not been reviewed" in err.value.message
    assert "doctor --refresh" in err.value.fix and "reviewed_against_live: true" in err.value.fix and "require_reviewed_allowlist" in err.value.fix


def test_the_chat_command_exits_with_the_startup_code_and_the_fix_while_it_is_unreviewed(project, ready):
    set_review_flag(project, False)
    result = runner.invoke(app, ["chat", "--user", "alice", "--config", str(project / "config.yaml")], input="/quit\n")
    assert result.exit_code == 2 and "reviewed against the live schema" in result.output and "reviewed_against_live: true" in result.output


def test_a_stopped_chat_leaves_no_conversation_behind(project, ready):
    import sqlite3

    set_review_flag(project, False)
    with pytest.raises(ConfigError):
        start_chat(project)
    with sqlite3.connect(project / "data" / "db.sqlite3") as conn:
        assert conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0
    set_review_flag(project, True)
    start_chat(project)
    with sqlite3.connect(project / "data" / "db.sqlite3") as conn:
        assert conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1  # the same query does count a real one


def test_the_owner_can_choose_to_start_anyway_and_the_chat_logs_a_warning(project, ready):
    from src.logging_setup import shutdown_logging

    (project / "config.yaml").write_text("guards:\n  require_reviewed_allowlist: false\n", encoding="utf-8")
    set_review_flag(project, False)
    log_file = project / "data" / "logs" / "app.log"
    start_chat(project)
    shutdown_logging()
    assert "not been marked as reviewed" in log_file.read_text(encoding="utf-8")


def test_a_reviewed_allow_list_starts_the_chat_without_a_warning(project, ready):
    from src.logging_setup import shutdown_logging

    set_review_flag(project, True)
    log_file = project / "data" / "logs" / "app.log"
    log_file.unlink(missing_ok=True)
    start_chat(project)
    shutdown_logging()
    assert "not been marked as reviewed" not in log_file.read_text(encoding="utf-8")
