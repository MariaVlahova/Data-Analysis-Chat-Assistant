import pytest
from typer.testing import CliRunner

from src.cli import doctor as doctor_module
from src.cli.main import app

runner = CliRunner(env={"COLUMNS": "200"})


@pytest.fixture
def fake_google_auth(monkeypatch):
    """Stand in for `gcloud auth application-default login` having been done."""
    import google.auth

    monkeypatch.setattr(google.auth, "default", lambda *a, **k: (object(), "adc-project"))


def run_doctor(config_path):
    return runner.invoke(app, ["doctor", "--config", str(config_path)])


def test_all_green_when_everything_is_set_up(project, config_path, monkeypatch, fake_google_auth):
    monkeypatch.setenv("GEMINI_API_KEY", "key-that-must-never-be-printed")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
    (project / "users.yaml").write_text(
        'users:\n  alice: {name: Alice, role: brand_manager, brands: ["Brand A"]}\n  ceo: {name: CEO, role: ceo, all_brands: true}\n',
        encoding="utf-8",
    )
    result = run_doctor(config_path)
    assert result.exit_code == 0, result.output
    assert "[FAIL]" not in result.output and "[warn]" not in result.output
    assert "All checks passed." in result.output
    for name in ("Python version", "Packages", "config.yaml", "users.yaml", "GEMINI_API_KEY", "Google Cloud credentials", "Google Cloud project", "SQLite database"):
        assert name in result.output
    assert "key-that-must-never-be-printed" not in result.output
    assert (project / "data" / "db.sqlite3").exists()


def test_missing_key_fails_with_a_fix_and_exit_1(config_path, fake_google_auth):
    result = run_doctor(config_path)
    assert result.exit_code == 1
    assert "[FAIL] GEMINI_API_KEY" in result.output
    assert "put GEMINI_API_KEY=... in .env" in result.output
    # the project is still found through the default credentials, so that check passes
    assert "[ ok ] Google Cloud project: adc-project" in result.output


def test_no_credentials_fails_with_the_gcloud_command(config_path, monkeypatch):
    import google.auth
    from google.auth.exceptions import DefaultCredentialsError

    def no_credentials(*args, **kwargs):
        raise DefaultCredentialsError("none")

    monkeypatch.setattr(google.auth, "default", no_credentials)
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    result = run_doctor(config_path)
    assert result.exit_code == 1
    assert "[FAIL] Google Cloud credentials" in result.output
    assert "gcloud auth application-default login" in result.output
    assert "[FAIL] Google Cloud project" in result.output


def test_a_credentials_file_that_does_not_exist_is_reported(config_path, monkeypatch, tmp_path):
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(tmp_path / "missing.json"))
    result = run_doctor(config_path)
    assert "[FAIL] Google Cloud credentials" in result.output
    assert "points to a file that does not exist" in result.output


def test_users_without_brands_or_with_placeholder_brands_are_flagged(project, config_path, monkeypatch, fake_google_auth):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "p")
    (project / "users.yaml").write_text(
        'users:\n  alice: {name: A, role: brand_manager, brands: ["<verified brand 1>"]}\n  nobrands: {name: N, role: brand_manager, brands: []}\n',
        encoding="utf-8",
    )
    result = run_doctor(config_path)
    # users.yaml is only a warning, but the chat refuses to start on a placeholder (plan 03), so the brand check fails.
    assert result.exit_code == 1
    assert "[warn] users.yaml" in result.output
    assert "no brands for: nobrands" in result.output
    assert "placeholder brand names for: alice" in result.output
    assert '[FAIL] Brand mapping: alice: "<verified brand 1>" is still a placeholder.' in result.output
    assert "Top brands by item count: Nike (1,200), Levi's (900)." in result.output


def test_a_bad_users_file_fails_and_names_the_problem(project, config_path):
    (project / "users.yaml").write_text("users:\n  eve: {name: Eve, role: brand_manager, all_brands: true}\n", encoding="utf-8")
    result = run_doctor(config_path)
    assert result.exit_code == 1
    assert "[FAIL] users.yaml" in result.output and "only allowed for role: ceo" in result.output


def test_a_missing_users_file_fails(project, config_path):
    (project / "users.yaml").unlink()
    result = run_doctor(config_path)
    assert result.exit_code == 1
    assert "[FAIL] users.yaml" in result.output


def test_a_bad_config_fails_but_the_other_checks_still_run(config_path, monkeypatch):
    config_path.write_text("loop:\n  max_replans: 9\n", encoding="utf-8")
    result = run_doctor(config_path)
    assert result.exit_code == 1
    assert "[FAIL] config.yaml" in result.output and "loop.max_replans" in result.output
    assert "[skip] users.yaml" in result.output and "[skip] SQLite database" in result.output
    assert "[ ok ] Python version" in result.output


def test_a_foreign_database_is_reported_with_the_fix(project, config_path):
    import sqlite3

    (project / "data").mkdir()
    foreign = sqlite3.connect(project / "data" / "db.sqlite3")
    foreign.execute("CREATE TABLE conversations (id TEXT, username TEXT)")
    foreign.commit()
    foreign.close()
    result = run_doctor(config_path)
    assert "[FAIL] SQLite database" in result.output
    assert "Move or delete db.sqlite3" in result.output


def test_a_crashing_check_becomes_a_failed_check(config_path, monkeypatch):
    def broken(ctx):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(doctor_module, "CHECKS", [broken])
    result = run_doctor(config_path)
    assert result.exit_code == 1
    assert "[FAIL] broken: the check crashed: RuntimeError" in result.output
    assert "kaboom" not in result.output


def test_later_plans_can_register_checks(config_path, monkeypatch):
    monkeypatch.setattr(doctor_module, "CHECKS", [])
    doctor_module.register_check(lambda ctx: doctor_module.Check("Extra", "ok", "added by a later plan"))
    result = run_doctor(config_path)
    assert result.exit_code == 0 and "[ ok ] Extra: added by a later plan" in result.output


def test_a_utf16_config_fails_the_config_check_but_doctor_still_prints_every_check(config_path):
    config_path.write_bytes("app: {}\n".encode("utf-16"))
    result = run_doctor(config_path)
    assert result.exit_code == 1
    assert "[FAIL] config.yaml" in result.output and "not UTF-8" in result.output
    assert "[ ok ] Python version" in result.output and "Traceback" not in result.output


def test_doctor_survives_a_config_loader_that_blows_up(config_path, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("src.cli.main.load_settings", boom)
    result = run_doctor(config_path)
    assert result.exit_code == 1
    assert "[FAIL] config.yaml" in result.output and "RuntimeError" in result.output
    assert "[ ok ] Python version" in result.output and "Traceback" not in result.output


def test_a_utf16_users_file_fails_the_users_check(project, config_path):
    (project / "users.yaml").write_bytes("users: {}\n".encode("utf-16"))
    result = run_doctor(config_path)
    assert result.exit_code == 1
    assert "[FAIL] users.yaml" in result.output and "UTF-8" in result.output
