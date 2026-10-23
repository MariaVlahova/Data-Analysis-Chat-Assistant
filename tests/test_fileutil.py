"""atomic_write_text: no half files, no shared temp names, no leftovers (plan 03 review)."""
import os

import pytest

from src.fileutil import atomic_write_text


def leftovers(folder):
    return sorted(p.name for p in folder.iterdir() if p.name.endswith(".tmp"))


def test_it_writes_a_new_file_and_makes_the_folder(tmp_path):
    target = tmp_path / "cache" / "deep" / "schema.json"
    atomic_write_text(target, '{"a": 1}\n')
    assert target.read_text(encoding="utf-8") == '{"a": 1}\n' and leftovers(target.parent) == []


def test_it_replaces_an_existing_file_and_writes_utf8_with_unix_line_ends(tmp_path):
    target = tmp_path / "x.json"
    target.write_text("old", encoding="utf-8")
    atomic_write_text(target, "line one\nline two \u00e9\n")
    assert target.read_bytes() == "line one\nline two \u00e9\n".encode("utf-8")


def test_a_failed_replace_leaves_no_temp_file_and_keeps_the_old_content(tmp_path, monkeypatch):
    target = tmp_path / "x.json"
    target.write_text("old", encoding="utf-8")

    def locked(src, dst):
        raise PermissionError("held by a virus scanner")

    monkeypatch.setattr(os, "replace", locked)
    with pytest.raises(PermissionError):
        atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "old" and leftovers(tmp_path) == []


def test_a_failed_write_leaves_no_temp_file(tmp_path, monkeypatch):
    from pathlib import Path

    def disk_full(self, *args, **kwargs):
        self.touch()  # the temp file exists when the write fails
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "write_text", disk_full)
    with pytest.raises(OSError):
        atomic_write_text(tmp_path / "x.json", "new")
    assert leftovers(tmp_path) == [] and not (tmp_path / "x.json").exists()


def test_two_writers_never_share_a_temp_file(tmp_path, monkeypatch):
    seen = []
    real_replace = os.replace
    monkeypatch.setattr(os, "replace", lambda src, dst: (seen.append(os.fspath(src)), real_replace(src, dst))[1])
    for _ in range(20):
        atomic_write_text(tmp_path / "x.json", "data")
    assert len(set(seen)) == 20  # a fixed "x.json.tmp" would make two processes overwrite each other's half-written file
