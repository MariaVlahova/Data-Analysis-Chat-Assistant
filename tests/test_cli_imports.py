"""The CLI must stay light and must survive a broken install (plan 03 review).

BigQuery and pandas load only when a chat starts or a BigQuery check runs. Otherwise `trace`, `metrics` and
`/help` pay about a second for nothing, and `doctor` dies on an import error before its Packages check can say
which package is missing. Each test runs in a fresh interpreter, so what the test run already imported cannot hide a problem.
"""
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run_clean(script: str, tmp_path) -> subprocess.CompletedProcess:
    prelude = f"import sys; sys.path.insert(0, {str(ROOT)!r})\n"
    return subprocess.run(
        [sys.executable, "-I", "-c", prelude + textwrap.dedent(script)],
        capture_output=True, text=True, cwd=tmp_path, timeout=120,
    )


def test_importing_the_cli_does_not_load_bigquery_or_pandas(tmp_path):
    result = run_clean(
        """
        import src.cli.main, src.cli.doctor, src.cli.commands
        loaded = [name for name in ("pandas", "google.cloud.bigquery") if name in sys.modules]
        print("LOADED:", loaded)
        """,
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert "LOADED: []" in result.stdout


def test_doctor_still_names_the_missing_package_when_bigquery_and_pandas_cannot_be_imported(tmp_path):
    result = run_clean(
        """
        sys.modules["pandas"] = None  # a None entry makes `import pandas` raise ImportError
        sys.modules["google.cloud.bigquery"] = None
        from src.cli.doctor import DoctorContext, check_packages
        check = check_packages(DoctorContext(settings=None))
        print("STATUS:", check.status)
        print("DETAIL:", check.detail)
        """,
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert "STATUS: fail" in result.stdout
    assert "pandas" in result.stdout and "google-cloud-bigquery" in result.stdout


def test_the_bigquery_checks_run_after_the_setup_checks_they_depend_on(tmp_path):
    result = run_clean(
        """
        from src.cli.doctor import CHECKS
        print("ORDER:", [fn.__name__ for fn in CHECKS])
        """,
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    order = eval(result.stdout.split("ORDER:", 1)[1].strip())
    bigquery_checks = ["check_bigquery_dataset", "check_byte_cap", "check_brand_mapping_live", "check_brand_coverage", "check_string_columns"]
    first = order.index(bigquery_checks[0])
    assert order[first:first + len(bigquery_checks)] == bigquery_checks  # together, in this order
    assert first > order.index("check_google_credentials") and first > order.index("check_google_project")  # after the setup checks


# the credential probe (plan 03 review) --------------------------------------------------------------------------------
# google-auth reads these limits once, when it is first imported, so each check runs in a fresh interpreter.

PRINT_PROBE = """
from google.auth.compute_engine import _metadata
print("TIMEOUT:", _metadata._METADATA_DEFAULT_TIMEOUT, "RETRIES:", _metadata._METADATA_DETECT_RETRIES, "SWITCHED_OFF:", _metadata._NO_GCE_CHECK)
"""


def test_importing_the_client_module_shortens_the_metadata_probe_without_switching_it_off(tmp_path):
    result = run_clean("import src.data_access.bq_client\n" + PRINT_PROBE, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "TIMEOUT: 1 RETRIES: 1 SWITCHED_OFF: False" in result.stdout  # not the library's 3 s x 3 tries


def test_a_limit_you_set_yourself_is_kept(tmp_path, monkeypatch):
    monkeypatch.setenv("GCE_METADATA_TIMEOUT", "7")
    result = subprocess.run(
        [sys.executable, "-c", f"import sys; sys.path.insert(0, {str(ROOT)!r})\nimport src.data_access.bq_client\n" + PRINT_PROBE],
        capture_output=True, text=True, cwd=tmp_path, timeout=120,  # no -I: that would drop the variable set above
    )
    assert result.returncode == 0, result.stderr
    assert "TIMEOUT: 7 RETRIES: 1" in result.stdout


def test_the_doctor_credentials_check_no_longer_switches_the_probe_off(tmp_path):
    """It used to set NO_GCE_CHECK before importing google-auth, which reads it once: credentials from the metadata
    server (Compute Engine, Cloud Run) were then invisible to every BigQuery check later in the same doctor run."""
    script = """
import os
os.environ["CLOUDSDK_CONFIG"] = os.getcwd()  # no gcloud login in this folder
os.environ["GCE_METADATA_HOST"] = "127.0.0.1:1"  # nothing listens there, so the probe fails at once
from src.cli.doctor import DoctorContext, check_google_credentials
print("CHECK:", check_google_credentials(DoctorContext(settings=None)).status)
""" + PRINT_PROBE
    result = run_clean(script, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "SWITCHED_OFF: False" in result.stdout and "TIMEOUT: 1 RETRIES: 1" in result.stdout
