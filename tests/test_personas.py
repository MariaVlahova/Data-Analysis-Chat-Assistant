import os
import time

import pytest

from src.errors import ConfigError
from src.personas import PersonaStore


def persona_text(version=1, tone="Plain.", pid="default", extra="") -> str:
    return f"id: {pid}\nversion: {version}\ntone: {tone}\nsections: [Summary]\nmax_words: 100\n{extra}"


@pytest.fixture
def folder(tmp_path):
    path = tmp_path / "personas"
    path.mkdir()
    (path / "default.yaml").write_text(persona_text(), encoding="utf-8")
    return path


def test_label_has_id_version_and_a_short_hash(folder):
    persona = PersonaStore(folder).get("default")
    assert persona.label.startswith("default@v1+")
    assert len(persona.label.split("+")[1]) == 8
    assert "Plain." in persona.prompt_block() and "100 words" in persona.prompt_block()


def test_an_edited_file_is_picked_up_without_a_restart(folder):
    store = PersonaStore(folder)
    before = store.get("default")
    assert store.get("default") is before  # unchanged file: served from the cache

    path = folder / "default.yaml"
    path.write_text(persona_text(version=2, tone="Much warmer."), encoding="utf-8")
    os.utime(path, ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))  # make the change visible on coarse clocks
    after = store.get("default")
    assert after.file.tone == "Much warmer." and after.label.startswith("default@v2+")
    assert after.label != before.label


def test_same_version_with_different_text_still_changes_the_hash(folder):
    store = PersonaStore(folder)
    first = store.get("default").label
    (folder / "default.yaml").write_text(persona_text(tone="Changed but same version."), encoding="utf-8")
    assert store.get("default").label != first


def test_unknown_persona_lists_the_available_ones(folder):
    with pytest.raises(ConfigError, match="Available: default"):
        PersonaStore(folder).get("nope")


def test_tone_is_limited_to_2000_characters(folder):
    (folder / "default.yaml").write_text(persona_text(tone="x" * 2001), encoding="utf-8")
    with pytest.raises(ConfigError, match="default.yaml.*tone"):
        PersonaStore(folder).get("default")
    (folder / "default.yaml").write_text(persona_text(tone="x" * 2000), encoding="utf-8")
    assert PersonaStore(folder).get("default")


@pytest.mark.parametrize(
    "text",
    [
        persona_text(version=0),
        persona_text(extra="status: active\n"),  # no lifecycle: a persona is just a file
        persona_text(extra="mood: grumpy\n"),
        persona_text(pid="Not Valid"),
        "id: default\n",
    ],
)
def test_invalid_persona_files_are_rejected(folder, text):
    (folder / "default.yaml").write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError):
        PersonaStore(folder).get("default")


def test_id_must_match_the_file_name(folder):
    (folder / "default.yaml").write_text(persona_text(pid="other"), encoding="utf-8")
    with pytest.raises(ConfigError, match="must match the file name"):
        PersonaStore(folder).get("default")


def test_the_shipped_persona_file_is_valid():
    from pathlib import Path

    store = PersonaStore(Path(__file__).resolve().parent.parent / "personas")
    assert store.names() == ["default"]
    assert store.get("default").label.startswith("default@v1+")
