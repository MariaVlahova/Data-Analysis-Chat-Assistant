"""Shared fixtures. Tests use temporary project folders and never touch the real users.yaml or data/."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.fake_bigquery import FakeClient, FakeRunner, default_client

REPO_ROOT = Path(__file__).resolve().parent.parent

USERS_YAML = """\
users:
  alice: {name: Alice, role: brand_manager, brands: ["Brand A", "Brand B"]}
  bob:   {name: Bob,   role: brand_manager, brands: ["Brand C"]}
  ceo:   {name: CEO,   role: ceo, all_brands: true}
  nobrands: {name: No Brands, role: brand_manager, brands: []}
"""

SECRET_ENV_VARS = ("GEMINI_API_KEY", "GOOGLE_CLOUD_PROJECT", "GOOGLE_APPLICATION_CREDENTIALS", "OPSFLEET_CONFIG")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """No real keys or config paths leak into a test. Tests marked `live` are the exception: they need the real
    GOOGLE_APPLICATION_CREDENTIALS and GOOGLE_CLOUD_PROJECT to reach BigQuery."""
    if request.node.get_closest_marker("live"):
        return
    for name in SECRET_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def fake_bigquery(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> FakeClient | None:
    """No test reaches BigQuery: every `SafeQueryRunner` gets this scripted client (canned dataset, see
    tests/fake_bigquery.py). A test changes its behaviour by adding rules. Tests marked `live` get the real client."""
    if request.node.get_closest_marker("live"):
        return None
    client = default_client()
    monkeypatch.setattr(
        "src.data_access.safe_runner.make_runner", lambda settings: FakeRunner(client)
    )
    return client


@pytest.fixture
def users_yaml() -> str:
    return USERS_YAML


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary project folder with a minimal config.yaml, a users.yaml and the repository's schema_allowlist.yaml.
    The cwd is moved into it, so no developer .env file is picked up."""
    (tmp_path / "config.yaml").write_text("app:\n  language: en\n", encoding="utf-8")
    (tmp_path / "users.yaml").write_text(USERS_YAML, encoding="utf-8")
    allowlist = (REPO_ROOT / "schema_allowlist.yaml").read_text(encoding="utf-8")  # plan 05
    flag = re.compile(r"^reviewed_against_live: (true|false)$", re.MULTILINE)
    assert flag.search(allowlist), "schema_allowlist.yaml must carry the reviewed_against_live flag"
    # The shipped file says whether a person has compared it with the live schema, and that changes once someone has. These
    # tests are about everything else, so their copy always says it was reviewed (the tests of the gate say otherwise).
    (tmp_path / "schema_allowlist.yaml").write_text(flag.sub("reviewed_against_live: true", allowlist), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def config_path(project: Path) -> Path:
    return project / "config.yaml"
