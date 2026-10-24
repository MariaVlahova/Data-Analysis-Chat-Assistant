import os
from pathlib import Path

import pytest

from src.config import Settings, load_settings
from src.errors import ConfigError

REPO = Path(__file__).resolve().parent.parent
NON_YAML = {"secrets", "base_dir", "config_path"}


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def load(path: Path) -> Settings:
    return load_settings(path, load_env_file=False)


def test_defaults_load_from_an_empty_file(tmp_path):
    settings = load(write(tmp_path / "config.yaml", ""))
    assert settings.guards.k_anonymity == 10
    assert settings.guards.rows_to_model == 50
    assert settings.loop.max_replans == 0
    assert settings.bigquery.dataset == "bigquery-public-data.thelook_ecommerce"
    assert settings.bigquery.max_bytes_billed == 536_870_912
    assert settings.budget.simple.calls == 8 and settings.budget.report.seconds == 90
    assert settings.persona.enabled is True


def test_shipped_config_yaml_matches_the_defaults_in_code():
    shipped = load(REPO / "config.yaml")
    assert shipped.model_dump(exclude=NON_YAML) == Settings().model_dump(exclude=NON_YAML)


def test_unknown_key_stops_startup_and_names_file_line_and_key(tmp_path):
    path = write(tmp_path / "config.yaml", "guards:\n  k_anonymity: 10\n  k_anonymty: 5\n")
    with pytest.raises(ConfigError) as err:
        load(path)
    assert "config.yaml line 3" in err.value.message
    assert "guards.k_anonymty" in err.value.message
    assert "unknown key" in err.value.message


def test_unknown_top_level_section_fails(tmp_path):
    with pytest.raises(ConfigError, match="gaurds"):
        load(write(tmp_path / "config.yaml", "gaurds: {}\n"))


@pytest.mark.parametrize(
    "text, key",
    [
        ("loop:\n  max_replans: 3\n", "loop.max_replans"),
        ("loop:\n  max_replans: -1\n", "loop.max_replans"),
        ("guards:\n  k_anonymity: 1\n", "guards.k_anonymity"),
        ("guards:\n  rows_to_model: 0\n", "guards.rows_to_model"),
        ("bigquery:\n  max_bytes_billed: 0\n", "bigquery.max_bytes_billed"),
        ("bigquery:\n  query_timeout_s: -5\n", "bigquery.query_timeout_s"),
        ("budget:\n  simple: {calls: 0, seconds: 60}\n", "budget.simple.calls"),
        ("budget:\n  bytes_per_turn: -1\n", "budget.bytes_per_turn"),
        ("retention:\n  audit_days: 0\n", "retention.audit_days"),
        ("retention:\n  trace_days: 0\n", "retention.trace_days"),
        ("composer:\n  numeric_retry: 2\n", "composer.numeric_retry"),
    ],
)
def test_validation_rules(tmp_path, text, key):
    with pytest.raises(ConfigError) as err:
        load(write(tmp_path / "config.yaml", text))
    assert key in err.value.message


def test_max_replans_ceiling_is_two(tmp_path):
    assert load(write(tmp_path / "config.yaml", "loop:\n  max_replans: 2\n")).loop.max_replans == 2


def test_yaml_syntax_error_reports_the_line(tmp_path):
    with pytest.raises(ConfigError) as err:
        load(write(tmp_path / "config.yaml", "guards:\n  k_anonymity: [10\n"))
    assert "config.yaml line" in err.value.message


def test_missing_config_file_is_a_clear_startup_error(tmp_path):
    with pytest.raises(ConfigError, match="not found") as err:
        load(tmp_path / "nope.yaml")
    assert err.value.fix


def test_path_order_is_flag_then_env_then_default(tmp_path, monkeypatch):
    explicit = write(tmp_path / "explicit.yaml", "guards:\n  k_anonymity: 3\n")
    from_env = write(tmp_path / "env.yaml", "guards:\n  k_anonymity: 4\n")
    write(tmp_path / "config.yaml", "guards:\n  k_anonymity: 5\n")
    monkeypatch.chdir(tmp_path)

    assert load_settings(load_env_file=False).guards.k_anonymity == 5
    monkeypatch.setenv("OPSFLEET_CONFIG", str(from_env))
    assert load_settings(load_env_file=False).guards.k_anonymity == 4
    assert load_settings(explicit, load_env_file=False).guards.k_anonymity == 3


def test_relative_paths_resolve_against_the_config_folder_not_the_cwd(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    write(project / "config.yaml", "")
    monkeypatch.chdir(tmp_path)
    settings = load(project / "config.yaml")
    assert settings.db_path == project.resolve() / "data" / "db.sqlite3"
    assert settings.resolve("users.yaml") == project.resolve() / "users.yaml"


def test_secrets_come_from_the_environment_and_never_show_in_repr(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "super-secret-key-123")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project-999")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/keys/sa-private.json")
    settings = load(write(tmp_path / "config.yaml", ""))

    assert settings.secrets.gemini_api_key.get_secret_value() == "super-secret-key-123"
    assert settings.gcp_project == "my-project-999"
    for text in (repr(settings), str(settings), repr(settings.secrets), settings.model_dump_json()):
        assert "super-secret-key-123" not in text
        assert "my-project-999" not in text
        assert "sa-private.json" not in text


def test_config_project_id_wins_over_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "from-env")
    settings = load(write(tmp_path / "config.yaml", "bigquery:\n  project_id: from-config\n"))
    assert settings.gcp_project == "from-config"


def test_blank_environment_values_count_as_unset(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "   ")
    assert load(write(tmp_path / "config.yaml", "")).secrets.gemini_api_key is None


@pytest.mark.parametrize("key", ["secrets", "base_dir", "config_path"])
def test_secrets_and_internal_fields_cannot_come_from_yaml(tmp_path, key):
    with pytest.raises(ConfigError, match=key):
        load(write(tmp_path / "config.yaml", f"{key}: {{}}\n"))


def test_dotenv_fills_in_missing_variables_but_never_overrides_real_ones(tmp_path, monkeypatch):
    write(tmp_path / ".env", "GEMINI_API_KEY=from-dotenv\nGOOGLE_CLOUD_PROJECT=dotenv-project\n")
    write(tmp_path / "config.yaml", "")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "real-env-project")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    try:
        settings = load_settings(tmp_path / "config.yaml")
        assert settings.secrets.gemini_api_key.get_secret_value() == "from-dotenv"
        assert settings.gcp_project == "real-env-project"
    finally:
        import os

        os.environ.pop("GEMINI_API_KEY", None)  # load_dotenv wrote it outside monkeypatch

