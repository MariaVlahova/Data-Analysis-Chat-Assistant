"""The whole app on the generated sample data, with a scripted model (plans 03 to 10 together; no network, no keys).

What the offline suite could not show before: the SQL the guard writes, running on real tables. Every figure the model is allowed to
say comes from DuckDB read directly (the truth), so a wrong scope, a wrong rewrite or a leak shows as a difference. The model's replies
are scripted; they are the part this file does not test.
"""
import json
import re
import shutil

import duckdb
import pytest
from typer.testing import CliRunner

from src.cli.main import app
from src.config import load_settings
from src.data_access.local_bq import LocalBigQueryClient
from src.golden.load import open_golden
from src.guards.allowlist import load_allowlist
from src.guards.sql_guard import SQLGuard
from src.llm.routing import Step
from tests.agent_support import REVENUE_SQL, composer_reply, guard_reply, make_rig, sql_reply
from tests.golden_support import CEO, MAPLE, OHARA, REPO_ROOT, SEED_DIR

runner = CliRunner(env={"COLUMNS": "250"})

REVENUE = ("SELECT p.brand, SUM(oi.sale_price) AS revenue FROM order_items oi JOIN products p ON p.id = oi.product_id "
           "WHERE oi.status NOT IN ('Cancelled', 'Returned') GROUP BY p.brand ORDER BY revenue DESC")
TOTAL = "SELECT SUM(oi.sale_price) AS revenue FROM order_items oi WHERE oi.status NOT IN ('Cancelled', 'Returned')"
TOP_CUSTOMERS = ("SELECT oi.user_id AS customer_id, SUM(oi.sale_price) AS total_spend, COUNT(DISTINCT oi.order_id) AS orders FROM order_items oi "
                 "WHERE oi.status NOT IN ('Cancelled', 'Returned') GROUP BY oi.user_id ORDER BY total_spend DESC LIMIT 20")


@pytest.fixture
def truth(local_db):
    """Run SQL on the file directly, outside the app: what the right answer is."""
    def ask(sql, *args):
        connection = duckdb.connect(str(local_db.path), read_only=True)
        try:
            connection.execute("SET TimeZone = 'UTC'")
            return connection.execute(sql, list(args)).fetchall()
        finally:
            connection.close()

    return ask


@pytest.fixture
def engine(monkeypatch):
    """Every query the app sends to the stand-in, as (sql, was a dry run, parameter names)."""
    calls = []
    original = LocalBigQueryClient.query

    def spy(self, sql, job_config=None, **kwargs):
        calls.append((sql, bool(job_config and job_config.dry_run), [p.name for p in (getattr(job_config, "query_parameters", None) or [])]))
        return original(self, sql, job_config, **kwargs)

    monkeypatch.setattr(LocalBigQueryClient, "query", spy)
    return calls


def real_jobs(engine):
    return [call for call in engine if not call[1]]


def rig_for(local_project, fake_bigquery, user, **options):
    return make_rig(local_project, user, fake_bigquery=fake_bigquery, **options)


def script(rig, sql, body="", question="a question"):
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply(standalone=question))
    rig.gateway.queue(Step.SQL_WRITE, sql_reply(sql))
    if body:
        rig.gateway.queue(Step.COMPOSER, body if callable(body) else composer_reply(body))


def composer_prompt(rig) -> str:
    call = rig.gateway.calls_for(Step.COMPOSER)[0]
    return call.system + call.contents


def everything_the_model_saw(rig) -> str:
    return " ".join(call.system + str(call.contents) for call in rig.gateway.calls)


# the right figures, for the right brands -----------------------------------------------------------------------------------------

def test_a_brand_manager_gets_the_real_revenue_of_their_brands_and_no_other_brand(local_project, fake_bigquery, truth, engine):
    rig = rig_for(local_project, fake_bigquery, MAPLE)
    expected = truth(
        "SELECT p.brand, SUM(oi.sale_price) FROM order_items oi JOIN products p ON p.id = oi.product_id "
        "WHERE p.brand IN ('Maple & Co', 'Nordic Peak') AND oi.status NOT IN ('Cancelled', 'Returned') GROUP BY p.brand ORDER BY 2 DESC"
    )
    body = "; ".join(f"{brand} earned {value:,.2f}" for brand, value in expected) + "."
    script(rig, REVENUE, body)
    engine.clear()
    result = rig.ask("What is the revenue of each of my brands?")
    assert result.outcome == "answered", result.text
    for brand, value in expected:
        assert f"{brand} earned {value:,.2f}" in result.text  # the numeric check kept every figure: each is in the results
    assert "your brands: Maple & Co, Nordic Peak" in result.text
    prompt = composer_prompt(rig)
    assert all(other not in prompt for other in ("O'Hara", "Urban Thread", "Blue Harbor", "Zephyr Sport", "Casa Lino", "Wren"))
    (query,) = real_jobs(engine)
    assert query[2] == ["brands"] and "Maple" not in query[0]  # the brands travel as a parameter, never in the text


def test_another_manager_asking_the_same_question_gets_a_disjoint_answer(local_project, fake_bigquery, truth):
    alice = rig_for(local_project, fake_bigquery, MAPLE)
    bob = rig_for(local_project, fake_bigquery, OHARA)
    (bob_brand, bob_value), = truth(
        "SELECT p.brand, SUM(oi.sale_price) FROM order_items oi JOIN products p ON p.id = oi.product_id "
        "WHERE p.brand = 'O''Hara Outfitters' AND oi.status NOT IN ('Cancelled', 'Returned') GROUP BY p.brand"
    )
    script(alice, REVENUE, "Revenue is shown by brand.")
    script(bob, REVENUE, f"{bob_brand} earned {bob_value:,.2f}.")
    alice.ask("revenue by brand")
    result = bob.ask("revenue by brand")
    assert result.outcome == "answered" and f"{bob_brand} earned {bob_value:,.2f}" in result.text
    assert "Maple" not in composer_prompt(bob) and "Nordic" not in composer_prompt(bob)
    assert "O'Hara" not in composer_prompt(alice)


def test_a_manager_total_is_below_the_ceo_total_and_each_answer_says_its_scope(local_project, fake_bigquery, truth):
    (ceo_total,), = truth(TOTAL.replace("SELECT SUM(oi.sale_price) AS revenue FROM order_items oi", "SELECT SUM(oi.sale_price) FROM order_items oi"))
    (alice_total,), = truth(
        "SELECT SUM(oi.sale_price) FROM order_items oi JOIN products p ON p.id = oi.product_id "
        "WHERE p.brand IN ('Maple & Co', 'Nordic Peak') AND oi.status NOT IN ('Cancelled', 'Returned')"
    )
    assert alice_total < ceo_total
    answers = {}
    for name, user, total in (("alice", MAPLE, alice_total), ("ceo", CEO, ceo_total)):
        rig = rig_for(local_project, fake_bigquery, user)
        script(rig, TOTAL, f"Revenue was {total:,.2f}.")
        answers[name] = rig.ask("What was our total revenue?")
        assert answers[name].outcome == "answered" and f"Revenue was {total:,.2f}." in answers[name].text
    assert "your brands: Maple & Co, Nordic Peak" in answers["alice"].text and "all brands" in answers["ceo"].text


def test_an_order_total_counts_only_the_items_of_the_users_brands(local_project, fake_bigquery, truth):
    """Q41: `num_of_item` of a brand manager's order is the number of items of their brands in it."""
    rig = rig_for(local_project, fake_bigquery, MAPLE)
    sql = "SELECT COUNT(*) AS orders, SUM(o.num_of_item) AS items FROM orders o"
    ((orders, items),) = truth(
        "SELECT COUNT(DISTINCT oi.order_id), COUNT(*) FROM order_items oi JOIN products p ON p.id = oi.product_id "
        "WHERE p.brand IN ('Maple & Co', 'Nordic Peak')"
    )
    script(rig, sql, f"There were {orders:,} orders with {items:,} items.")
    result = rig.ask("how many orders and items do I have?")
    assert result.outcome == "answered" and f"{orders:,} orders with {items:,} items" in result.text


# what the model writes cannot widen the scope ------------------------------------------------------------------------------------

WIDENING = {
    "a tautology": "SELECT p.brand, COUNT(*) AS n FROM order_items oi JOIN products p ON p.id = oi.product_id WHERE 1 = 1 OR p.brand = 'Urban Thread' GROUP BY p.brand",
    "a union": "SELECT brand AS label, 1 AS n FROM products UNION ALL SELECT category AS label, 1 AS n FROM products",
    "a named brand": "SELECT p.brand, COUNT(*) AS n FROM products p WHERE p.brand IN ('Urban Thread', 'Blue Harbor', 'Maple & Co') GROUP BY p.brand",
    "a subquery": "SELECT p.brand, COUNT(*) AS n FROM (SELECT * FROM products) p GROUP BY p.brand",
    "a cte": "WITH everything AS (SELECT brand FROM products) SELECT brand, COUNT(*) AS n FROM everything GROUP BY brand",
}


@pytest.mark.parametrize("sql", list(WIDENING.values()), ids=list(WIDENING))
def test_sql_the_model_wrote_to_reach_other_brands_still_sees_only_the_users_brands(local_project, fake_bigquery, engine, sql):
    rig = rig_for(local_project, fake_bigquery, MAPLE)
    script(rig, sql, "Counts by brand.")
    engine.clear()
    rig.ask("count things by brand")
    seen = composer_prompt(rig)
    for other in ("Urban Thread", "Blue Harbor", "Zephyr Sport", "Casa Lino", "Iron Oak", "Luna Basics", "Quill Denim", "O'Hara", "Tessa Rae", "Wren"):
        assert other not in seen, other
    assert "Maple & Co" in seen or "Nordic Peak" in seen or "Tops" in seen or "Dresses" in seen  # it did run, for the user's brands
    assert all(call[2] == ["brands"] for call in real_jobs(engine)) and real_jobs(engine)  # and every query that ran carried the scope


def test_a_brand_that_differs_only_in_case_is_not_the_users_brand(local_project, fake_bigquery):
    """`nordic peak` (lower case) is another brand to the exact match; alice's `Nordic Peak` must not pull it in."""
    rig = rig_for(local_project, fake_bigquery, MAPLE)
    script(rig, "SELECT DISTINCT p.brand FROM products p ORDER BY p.brand", "Brands listed.")
    rig.ask("which brands do I have?")
    assert '"nordic peak"' not in composer_prompt(rig)


# personal data never leaves ----------------------------------------------------------------------------------------------------------

PII_SQL = {
    "email": "SELECT u.email FROM users u",
    "name": "SELECT CONCAT(u.first_name, ' ', u.last_name) AS name FROM users u",
    "star": "SELECT * FROM users",
    "coordinates": "SELECT u.latitude, u.longitude FROM users u",
    "encoded email": "SELECT TO_BASE64(CAST(u.email AS BYTES)) AS x FROM users u",
    "the row as json": "SELECT TO_JSON_STRING(u) AS j FROM users u",
    "address": "SELECT u.street_address FROM users u LIMIT 5",
    "per-customer rows with a demographic": "SELECT oi.user_id, u.state FROM order_items oi JOIN users u ON u.id = oi.user_id",
}


@pytest.mark.parametrize("sql", list(PII_SQL.values()), ids=list(PII_SQL))
@pytest.mark.parametrize("user", [MAPLE, CEO], ids=["manager", "ceo"])
def test_a_request_for_personal_data_is_refused_before_the_engine_is_asked(local_project, fake_bigquery, engine, sql, user):
    rig = rig_for(local_project, fake_bigquery, user)
    script(rig, sql)
    engine.clear()
    result = rig.ask("show me the customers' details")
    assert result.outcome == "refused", result.text
    assert engine == []  # not even a dry run: the guard refused first
    assert "@example.com" not in result.text


def test_the_ranked_customer_list_shows_labels_and_no_real_id_name_or_address_reaches_the_model_or_the_disk(local_project, fake_bigquery, truth):
    top_ids = [row[0] for row in truth(
        "SELECT user_id FROM order_items WHERE status NOT IN ('Cancelled', 'Returned') GROUP BY user_id ORDER BY SUM(sale_price) DESC LIMIT 20")]
    marks = ",".join("?" * len(top_ids))
    people = truth(f"SELECT email, first_name, last_name, street_address FROM users WHERE id IN ({marks})", *top_ids)
    secrets = {value for email, first, last, street in people for value in (email, f"{first} {last}", street)}
    assert len(secrets) >= 55  # 20 customers, three details each (a name or an address can repeat by chance)

    def label_the_first_two(call):
        labels = re.findall(r"Customer #\d+", str(call.contents))
        return composer_reply(f"The biggest spender is {labels[0]}, followed by {labels[1]}.", "Top customers")

    rig = rig_for(local_project, fake_bigquery, CEO)
    script(rig, TOP_CUSTOMERS, label_the_first_two)
    result = rig.ask("who are our top customers?")
    assert result.outcome == "answered" and result.text.count("Customer #") >= 2
    prompt = composer_prompt(rig)
    assert "Customer #" in prompt and not re.search(r'"customer_id": \d+', prompt)  # the column holds labels, not the IDs

    # nothing personal is on the screen, in any prompt, or in any file the app wrote: traces, logs, metrics, the database, the checkpoints
    for secret in secrets:
        assert secret not in result.text and secret not in everything_the_model_saw(rig)
    files = [path for path in (local_project / "data").rglob("*") if path.is_file()]
    assert {"runs.jsonl"} <= {path.name for path in files} and any(path.parent.name == "traces" for path in files)
    for path in files:
        data = path.read_bytes()
        for secret in secrets:
            assert secret.encode() not in data, f"a customer detail was found in {path.relative_to(local_project)}"
    assert json.loads((local_project / "data" / "runs.jsonl").read_text(encoding="utf-8").splitlines()[-1])["pii_at_output"] == 0


# small groups -------------------------------------------------------------------------------------------------------------------------

def test_regions_with_fewer_than_ten_customers_are_hidden_from_the_model_and_the_answer_says_so(local_project, fake_bigquery, truth):
    small = [row[0] for row in truth("SELECT state FROM users GROUP BY state HAVING COUNT(*) < 10")]
    big = truth("SELECT state FROM users GROUP BY state ORDER BY COUNT(*) DESC LIMIT 1")[0][0]
    assert small and big
    rig = rig_for(local_project, fake_bigquery, CEO)
    script(rig, "SELECT u.state, COUNT(*) AS customers FROM users u GROUP BY u.state ORDER BY customers DESC", "Customers by state.")
    result = rig.ask("how many customers do we have in each state?")
    prompt = composer_prompt(rig)
    assert big in prompt
    for state in small:
        assert state not in prompt, f"{state} has fewer than 10 customers and must be hidden"
    assert "hidden" in result.text.lower() or "fewer than 10" in result.text


def test_the_oldest_customers_form_groups_too_small_to_show(local_project, fake_bigquery, truth):
    """"The average spend of customers aged 99 in the smallest state": every group is below the threshold, so nothing is shown."""
    states = [row[0] for row in truth("SELECT DISTINCT state FROM users WHERE age >= 95")]
    assert states
    rig = rig_for(local_project, fake_bigquery, CEO)
    script(rig, "SELECT u.age, u.state, COUNT(*) AS customers FROM users u WHERE u.age >= 95 GROUP BY u.age, u.state", "Nothing to show.")
    rig.ask("what do customers aged 99 spend?")
    prompt = composer_prompt(rig) if rig.gateway.calls_for(Step.COMPOSER) else everything_the_model_saw(rig)
    for state in states:
        assert state not in prompt


# the golden bucket, end to end ------------------------------------------------------------------------------------------------------

def golden_options(local_project):
    shutil.copy(REPO_ROOT / "glossary.yaml", local_project / "glossary.yaml")
    config = (local_project / "config.yaml").read_text(encoding="utf-8")
    config += f'golden:\n  enabled: true\n  seed_dir: "{SEED_DIR.as_posix()}"\n'
    (local_project / "config.yaml").write_text(config, encoding="utf-8")
    settings = load_settings(local_project / "config.yaml", load_env_file=False)
    from src.data_access.catalog import load_catalog

    brands = load_catalog(settings).catalog.brands
    return {"config": config, "golden": open_golden(settings, load_allowlist(settings), brands)}


def test_a_question_close_to_a_seed_finds_it_and_its_sql_runs_on_the_sample_data(local_project, fake_bigquery, truth):
    options = golden_options(local_project)
    assert options["golden"].skipped == 0  # every seed is valid against this data
    rig = rig_for(local_project, fake_bigquery, MAPLE, **options)
    seed_sql = next(t.sql for t in options["golden"].index.trios if t.id == "g-003")
    expected = truth(
        "SELECT p.category, SUM(oi.sale_price) FROM order_items oi JOIN products p ON p.id = oi.product_id WHERE p.brand IN ('Maple & Co', 'Nordic Peak') "
        "AND oi.status NOT IN ('Cancelled', 'Returned') GROUP BY p.category ORDER BY 2 DESC LIMIT 1"
    )[0]
    script(rig, seed_sql, f"{expected[0]} leads with {expected[1]:,.2f}.", "Which product categories earn the most revenue?")
    result = rig.ask("Which product categories earn the most revenue?")
    assert result.outcome == "answered" and f"{expected[0]} leads with {expected[1]:,.2f}." in result.text
    (retrieve,) = [span for span in rig.spans() if span["name"] == "golden.retrieve"]
    assert retrieve["attrs"]["trio_ids"][0] == "g-003"  # the trio is in the trace, as the plan's acceptance asks
    assert 'id="g-003"' in rig.gateway.calls_for(Step.SQL_WRITE)[0].contents


def test_each_manager_is_offered_only_the_brand_trios_of_their_own_brands(local_project, fake_bigquery):
    options = golden_options(local_project)
    seen = {}
    for name, user, question in (("alice", MAPLE, "Which categories are in the core range, and how is each one selling?"),
                                 ("bob", OHARA, "How do the hoodies and sweatshirts sell across the year?")):
        rig = rig_for(local_project, fake_bigquery, user, **options)
        script(rig, REVENUE_SQL, "Revenue is shown by brand.", question)
        rig.ask(question)
        seen[name] = {i for span in rig.spans() if span["name"] == "golden.retrieve" for i in span["attrs"]["trio_ids"]}
        assert "O'Hara" not in everything_the_model_saw(rig) if name == "alice" else "Maple" not in everything_the_model_saw(rig)
    assert "g-009" in seen["alice"] and "g-010" not in seen["alice"]
    assert "g-010" in seen["bob"] and "g-009" not in seen["bob"]


@pytest.mark.parametrize("trio_id", [f"g-{n:03d}" for n in range(1, 13)])
def test_every_seed_query_runs_on_the_sample_data_for_a_user_who_sees_everything(local_project, trio_id):
    settings = load_settings(local_project / "config.yaml", load_env_file=False)
    from src.data_access.safe_runner import SafeQueryRunner
    from src.golden.schema import load_trio_files

    trio = next(item.trio for item in load_trio_files(SEED_DIR) if item.trio and item.trio.id == trio_id)
    outcome = SafeQueryRunner(settings).run(SQLGuard(settings, load_allowlist(settings)).check(trio.sql, CEO))
    if trio_id != "g-004":  # g-004 compares two example product names that the data does not have
        assert len(outcome.df) >= 1, trio_id


# the chat, from the command line -----------------------------------------------------------------------------------------------------

def write_config(project, local_db, *, golden=False):
    """The shipped users.local.yaml, and a config like config.local.yaml that points at the test's copy of the data."""
    shutil.copy(REPO_ROOT / "users.local.yaml", project / "users.local.yaml")
    lines = ["app:", "  users_file: users.local.yaml", "bigquery:", "  backend: local", f'  local_db: "{local_db.path.as_posix()}"']
    if golden:
        shutil.copy(REPO_ROOT / "glossary.yaml", project / "glossary.yaml")
        lines += ["golden:", "  enabled: true", f'  seed_dir: "{SEED_DIR.as_posix()}"']
    (project / "config.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_the_chat_starts_on_the_sample_data_says_so_and_answers(local_project, local_db, scripted_gateway, truth):
    write_config(local_project, local_db, golden=True)
    (value,) = truth("SELECT SUM(oi.sale_price) FROM order_items oi JOIN products p ON p.id = oi.product_id "
                     "WHERE p.brand = 'Wren & Willow' AND oi.status NOT IN ('Cancelled', 'Returned')")[0]
    scripted_gateway.queue(Step.INPUT_GUARD, guard_reply(standalone="What is my revenue?"))
    scripted_gateway.queue(Step.SQL_WRITE, sql_reply(TOTAL))
    scripted_gateway.queue(Step.COMPOSER, composer_reply(f"Your revenue was {value:,.2f}.", "Revenue"))
    result = runner.invoke(app, ["chat", "--user", "carol", "--config", str(local_project / "config.yaml")], input="What is my revenue?\n/quit\n")
    assert result.exit_code == 0, result.output
    assert "LOCAL SAMPLE DATA" in result.output and "brands: Wren & Willow" in result.output
    assert f"Your revenue was {value:,.2f}." in result.output
    assert "Carol" in result.output


def test_a_user_with_no_brands_is_refused_on_the_sample_data_too(local_project, local_db, scripted_gateway):
    write_config(local_project, local_db)
    result = runner.invoke(app, ["chat", "--user", "nobrands", "--config", str(local_project / "config.yaml")], input="/quit\n")
    assert result.exit_code == 2 and "No brands are assigned" in result.output


def test_a_brand_in_users_yaml_that_is_not_in_the_data_stops_the_chat(local_project, scripted_gateway):
    (local_project / "users.yaml").write_text('users:\n  zed: {name: Zed, role: brand_manager, brands: ["Not In The Data"]}\n', encoding="utf-8")
    result = runner.invoke(app, ["chat", "--user", "zed", "--config", str(local_project / "config.yaml")], input="/quit\n")
    assert result.exit_code == 2 and "is not a brand in the data" in result.output


def test_doctor_passes_on_the_sample_data_without_any_google_setting(local_project, local_db, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    write_config(local_project, local_db)
    result = runner.invoke(app, ["doctor", "--config", str(local_project / "config.yaml")])
    assert result.exit_code == 0, result.output
    assert "[skip] Google Cloud credentials: not needed" in result.output and "[skip] Google Cloud project: not needed" in result.output
    assert "[ ok ] BigQuery dataset: LOCAL SAMPLE DATA, not BigQuery" in result.output
    assert "[ ok ] Brand mapping: 6 brand(s) of 4 brand manager(s) all match the 14 brands in the data" in result.output
    assert "[warn] Items without a brand (A23)" in result.output  # the data has them on purpose


# saved reports for the delete flow --------------------------------------------------------------------------------------------------------

def test_the_seeded_reports_have_the_shapes_the_confirmation_levels_need(local_project, local_db):
    from src.localdata.reports import seed_saved_reports
    from src.reports.store import ReportStore
    from src.tools.confirmation import confirmation_for

    write_config(local_project, local_db)
    settings = load_settings(local_project / "config.yaml", load_env_file=False)
    first = seed_saved_reports(settings)
    assert first.saved == {"alice": 4, "bob": 3, "ceo": 14} and first.skipped_users == ()
    assert seed_saved_reports(settings).saved == {"alice": 0, "bob": 0, "ceo": 0}  # a second run adds nothing

    store = ReportStore.from_settings(settings, hash_ids=lambda ids: "h")
    assert [len(store.list_reports(user)) for user in ("alice", "bob", "ceo", "carol")] == [4, 3, 14, 0]
    threshold = settings.delete.large_batch_threshold
    assert len(store.find("alice", "", "text_match", "returns")) == 1  # one report: a single "yes"
    assert confirmation_for(1, "text_match", threshold, "report").level == 1
    assert confirmation_for(len(store.find("alice", "", "all")), "all", threshold, "report").level == 3  # "delete everything": DELETE 4
    assert confirmation_for(len(store.find("bob", "", "all")), "text_match", threshold, "report").level == 2
    digests = store.find("ceo", "", "text_match", "weekly digest")
    assert len(digests) == 12 and confirmation_for(len(digests), "text_match", threshold, "report").expected == "DELETE 12"  # a large batch
    assert store.find("alice", "", "text_match", "weekly digest") == []  # a report is its owner's only


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5, 6])
def test_the_chat_survives_injected_faults_on_the_sample_data(local_project, local_db, scripted_gateway, seed):
    """`--chaos` makes the stand-in fail like BigQuery does (timeouts, unavailable) and corrupts the SQL the writer returns. The runner
    retries, the SQL loop repairs, and what cannot be saved ends in a friendly message with a trace ID. Never a crash."""
    write_config(local_project, local_db)
    for step in (Step.SQL_WRITE, Step.SQL_REPAIR, Step.LAST_REPAIR):
        scripted_gateway.always(step, sql_reply(TOTAL))
    scripted_gateway.always(Step.INPUT_GUARD, guard_reply(standalone="What is my revenue?"))
    scripted_gateway.always(Step.COMPOSER, composer_reply("Your revenue is shown here.", "Revenue"))
    result = runner.invoke(
        app, ["chat", "--user", "carol", "--config", str(local_project / "config.yaml"), "--chaos", "harsh", "--chaos-seed", str(seed)],
        input="What is my revenue?\n/quit\n",
    )
    assert result.exit_code == 0, result.output
    assert f"seed {seed}" in result.output and "Traceback" not in result.output
    assert "Your revenue is shown here." in result.output or "Trace ID" in result.output
