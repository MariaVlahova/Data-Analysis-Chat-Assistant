"""The BigQuery checks of `doctor` (plan 03): dataset, byte cap, brand mapping, A23 and A21."""
import re
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from typer.testing import CliRunner

from src.cli.main import app
from src.config import load_settings
from src.data_access.catalog import load_catalog

runner = CliRunner(env={"COLUMNS": "250"})


@pytest.fixture
def ready(project, monkeypatch):
    """Keys, credentials and a clean users.yaml are in place, so only the BigQuery checks can fail."""
    import google.auth

    (project / "users.yaml").write_text(
        'users:\n  alice: {name: Alice, role: brand_manager, brands: ["Brand A", "Brand B"]}\n  ceo: {name: CEO, role: ceo, all_brands: true}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(google.auth, "default", lambda *a, **k: (object(), "adc-project"))
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")


def run_doctor(config_path, *extra):
    return runner.invoke(app, ["doctor", "--config", str(config_path), *extra])


def line(output: str, name: str) -> str:
    """The result line of one check (not a Fix line that happens to mention it)."""
    return next(row for row in output.splitlines() if re.match(rf"\[.{{4}}\] {re.escape(name)}", row))


def test_all_bigquery_checks_pass_on_a_healthy_setup(project, config_path, ready):
    result = run_doctor(config_path)
    assert result.exit_code == 0, result.output
    assert "[ ok ] BigQuery dataset: bigquery-public-data.thelook_ecommerce: 4 tables, latest order item 2026-10-22" in result.output
    assert "[ ok ] Byte cap: 512.0 MiB per query, 2.0 GiB per turn; largest table is order_items at 40.1 MiB, about 5x is 200.3 MiB" in result.output
    assert "[ ok ] Brand mapping: 2 brand(s) of 1 brand manager(s) all match the 9 brands in the data" in result.output
    assert "[ ok ] Items without a brand (A23): all 180,000 order items join to a brand" in result.output
    assert "[ ok ] Text columns (A21)" in result.output and "users: first_name, last_name, email" in result.output
    assert "All checks passed." in result.output


def test_without_any_credentials_the_dataset_check_fails_once_with_the_fix_and_the_rest_are_skipped(project, config_path, ready, monkeypatch):
    from google.auth.exceptions import DefaultCredentialsError

    def no_credentials(settings):
        raise DefaultCredentialsError("none")

    monkeypatch.setattr("src.data_access.safe_runner.make_runner", no_credentials)
    result = run_doctor(config_path)
    assert result.exit_code == 1
    assert "[FAIL] BigQuery dataset" in line(result.output, "BigQuery dataset") and "gcloud auth application-default login" in result.output
    for name in ("Byte cap", "Brand mapping", "Items without a brand", "Text columns"):
        assert line(result.output, name).startswith("[skip]") and "BigQuery dataset check failed" in line(result.output, name)
    assert "the check crashed" not in result.output


def test_credentials_the_probe_cannot_see_do_not_hide_the_bigquery_section(project, config_path, ready, monkeypatch):
    """Credentials from a metadata server (GCE, Cloud Run, workload identity) are invisible to the credentials check,
    which switches that probe off. BigQuery itself works there, so its checks must still run."""
    import google.auth
    from google.auth.exceptions import DefaultCredentialsError

    def not_found_without_the_probe(*args, **kwargs):
        raise DefaultCredentialsError("metadata probe switched off")

    monkeypatch.setattr(google.auth, "default", not_found_without_the_probe)
    result = run_doctor(config_path)
    assert line(result.output, "Google Cloud credentials").startswith("[FAIL]")
    for name in ("BigQuery dataset", "Byte cap", "Brand mapping", "Items without a brand", "Text columns"):
        assert line(result.output, name).startswith("[ ok ]"), name


def test_during_an_outage_doctor_makes_one_failed_round_of_calls_not_one_per_check(project, config_path, ready, fake_bigquery):
    settings = load_settings(config_path, load_env_file=False)
    load_catalog(settings, now=datetime.now(timezone.utc) - timedelta(days=3))
    fake_bigquery.down = True
    tables_before, jobs_before = len(fake_bigquery.table_calls), len(fake_bigquery.jobs)
    result = run_doctor(config_path)
    assert len(fake_bigquery.table_calls) - tables_before == 12  # four tables read together, 3 tries each (transient_retries is 2) ...
    assert len(fake_bigquery.jobs) == jobs_before  # ... and the brand query and the A23 count were not tried at all
    assert line(result.output, "Items without a brand").startswith("[skip]") and "could not be reached" in line(result.output, "Items without a brand")


def test_a_brand_typo_during_an_outage_gets_no_live_top_brands_hint(project, config_path, ready, fake_bigquery):
    settings = load_settings(config_path, load_env_file=False)
    load_catalog(settings, now=datetime.now(timezone.utc) - timedelta(days=3))
    (project / "users.yaml").write_text('users:\n  alice: {name: A, role: brand_manager, brands: ["levis"]}\n', encoding="utf-8")
    fake_bigquery.down = True
    jobs_before = len(fake_bigquery.jobs)
    result = run_doctor(config_path)
    assert 'Did you mean "Levi\'s"?' in result.output  # from the saved list
    assert "Top brands by item count" not in result.output and len(fake_bigquery.jobs) == jobs_before


def test_one_outage_is_reported_once_not_five_times(config_path, ready, fake_bigquery):
    fake_bigquery.down = True
    result = run_doctor(config_path)
    assert result.exit_code == 1
    assert "[FAIL] BigQuery dataset: BigQuery could not be reached and there is no saved schema" in result.output
    assert result.output.count("no saved schema") == 1
    for name in ("Byte cap", "Brand mapping", "Items without a brand", "Text columns"):
        assert "[skip]" in line(result.output, name) and "BigQuery dataset check failed" in line(result.output, name)


def test_a_saved_copy_during_an_outage_is_a_warning_not_a_failure(project, config_path, ready, fake_bigquery):
    settings = load_settings(config_path, load_env_file=False)
    load_catalog(settings, now=datetime.now(timezone.utc) - timedelta(days=3))
    fake_bigquery.down = True
    result = run_doctor(config_path)
    assert "[warn] BigQuery dataset" in result.output and "so the schema saved on" in result.output
    assert "Fix: Check your connection" in result.output
    assert "[ ok ] Brand mapping" in result.output  # the saved brand list still checks users.yaml


def test_refresh_reads_the_dataset_again(project, config_path, ready, fake_bigquery):
    run_doctor(config_path)
    after_first = len(fake_bigquery.table_calls)
    run_doctor(config_path)
    assert len(fake_bigquery.table_calls) == after_first  # the saved copy was used
    run_doctor(config_path, "--refresh")
    assert len(fake_bigquery.table_calls) == after_first + 4


def test_a_brand_typo_fails_with_the_suggestion_and_the_top_brands(project, config_path, ready):
    (project / "users.yaml").write_text(
        'users:\n  alice: {name: A, role: brand_manager, brands: ["levis"]}\n  ceo: {name: C, role: ceo, all_brands: true}\n',
        encoding="utf-8",
    )
    result = run_doctor(config_path)
    assert result.exit_code == 1
    assert '[FAIL] Brand mapping: alice: "levis" is not a brand in the data. Did you mean "Levi\'s"?' in result.output
    assert "Top brands by item count: Nike (1,200), Levi's (900)." in result.output


def test_items_without_a_brand_are_a_finding(project, config_path, ready, fake_bigquery):
    fake_bigquery.add("COUNTIF", df=pd.DataFrame({"items": [180_000], "without_product": [40], "without_brand": [1_800]}))
    result = run_doctor(config_path)
    assert result.exit_code == 0  # a finding to write down, not a setup failure
    assert "[warn] Items without a brand (A23): 1,840 of 180,000 order items (1.02%)" in result.output
    assert "only the CEO can" in result.output and "Write this count in the docs (A23)." in result.output


def test_a_count_that_cannot_run_is_a_warning(project, config_path, ready, fake_bigquery):
    from google.api_core import exceptions as api_exc

    fake_bigquery.add("COUNTIF", dry_errors=[api_exc.Forbidden("Access Denied")])
    result = run_doctor(config_path)
    assert "[warn] Items without a brand (A23): could not count them (BQ_UNAVAILABLE)" in result.output


def test_a_cap_below_the_largest_table_is_a_warning(project, config_path, ready):
    # 20 MB: above the startup queries (8 MB and 1 MB in the fake), below the 42 MB table
    config_path.write_text("bigquery:\n  max_bytes_billed: 20000000\n", encoding="utf-8")
    result = run_doctor(config_path)
    assert "[warn] Byte cap: 19.1 MiB per query" in result.output
    assert "Fix: Raise bigquery.max_bytes_billed" in result.output


def test_a_cap_too_small_for_the_startup_queries_fails_the_dataset_check(project, config_path, ready):
    config_path.write_text("bigquery:\n  max_bytes_billed: 1000000\n", encoding="utf-8")
    result = run_doctor(config_path)
    assert result.exit_code == 1
    assert "[FAIL] BigQuery dataset" in result.output and "QUERY_TOO_EXPENSIVE" in result.output


def test_a_stale_dataset_is_mentioned_in_the_dataset_line(project, config_path, ready, fake_bigquery):
    fake_bigquery.add("MAX(created_at)", df=pd.DataFrame({"latest": [pd.Timestamp("2020-01-02", tz="UTC")]}))
    result = run_doctor(config_path)
    assert "latest order item 2020-01-02" in result.output and "The data ends on 2020-01-02" in result.output
    assert "[ ok ] BigQuery dataset" in result.output  # informational: the answer says it too (A5)


def test_the_output_carries_neither_the_credentials_project_nor_any_cell_value(project, config_path, ready):
    result = run_doctor(config_path)
    assert "my-project" in result.output  # the configured project is shown by its own check ...
    assert "adc-project" not in result.output  # ... the project found in the credentials is not
    assert "Nike" not in result.output and "Levi" not in result.output  # brand rows only appear in a failure hint
    assert "Traceback" not in result.output


def test_an_unexpected_crash_while_reading_the_dataset_is_reported_once_and_not_repeated(project, config_path, ready, monkeypatch):
    calls = []

    def boom(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("kaboom canary@example.test")

    monkeypatch.setattr("src.data_access.catalog.load_catalog", boom)
    result = run_doctor(config_path)
    assert result.exit_code == 1 and len(calls) == 1  # one attempt, not one per check
    assert "[FAIL] BigQuery dataset: Reading the BigQuery dataset failed unexpectedly (RuntimeError)" in result.output
    assert "the check crashed" not in result.output
    assert "kaboom" not in result.output and "canary" not in result.output
    for name in ("Byte cap", "Brand mapping", "Items without a brand", "Text columns"):
        assert "[skip]" in line(result.output, name)
    # The failure leaves a trace for the operator: class and call sites in the masked log, never the text.
    log_text = (project / "data" / "logs" / "app.log").read_text(encoding="utf-8")
    assert "reading the BigQuery dataset failed: RuntimeError at" in log_text and "kaboom" not in log_text
    assert "chat --user ceo" not in result.output  # the old fix named a user that may not exist


def test_no_project_anywhere_is_one_clear_failure_not_five_crashes(project, config_path, monkeypatch):
    import google.auth

    def no_project(settings):
        raise OSError("Project was not passed and could not be determined from the environment.")

    monkeypatch.setattr(google.auth, "default", lambda *a, **k: (object(), None))
    monkeypatch.setattr("src.data_access.safe_runner.make_runner", no_project)
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    result = run_doctor(config_path)
    assert "[FAIL] Google Cloud project" in result.output
    assert "[FAIL] BigQuery dataset" in result.output and "GOOGLE_CLOUD_PROJECT" in result.output
    assert "the check crashed" not in result.output and "Traceback" not in result.output


def test_a_crash_in_the_top_brands_hint_does_not_replace_the_brand_failure(project, config_path, ready, monkeypatch):
    (project / "users.yaml").write_text('users:\n  alice: {name: A, role: brand_manager, brands: ["levis"]}\n', encoding="utf-8")

    def boom(*args, **kwargs):
        raise ValueError("kaboom")

    monkeypatch.setattr("src.data_access.brand_check.top_brands", boom)
    result = run_doctor(config_path)
    assert 'Brand mapping: alice: "levis" is not a brand in the data. Did you mean "Levi\'s"?' in result.output
    assert "the check crashed" not in result.output and "Top brands by item count" not in result.output
    assert "kaboom" not in result.output


def test_a_crash_in_the_a23_count_is_a_warning_with_the_class_not_a_crashed_check(project, config_path, ready, monkeypatch):
    def boom(*args, **kwargs):
        raise ValueError("kaboom")

    monkeypatch.setattr("src.data_access.brand_check.brand_coverage", boom)
    result = run_doctor(config_path)
    assert "[warn] Items without a brand (A23): could not count them (ValueError)" in result.output
    assert "the check crashed" not in result.output and "kaboom" not in result.output
