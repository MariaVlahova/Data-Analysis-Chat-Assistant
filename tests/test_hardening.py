"""Regression tests from the code review: YAML loading, .env order, logging, persona names, users file."""
import logging
import os
from pathlib import Path

import pytest

from src.access.users import load_user_directory
from src.config import load_settings
from src.errors import AgentError, ConfigError, ErrorKind
from src.logging_setup import setup_logging, shutdown_logging
from src.personas import PersonaStore


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def load(path: Path):
    return load_settings(path, load_env_file=False)


# YAML: repeated keys, encodings, odd values ---------------------------------------------------------

def test_a_repeated_section_is_an_error_not_a_silent_overwrite(tmp_path):
    path = write(tmp_path / "config.yaml", "guards:\n  k_anonymity: 10\nloop:\n  max_replans: 0\nguards:\n  rows_to_model: 5\n")
    with pytest.raises(ConfigError) as err:
        load(path)
    assert "config.yaml line 5" in err.value.message and "duplicate key 'guards'" in err.value.message


def test_a_repeated_key_inside_a_section_is_an_error_too(tmp_path):
    with pytest.raises(ConfigError, match="duplicate key 'k_anonymity'"):
        load(write(tmp_path / "config.yaml", "guards:\n  k_anonymity: 10\n  k_anonymity: 2\n"))


def test_a_repeated_user_cannot_silently_change_a_brand_scope(tmp_path):
    path = write(
        tmp_path / "users.yaml",
        'users:\n  alice: {name: Alice, role: brand_manager, brands: ["A"]}\n  alice: {name: Alice, role: brand_manager, brands: ["A", "B", "C"]}\n',
    )
    with pytest.raises(ConfigError, match="duplicate key 'alice'"):
        load_user_directory(path, salt="s")


def test_a_utf16_config_is_a_clear_startup_error_not_a_crash(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_bytes("guards:\n  k_anonymity: 10\n".encode("utf-16"))  # what PowerShell 5.1 `>` writes
    with pytest.raises(ConfigError, match="not UTF-8") as err:
        load(path)
    assert "UTF-8" in err.value.fix


def test_a_utf8_byte_order_mark_is_fine(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_bytes("﻿guards:\n  k_anonymity: 7\n".encode("utf-8"))
    assert load(path).guards.k_anonymity == 7


def test_a_non_text_key_is_a_validation_error_not_a_type_error(tmp_path):
    with pytest.raises(ConfigError, match="config.yaml line 1: 1: Keys should be strings"):
        load(write(tmp_path / "config.yaml", "1: x\n"))


def test_an_impossible_date_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="could not be read"):
        load(write(tmp_path / "config.yaml", "app:\n  language: 2020-13-45\n"))


# users.yaml fails closed ----------------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["users:\n  a: {name: A, role: ceo, all_brands: true}\n".encode("utf-16"), b"users:\n  a: \xff\xfe\n"])
def test_a_users_file_that_is_not_utf8_is_access_store_down(tmp_path, raw):
    path = tmp_path / "users.yaml"
    path.write_bytes(raw)
    with pytest.raises(AgentError) as err:
        load_user_directory(path, salt="s")
    assert err.value.kind is ErrorKind.ACCESS_STORE_DOWN
    assert "UTF-8" in err.value.detail


# .env: only the one next to the config --------------------------------------------------------------

def test_only_the_env_file_next_to_the_config_is_used_when_the_config_path_is_given(tmp_path, monkeypatch):
    project, elsewhere = tmp_path / "project", tmp_path / "other-repo"
    project.mkdir()
    elsewhere.mkdir()
    write(project / "config.yaml", "")
    write(project / ".env", "GOOGLE_CLOUD_PROJECT=project-one\n")
    write(elsewhere / ".env", "GOOGLE_CLOUD_PROJECT=wrong-project\nGEMINI_API_KEY=wrong-key\n")
    monkeypatch.chdir(elsewhere)
    try:
        settings = load_settings(project / "config.yaml")
        assert settings.gcp_project == "project-one"
        assert settings.secrets.gemini_api_key is None  # the other folder's key was never read
    finally:
        os.environ.pop("GOOGLE_CLOUD_PROJECT", None)  # load_dotenv wrote it outside monkeypatch
        os.environ.pop("GEMINI_API_KEY", None)


def test_an_unreadable_env_file_is_a_clear_startup_error(tmp_path):
    write(tmp_path / "config.yaml", "")
    (tmp_path / ".env").write_bytes("GEMINI_API_KEY=x\n".encode("utf-16"))
    with pytest.raises(ConfigError, match=r"\.env file could not be read"):
        load_settings(tmp_path / "config.yaml")


# logging: only the `src` logger ---------------------------------------------------------------------

@pytest.fixture
def settings(tmp_path):
    return load(write(tmp_path / "config.yaml", ""))


def test_setup_logging_leaves_the_root_logger_alone(settings):
    root = logging.getLogger()
    level, handlers = root.level, list(root.handlers)
    try:
        setup_logging(settings, debug=True)
        assert root.level == level and root.handlers == handlers
        assert logging.getLogger("src").level == logging.DEBUG
    finally:
        shutdown_logging()
    assert root.level == level and root.handlers == handlers
    assert logging.getLogger("src").level == logging.NOTSET
    assert logging.getLogger("src").propagate is True


def test_other_libraries_stay_out_of_the_app_log(settings):
    try:
        setup_logging(settings)
        logging.getLogger("src.demo").error("our line")
        logging.getLogger("httpx").error("third party line")
        logging.getLogger().error("root line")
    finally:
        shutdown_logging()
    text = settings.log_path.read_text(encoding="utf-8")
    assert "our line" in text
    assert "third party line" not in text and "root line" not in text


def test_setup_logging_can_run_twice_without_duplicate_lines(settings):
    try:
        setup_logging(settings)
        setup_logging(settings)
        logging.getLogger("src.demo").error("once")
    finally:
        shutdown_logging()
    assert settings.log_path.read_text(encoding="utf-8").count("once") == 1


def test_logging_before_setup_never_reaches_the_console(capsys):
    logging.getLogger("src.cli.main").error("early crash")  # no handlers yet: the NullHandler on `src` catches it
    assert capsys.readouterr().err == ""


def test_an_unwritable_log_file_does_not_turn_into_console_noise(tmp_path, capsys):
    blocker = write(tmp_path / "data", "a file where the data folder should be")
    settings = load(write(tmp_path / "config.yaml", ""))
    assert blocker.is_file()
    try:
        setup_logging(settings)
        logging.getLogger("src.demo").error("should vanish quietly")
    finally:
        shutdown_logging()
    assert capsys.readouterr().err == ""


# persona names and files ----------------------------------------------------------------------------

@pytest.fixture
def folder(tmp_path):
    path = tmp_path / "personas"
    path.mkdir()
    write(path / "default.yaml", "id: default\nversion: 1\ntone: Plain.\nmax_words: 100\n")
    return path


@pytest.mark.parametrize("name", ["../secret", "..\\secret", "../../etc/passwd", "sub/default", "Default", "", ".", "default.yaml"])
def test_persona_names_that_are_not_plain_ids_never_reach_the_filesystem(folder, name):
    write(folder.parent / "secret.yaml", "id: secret\nversion: 1\ntone: Leaked.\nmax_words: 10\n")
    with pytest.raises(ConfigError, match="Unknown persona"):
        PersonaStore(folder).get(name)


def test_an_absolute_path_is_not_a_persona_name(folder, tmp_path):
    outside = write(tmp_path / "outside.yaml", "id: outside\nversion: 1\ntone: Leaked.\nmax_words: 10\n")
    with pytest.raises(ConfigError, match="Unknown persona"):
        PersonaStore(folder).get(str(outside.with_suffix("")))


def test_a_persona_file_that_cannot_be_read_is_a_config_error_not_an_os_error(folder):
    (folder / "locked.yaml").mkdir()  # stat works, reading a folder as a file fails like a locked file does
    with pytest.raises(ConfigError, match="could not be read"):
        PersonaStore(folder).get("locked")


def test_a_persona_file_that_is_not_utf8_is_a_config_error(folder):
    (folder / "default.yaml").write_bytes(b"id: default\nversion: 1\ntone: caf\xe9\nmax_words: 10\n")
    with pytest.raises(ConfigError, match="not UTF-8"):
        PersonaStore(folder).get("default")
