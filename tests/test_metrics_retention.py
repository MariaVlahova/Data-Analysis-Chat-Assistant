"""`runs.jsonl` and `evaluations.jsonl` are kept `retention.metrics_days` (90) and no longer (the metrics purge; found by the check of what grows without a limit).

Their rows hold counts and hashes, no text, and `metrics` counts the month's BigQuery use from them, so the limit has a floor of a month. The purge runs at the start of a chat, for both
files, and is optional like every startup cleanup: a file it cannot rewrite is left as it is and the chat starts.
"""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from src.cli.main import app
from src.config import load_settings
from src.errors import ConfigError
from src.observability import metrics
from src.observability.metrics import purge_old_rows, read_runs, summarize, write_run_row

runner = CliRunner(env={"COLUMNS": "200"})
NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)


def stamp(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat(timespec="milliseconds")


def row(days_ago: float, **fields) -> dict:
    return {"ts": stamp(days_ago), "trace_id": f"{int(days_ago * 100):08x}", **fields}


def write_rows(path: Path, *rows: dict) -> None:
    for item in rows:
        write_run_row(path, item)


def trace_ids(path: Path) -> list[str]:
    return [item["trace_id"] for item in read_runs(path)[0]]


@pytest.fixture
def runs_file(tmp_path) -> Path:
    return tmp_path / "runs.jsonl"


# the function ----------------------------------------------------------------------------------------------------------------------------

def test_rows_older_than_the_limit_are_removed_and_newer_ones_stay(runs_file):
    write_rows(runs_file, row(200), row(91), row(89), row(1), row(0))
    assert purge_old_rows(runs_file, 90, now=NOW) == 2
    assert trace_ids(runs_file) == [row(89)["trace_id"], row(1)["trace_id"], row(0)["trace_id"]]


def test_a_row_at_exactly_the_limit_is_kept(runs_file):
    write_rows(runs_file, row(90))
    assert purge_old_rows(runs_file, 90, now=NOW) == 0
    assert len(read_runs(runs_file)[0]) == 1


def test_the_kept_lines_are_kept_byte_for_byte(runs_file):
    kept = json.dumps({**row(1), "note": "café   {\"a\": 1}"}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    runs_file.write_bytes(json.dumps(row(300)).encode() + b"\n" + kept + b"\n")
    purge_old_rows(runs_file, 90, now=NOW)
    assert runs_file.read_bytes() == kept + b"\n"


def test_a_line_that_cannot_be_dated_is_kept(runs_file):
    odd = [
        b'{"ts":"2020-01-01T00:00:00+00:00","trace_id":"torn',  # cut in the middle
        b'{"ts":"2020-01-01T00:00:00+00:00","bad":"\xff\xfe"}',  # not UTF-8
        b'{"trace_id":"nodate0"}',  # no ts
        b'{"ts":"last week","trace_id":"notadate"}',  # a ts that is no date
        b'{"ts":12345,"trace_id":"number00"}',  # a ts that is no text
        b'["2020-01-01T00:00:00+00:00"]',  # not an object
    ]
    runs_file.write_bytes(json.dumps(row(300)).encode() + b"\n" + b"\n".join(odd) + b"\n")
    assert purge_old_rows(runs_file, 90, now=NOW) == 1
    assert runs_file.read_bytes() == b"\n".join(odd) + b"\n"


def test_a_ts_with_no_time_zone_is_read_as_utc(runs_file):
    runs_file.write_text('{"ts":"2020-01-01T00:00:00","trace_id":"old00000"}\n{"ts":"2026-10-25T00:00:00","trace_id":"new00000"}\n', encoding="utf-8")
    assert purge_old_rows(runs_file, 90, now=NOW) == 1
    assert trace_ids(runs_file) == ["new00000"]


def test_a_file_with_nothing_to_remove_is_not_touched(runs_file):
    write_rows(runs_file, row(5), row(1))
    runs_file.write_bytes(runs_file.read_bytes() + b"\n\n")  # blank lines would be dropped by a rewrite
    before = runs_file.read_bytes()
    os.utime(runs_file, (1_000_000_000, 1_000_000_000))
    assert purge_old_rows(runs_file, 90, now=NOW) == 0
    assert runs_file.read_bytes() == before and runs_file.stat().st_mtime == 1_000_000_000
    assert [p.name for p in runs_file.parent.iterdir()] == ["runs.jsonl"]


def test_no_file_is_nothing_to_purge(tmp_path):
    assert purge_old_rows(tmp_path / "missing.jsonl", 90, now=NOW) == 0
    assert list(tmp_path.iterdir()) == []


def test_a_file_that_grew_after_it_was_read_is_left_for_the_next_start(runs_file, monkeypatch):
    write_rows(runs_file, row(300), row(1))
    appended = json.dumps(row(0, trace_id="written-meanwhile")).encode() + b"\n"
    real = Path.read_bytes

    def read_then_another_chat_appends(self):
        data = real(self)
        with open(self, "ab") as handle:
            handle.write(appended)
        return data

    monkeypatch.setattr(Path, "read_bytes", read_then_another_chat_appends)
    assert purge_old_rows(runs_file, 90, now=NOW) == 0
    monkeypatch.undo()
    assert len(read_runs(runs_file)[0]) == 3  # the old row stays for now, and the row written at the same time is not lost


def test_a_failed_replace_keeps_the_file_and_leaves_no_temp_file(runs_file, monkeypatch):
    write_rows(runs_file, row(300), row(1))
    before = runs_file.read_bytes()

    def locked(src, dst):
        raise PermissionError("another chat has the file open")

    monkeypatch.setattr(os, "replace", locked)
    with pytest.raises(PermissionError):
        purge_old_rows(runs_file, 90, now=NOW)
    assert runs_file.read_bytes() == before and [p.name for p in runs_file.parent.iterdir()] == ["runs.jsonl"]


def test_the_totals_still_read_the_purged_file(runs_file):
    this_month = datetime.now(timezone.utc)
    write_rows(
        runs_file,
        {"ts": "2020-01-01T00:00:00+00:00", "trace_id": "old00000", "outcome": "answered", "bytes_billed": 10},
        {"ts": this_month.isoformat(), "trace_id": "new00000", "outcome": "answered", "bytes_billed": 5},
    )
    purge_old_rows(runs_file, 90)
    rows, skipped = read_runs(runs_file)
    summary = summarize(rows, skipped_lines=skipped)
    assert summary.turns == 1 and summary.bytes_billed_month == 5 and skipped == 0


def test_the_module_says_the_rows_are_not_kept_for_ever():
    assert "kept without a retention limit" not in metrics.__doc__


# the setting -----------------------------------------------------------------------------------------------------------------------------

def test_the_default_is_90_days(tmp_path):
    (tmp_path / "config.yaml").write_text("app:\n  language: en\n", encoding="utf-8")
    assert load_settings(tmp_path / "config.yaml", load_env_file=False).retention.metrics_days == 90


@pytest.mark.parametrize("days, ok", [(30, False), (31, True), (365, True)])
def test_the_limit_is_at_least_a_month(tmp_path, days, ok):
    (tmp_path / "config.yaml").write_text(f"retention:\n  metrics_days: {days}\n", encoding="utf-8")
    if ok:
        assert load_settings(tmp_path / "config.yaml", load_env_file=False).retention.metrics_days == days
    else:
        with pytest.raises(ConfigError) as err:
            load_settings(tmp_path / "config.yaml", load_env_file=False)
        assert "retention.metrics_days" in err.value.message


# the chat --------------------------------------------------------------------------------------------------------------------------------

def chat(config_path: Path, user: str = "alice"):
    return runner.invoke(app, ["chat", "--user", user, "--config", str(config_path)], input="/quit\n")


def old_and_new_rows(path: Path) -> None:
    now = datetime.now(timezone.utc)
    write_rows(
        path,
        {"ts": (now - timedelta(days=120)).isoformat(), "trace_id": "old00000"},
        {"ts": (now - timedelta(days=5)).isoformat(), "trace_id": "new00000"},
    )


def test_a_chat_purges_both_files_at_startup(project, config_path):
    data = project / "data"
    data.mkdir()
    old_and_new_rows(data / "runs.jsonl")
    old_and_new_rows(data / "evaluations.jsonl")
    assert chat(config_path).exit_code == 0
    assert trace_ids(data / "runs.jsonl") == ["new00000"] and trace_ids(data / "evaluations.jsonl") == ["new00000"]


def test_the_days_come_from_the_config_and_the_files_can_be_moved(project, config_path):
    config_path.write_text(
        "retention:\n  metrics_days: 31\nobservability:\n  metrics_file: elsewhere/turns.jsonl\n  evaluations_file: elsewhere/judged.jsonl\n", encoding="utf-8"
    )
    (project / "elsewhere").mkdir()
    now = datetime.now(timezone.utc)
    for name in ("turns.jsonl", "judged.jsonl"):
        write_rows(project / "elsewhere" / name, {"ts": (now - timedelta(days=40)).isoformat(), "trace_id": "old00000"}, {"ts": (now - timedelta(days=20)).isoformat(), "trace_id": "new00000"})
    assert chat(config_path).exit_code == 0
    assert trace_ids(project / "elsewhere" / "turns.jsonl") == ["new00000"] and trace_ids(project / "elsewhere" / "judged.jsonl") == ["new00000"]


def test_a_chat_starts_when_one_file_cannot_be_purged_and_still_purges_the_other(project, config_path, monkeypatch):
    data = project / "data"
    data.mkdir()
    old_and_new_rows(data / "runs.jsonl")
    old_and_new_rows(data / "evaluations.jsonl")
    real = metrics.purge_old_rows

    def runs_file_is_locked(path, days, **kwargs):
        if path.name == "runs.jsonl":
            raise PermissionError("another chat has the file open")
        return real(path, days, **kwargs)

    monkeypatch.setattr(metrics, "purge_old_rows", runs_file_is_locked)
    result = chat(config_path)
    assert result.exit_code == 0 and "Conversation conv-" in result.output
    assert trace_ids(data / "runs.jsonl") == ["old00000", "new00000"] and trace_ids(data / "evaluations.jsonl") == ["new00000"]
    assert "startup cleanup failed (the purge of old rows of runs.jsonl)" in (data / "logs" / "app.log").read_text(encoding="utf-8")
