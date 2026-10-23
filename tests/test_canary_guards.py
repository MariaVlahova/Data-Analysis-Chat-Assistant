"""The canary test for the Result Guard and the Output Guard together (plan 06).

Plant an e-mail address, a street address and a real customer ID in raw rows. None of them may appear in the
`GuardedResult`, in what a Composer builds from it, in the trace file, the log file, the metrics file, the database or
on the screen. Plan 09 runs the same canaries against all layers with the real graph.
"""
import dataclasses
import json
import logging
import pickle

import pandas as pd
import pytest
from typer.testing import CliRunner

from src.cli import main as cli_main
from src.cli.main import app
from src.config import Settings
from src.errors import AgentError, ErrorKind
from src.guards.output_guard import OutputGuard
from src.guards.pseudonymizer import SessionPseudonymizer
from src.guards.result_guard import GuardedResult, ResultGuard
from tests.guard_support import issue_query, make_allowlist

CANARY = "canary@example.test"
STREET = "42 Wallaby Way"
REAL_ID = "4821377"
SECRETS = (CANARY, "Wallaby", REAL_ID)
runner = CliRunner(env={"COLUMNS": "200"})
log = logging.getLogger("src.canary_test")


def raw_rows() -> pd.DataFrame:
    return pd.DataFrame({
        "user_id": [int(REAL_ID), 77],
        "spend": [310.5, 120.25],
        "note": [f"vip, mail {CANARY}, ships to {STREET}, Sydney", "regular"],
        "email": [CANARY, "other@example.test"],
    })


RANKED = issue_query(per_customer=True, id_columns=("user_id",))


def result_guard() -> ResultGuard:
    return ResultGuard(Settings(), make_allowlist(), SessionPseudonymizer())


def compose(result: GuardedResult) -> str:
    """A stand-in for the Composer: it sees the guarded result and nothing else."""
    lines = [*result.notices, ""]
    lines += [", ".join(f"{column}: {row[column]}" for column in result.columns) for row in result.rows]
    return "\n".join(lines)


def test_nothing_the_canary_touched_is_in_the_guarded_result_or_what_is_built_from_it():
    result = result_guard().apply(raw_rows(), RANKED, 2)
    everything = json.dumps(dataclasses.asdict(result)) + repr(result) + pickle.dumps(result).decode("latin-1") + compose(result)
    for secret in SECRETS:
        assert secret not in everything
    assert "[EMAIL_MASKED]" in result.rows[0]["note"] and "[ADDRESS_MASKED]" in result.rows[0]["note"]
    assert OutputGuard().check(compose(result))  # and the final answer passes its scan


def test_without_the_result_guard_the_output_guard_still_blocks_the_canary():
    raw = raw_rows()
    draft = f"{raw.loc[0, 'note']} and {raw.loc[1, 'note']}"  # what a Composer would write from raw rows
    with pytest.raises(AgentError) as err:
        OutputGuard().check(draft)
    assert err.value.kind == ErrorKind.OUTPUT_BLOCKED


def test_nothing_the_canary_touched_is_on_the_screen_or_on_disk_after_a_chat_turn(project, config_path, monkeypatch):
    pseudonymizer = SessionPseudonymizer()  # per session; reaches the node through a closure, never the session or the state
    captured: dict = {}

    def turn(session, text, *, trace_id, progress):
        progress("Looking up the numbers...")
        guard = ResultGuard(session.settings, make_allowlist(), pseudonymizer)
        result = guard.apply(raw_rows(), RANKED, 2)  # the raw frame is a local of this function
        captured["result"] = result
        log.warning("guarded result: %r", result)  # a careless log line carries only what the guard let through
        return OutputGuard().check(compose(result))

    monkeypatch.setattr(cli_main, "run_turn", turn)
    result = runner.invoke(app, ["chat", "--user", "alice", "--config", str(config_path)], input="who are my top customers?\n/quit\n")
    assert result.exit_code == 0, result.output

    assert "Customer #" in result.output and "Customer numbers are labels for this session only." in result.output
    for secret in SECRETS:
        assert secret not in result.output
        assert secret not in json.dumps(dataclasses.asdict(captured["result"]))

    files = [path for path in (project / "data").rglob("*") if path.is_file()]
    names = {path.name for path in files}
    assert "app.log" in names and "runs.jsonl" in names and any(path.suffix == ".jsonl" and path.parent.name == "traces" for path in files)
    for path in files:
        data = path.read_bytes()
        for secret in SECRETS:
            assert secret.encode() not in data, f"{secret} found in {path.relative_to(project)}"
    assert json.loads((project / "data" / "runs.jsonl").read_text(encoding="utf-8").splitlines()[0])["pii_at_output"] == 0
