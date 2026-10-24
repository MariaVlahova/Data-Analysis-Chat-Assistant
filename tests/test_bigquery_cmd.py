"""`bigquery init-users` and `config.bigquery.yaml`: the first steps of running against real BigQuery, tested here against the sample data (the
command works with whatever data the config names) and with the data warehouse failing."""
import pytest
import yaml
from typer.testing import CliRunner

from src.access.users import load_user_directory
from src.cli.bigquery_cmd import WANTED_BRANDS, users_yaml
from src.cli.main import app
from src.config import load_settings
from src.data_access.brand_check import check_brand_mapping
from src.data_access.catalog import load_catalog
from tests.golden_support import REPO_ROOT

runner = CliRunner(env={"COLUMNS": "250"})
BRANDS = [(f"Brand {n}", 1000 - n) for n in range(1, 9)]


def invoke(config, *args):
    return runner.invoke(app, ["bigquery", "init-users", "--config", str(config), *args])


@pytest.fixture
def config(local_project):
    return local_project / "config.yaml"


# the file ------------------------------------------------------------------------------------------------------------------------

def test_the_users_are_the_six_of_the_local_file_with_the_busiest_brands_in_order():
    data = yaml.safe_load(users_yaml(BRANDS))["users"]
    assert list(data) == ["alice", "bob", "carol", "dave", "ceo", "nobrands"]
    assert data["alice"]["brands"] == ["Brand 1", "Brand 2"] and data["bob"]["brands"] == ["Brand 3"] and data["carol"]["brands"] == ["Brand 4"]
    assert data["dave"]["brands"] == ["Brand 5", "Brand 6"]
    assert data["ceo"] == {"name": "CEO", "role": "ceo", "all_brands": True} and data["nobrands"]["brands"] == []


def test_the_same_user_names_as_the_local_file_so_the_scenarios_and_the_manual_script_work_unchanged():
    local = yaml.safe_load((REPO_ROOT / "users.local.yaml").read_text(encoding="utf-8"))["users"]
    assert list(yaml.safe_load(users_yaml(BRANDS))["users"]) == list(local)


@pytest.mark.parametrize("name", ["Maple & Co", "O'Hara Outfitters", 'Say "Hi"', "Café Ñandú", "back\\slash", "Levi's"])
def test_brand_names_with_awkward_characters_survive_the_round_trip(name, tmp_path):
    brands = [(name, 100)] + BRANDS[:7]
    path = tmp_path / "users.yaml"
    path.write_text(users_yaml(brands), encoding="utf-8")
    assert load_user_directory(path, salt="t").get("alice").brands[0] == name


# the command ------------------------------------------------------------------------------------------------------------------------

def test_it_writes_a_valid_users_file_whose_brands_are_all_in_the_data_and_are_the_busiest(config, local_project):
    from src.data_access.brand_check import top_brands
    from src.data_access.safe_runner import SafeQueryRunner

    target = local_project / "users.written.yaml"
    result = invoke(config, "--out", str(target))
    assert result.exit_code == 0, result.output
    settings = load_settings(config, load_env_file=False)
    directory = load_user_directory(target, salt="t")
    check_brand_mapping(directory, load_catalog(settings).catalog)  # every brand written exists in the data, spelled exactly
    busiest = [name for name, _ in top_brands(SafeQueryRunner(settings), 10) if name.strip()]
    assert directory.get("alice").brands == tuple(busiest[:2]) and directory.get("bob").brands == (busiest[2],)
    assert directory.get("dave").brands == tuple(busiest[4:6]) and directory.get("ceo").scope_mode == "all" and directory.get("nobrands").scope_mode == "none"
    assert f"alice: {busiest[0]} (" in result.output and "ceo: all brands; nobrands: none" in result.output


def test_it_uses_the_users_file_the_config_names_by_default(local_project, config):
    config.write_text("app:\n  users_file: users.default.yaml\n" + config.read_text(encoding="utf-8").replace("app:\n  language: en\n", ""), encoding="utf-8")
    result = invoke(config)
    assert result.exit_code == 0, result.output
    assert (local_project / "users.default.yaml").is_file()


def test_it_will_not_overwrite_a_file_without_force(local_project, config):
    target = local_project / "users.written.yaml"
    target.write_text("users: {}\n", encoding="utf-8")
    refused = invoke(config, "--out", str(target))
    assert refused.exit_code == 2 and "already exists" in refused.output and target.read_text(encoding="utf-8") == "users: {}\n"
    assert invoke(config, "--out", str(target), "--force").exit_code == 0 and "alice" in target.read_text(encoding="utf-8")


def test_it_needs_enough_brands_for_the_six_users(local_project, config, monkeypatch):
    from src.data_access import brand_check

    monkeypatch.setattr(brand_check, "top_brands", lambda safe, limit: BRANDS[: WANTED_BRANDS - 2])
    result = invoke(config, "--out", str(local_project / "u.yaml"))
    assert result.exit_code == 2 and f"only {WANTED_BRANDS - 2} brand(s)" in result.output and not (local_project / "u.yaml").exists()


def test_a_blank_brand_is_never_given_to_a_user(local_project, config, monkeypatch):
    from src.data_access import brand_check

    monkeypatch.setattr(brand_check, "top_brands", lambda safe, limit: [(" ", 5000)] + BRANDS)
    target = local_project / "u.yaml"
    assert invoke(config, "--out", str(target)).exit_code == 0
    assert all(brand.strip() for user in yaml.safe_load(target.read_text(encoding="utf-8"))["users"].values() for brand in user.get("brands", []))


def test_when_the_data_cannot_be_read_it_says_what_to_check_and_writes_nothing(local_project, config, tmp_path):
    config.write_text(f'bigquery:\n  backend: local\n  local_db: "{(tmp_path / "missing.duckdb").as_posix()}"\n', encoding="utf-8")
    target = local_project / "u.yaml"
    result = invoke(config, "--out", str(target))
    assert result.exit_code == 2 and "could not be read" in result.output and not target.exists()


# the config ------------------------------------------------------------------------------------------------------------------------

def test_the_shipped_bigquery_config_is_real_bigquery_with_the_local_model():
    settings = load_settings(REPO_ROOT / "config.bigquery.yaml", load_env_file=False)
    assert settings.bigquery.backend == "bigquery" and settings.bigquery.dataset == "bigquery-public-data.thelook_ecommerce"
    assert settings.app.environment == "local" and settings.llm_backend.model == "qwen/qwen3-30b-a3b-2507"
    assert settings.app.users_file == "users.bigquery.yaml" and settings.app.data_dir == "data/bigquery"  # apart from the sample data and a plain live run
    assert settings.budget.simple.seconds >= 300 and settings.golden.enabled
    assert settings.bigquery.max_bytes_billed == 536_870_912  # the cap that bounds a mistake is the default one


def test_the_three_configs_keep_their_data_apart():
    folders = {name: load_settings(REPO_ROOT / name, load_env_file=False).app.data_dir for name in ("config.yaml", "config.local.yaml", "config.bigquery.yaml")}
    assert len(set(folders.values())) == 3
