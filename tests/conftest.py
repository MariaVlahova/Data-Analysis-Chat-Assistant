"""Shared fixtures. Tests use temporary project folders and never touch the real users.yaml or data/."""
from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from src.data_access import safe_runner as _safe_runner
from tests.fake_bigquery import FakeClient, FakeRunner, default_client

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_MAKE_RUNNER = _safe_runner.make_runner  # taken at import, before the autouse fixture below replaces it with the fake

USERS_YAML = """\
users:
  alice: {name: Alice, role: brand_manager, brands: ["Brand A", "Brand B"]}
  bob:   {name: Bob,   role: brand_manager, brands: ["Brand C"]}
  ceo:   {name: CEO,   role: ceo, all_brands: true}
  nobrands: {name: No Brands, role: brand_manager, brands: []}
"""

SECRET_ENV_VARS = ("GEMINI_API_KEY", "GOOGLE_CLOUD_PROJECT", "GOOGLE_APPLICATION_CREDENTIALS", "OPSFLEET_CONFIG", "OPSFLEET_ENV")


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


@pytest.fixture(scope="session")
def local_db(tmp_path_factory: pytest.TempPathFactory):
    """The generated sample data (`src/localdata`), built once per test run: a small, fixed copy (2500 customers, data to 2026-10-22).
    Returns the `BuildSummary`."""
    from datetime import date

    from src.localdata.generate import BuildOptions, generate

    path = tmp_path_factory.mktemp("localdata") / "thelook.duckdb"
    return generate(path, BuildOptions(seed=7, users=2500, end=date(2026, 10, 22)))


@pytest.fixture
def local_project(project: Path, local_db, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`project`, with a config that points at the generated data. The autouse fake BigQuery is undone for this test: the real
    `make_runner` reads `bigquery.backend: local` and gives the DuckDB stand-in, so the whole path from the config is the one tested."""
    from src.data_access import safe_runner

    monkeypatch.setattr(safe_runner, "make_runner", REAL_MAKE_RUNNER)
    (project / "config.yaml").write_text(
        f'app:\n  language: en\nbigquery:\n  backend: local\n  local_db: "{local_db.path.as_posix()}"\n', encoding="utf-8"
    )
    return project


@pytest.fixture
def local_settings(local_project: Path):
    """The settings of `local_project`: the sample data behind the local stand-in."""
    from src.config import load_settings

    return load_settings(local_project / "config.yaml", load_env_file=False)


@pytest.fixture
def truth(local_settings):
    """What the scenarios compute the right answers with, over the sample data."""
    from src.scenarios.truth import Truth

    handle = Truth.from_settings(local_settings)
    yield handle
    handle.close()


@pytest.fixture
def users_yaml() -> str:
    return USERS_YAML


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary project folder with a minimal config.yaml, a users.yaml, the repository's schema_allowlist.yaml and its
    static persona (on by default, decision 3.9). The cwd is moved into it, so no developer .env file is picked up."""
    (tmp_path / "config.yaml").write_text("app:\n  language: en\n", encoding="utf-8")
    (tmp_path / "users.yaml").write_text(USERS_YAML, encoding="utf-8")
    (tmp_path / "personas").mkdir()
    shutil.copyfile(REPO_ROOT / "personas" / "default.yaml", tmp_path / "personas" / "default.yaml")
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


@pytest.fixture
def scripted_gateway(monkeypatch: pytest.MonkeyPatch):
    """The chat's model gateway replaced by a `FakeGateway` the test scripts (plan 07). `open_session` builds its agent with
    `LLMGateway`, so a CLI test can run the real graph, guards and query runner with no network."""
    from src.llm.fake import FakeGateway

    gateway = FakeGateway()
    monkeypatch.setattr("src.llm.gateway.LLMGateway", lambda settings, **kwargs: gateway)
    return gateway


@pytest.fixture
def reports(project: Path, fake_bigquery: FakeClient):
    """The graph with the real Saved Reports tools on a migrated SQLite file (plan 08). The checkpoints are in memory."""
    from tests.reports_support import ReportsEnv

    env = ReportsEnv(project, fake_bigquery)
    yield env
    env.close()


@pytest.fixture
def durable(project: Path, fake_bigquery: FakeClient):
    """The same, with LangGraph's `SqliteSaver` on the same file, so `restart()` finds a conversation where it stopped."""
    from tests.reports_support import ReportsEnv

    env = ReportsEnv(project, fake_bigquery, checkpoints=True)
    yield env
    env.close()


@pytest.fixture
def offline_eval(local_project: Path, local_db, scripted_gateway, tmp_path: Path):
    """The evaluation harness on the sample data with a scripted model (plan 09): the app a `chat` starts, the Golden Bucket on as in
    config.local.yaml, and a `FakeGateway` the test scripts. See tests/eval_support.py."""
    from eval.harness import Harness
    from tests.eval_support import Offline, write_local_config

    settings = write_local_config(local_project, local_db, golden=True)
    out_dir = tmp_path / "eval-run"
    harness = Harness(settings, out_dir)
    yield Offline(settings, harness, scripted_gateway, out_dir)
    harness.close()


@pytest.fixture
def chaos_eval(local_project: Path, local_db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """The same, with the REAL `LLMGateway` (retries, breaker, rate limit, injected faults) over a fake SDK client that answers by schema, and ONE clock that
    moves when the gateway waits and that the turn budget reads too, so a 429 storm takes no real time and still spends the turn's seconds (plan 09,
    `tests/test_chaos_offline.py`)."""
    from eval.harness import Harness
    from src.agent import runtime as runtime_module
    from src.llm import gateway as gateway_module
    from src.llm.fake import FakeClock
    from tests.eval_support import Offline, SchemaClient, write_local_config

    real, real_build = gateway_module.LLMGateway, runtime_module.build_agent
    clock, client = FakeClock(), SchemaClient()
    monkeypatch.setattr(
        "src.llm.gateway.LLMGateway",
        lambda settings, **kwargs: real(settings, client=client, clock=clock, sleep=clock.sleep, jitter=lambda: 0.5, **kwargs),
    )
    monkeypatch.setattr("src.agent.runtime.build_agent", lambda *args, **kwargs: real_build(*args, clock=clock, **kwargs))
    settings = write_local_config(local_project, local_db, golden=False)
    harness = Harness(settings, tmp_path / "eval-run")
    yield Offline(settings, harness, None, tmp_path / "eval-run", client=client, clock=clock)
    harness.close()
