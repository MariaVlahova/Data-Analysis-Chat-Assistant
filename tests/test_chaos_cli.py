"""The --chaos and --chaos-seed flags (plan 04, F37): they build a seeded Chaos, show the seed, and stay light."""
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from src.cli import main as cli_main
from src.cli.main import app
from src.cli.render import Renderer
from tests.test_cli_imports import ROOT

runner = CliRunner(env={"COLUMNS": "200"})


def chat(config_path, *extra: str):
    return runner.invoke(app, ["chat", "--user", "alice", "--config", str(config_path), *extra], input="/quit\n")


def open_session(config_path, **options):
    return cli_main.open_session(
        Renderer(), "alice", config_path, debug=False, conversation=None,
        chaos=options.get("chaos"), chaos_seed=options.get("chaos_seed"),
    )


def test_the_banner_shows_the_level_and_the_seed_to_replay_the_run(config_path):
    result = chat(config_path, "--chaos", "harsh", "--chaos-seed", "7")
    assert result.exit_code == 0, result.output
    assert "Chaos mode harsh" in result.output and "--chaos harsh --chaos-seed 7" in result.output


def test_the_banner_says_faults_are_injected_now_that_the_agent_consumes_the_engine(config_path):
    """The banner used to say nothing was injected, because the turn only echoed. Plan 07 connected the engine."""
    output = " ".join(chat(config_path, "--chaos", "mild", "--chaos-seed", "3").output.split())
    assert "faults are injected on purpose" in output
    assert "not connected yet" not in output and "nothing is injected" not in output


def test_without_a_seed_one_is_drawn_and_shown(config_path):
    result = chat(config_path, "--chaos", "mild")
    assert result.exit_code == 0, result.output
    assert "--chaos mild --chaos-seed " in result.output and "--chaos-seed None" not in result.output


def test_no_chaos_line_without_the_flag(config_path):
    assert "Chaos mode" not in chat(config_path).output


def test_a_seed_without_a_level_is_a_startup_problem_not_silently_ignored(project, config_path):
    result = chat(config_path, "--chaos-seed", "7")
    assert result.exit_code == 2
    assert "--chaos-seed has no effect without --chaos" in result.output
    assert not (project / "data" / "db.sqlite3").exists()  # refused before anything was set up


def test_the_session_carries_the_chaos_engine_with_the_seed_in_use(config_path):
    session = open_session(config_path, chaos=cli_main.ChaosLevel.harsh, chaos_seed=99)
    try:
        assert session.chaos == "harsh" and session.chaos_seed == 99
        assert session.chaos_engine is not None and session.chaos_engine.seed == 99 and session.chaos_engine.level == "harsh"
    finally:
        session.db.close()


def test_without_the_flag_there_is_no_engine(config_path):
    session = open_session(config_path)
    try:
        assert session.chaos is None and session.chaos_engine is None
    finally:
        session.db.close()


def loaded_after(imports: str, tmp_path) -> str:
    script = (
        f"import sys; sys.path.insert(0, {str(ROOT)!r})\n{imports}\n"
        "print('LOADED:', [m for m in ('google.genai', 'httpx') if m in sys.modules])\n"
    )
    result = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, text=True, cwd=tmp_path, timeout=120)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_the_cli_does_not_load_the_google_sdk_for_the_chaos_flag(tmp_path):
    assert "LOADED: []" in loaded_after("import src.cli.main, src.cli.commands, src.cli.doctor", tmp_path)


def test_the_light_parts_of_the_llm_package_do_not_load_the_sdk_but_the_gateway_does(tmp_path):
    """Review finding: `src/llm/__init__.py` imported the gateway eagerly, so importing src.llm.chaos loaded the SDK anyway."""
    for light in ("src.llm.chaos", "src.llm.budget", "src.llm.limiter", "src.llm.breaker", "src.llm.routing", "src.llm_steps"):
        assert "LOADED: []" in loaded_after(f"import {light}", tmp_path), light
    assert "LOADED: []" in loaded_after("from src.llm import Chaos, TurnBudget, Step", tmp_path)
    assert "google.genai" in loaded_after("from src.llm import LLMGateway", tmp_path)
    assert "google.genai" in loaded_after("import src.llm.fake", tmp_path)


def test_the_names_of_the_llm_package_resolve_on_first_use_and_unknown_names_are_an_error():
    import src.llm

    assert src.llm.TurnBudget.__module__ == "src.llm.budget" and src.llm.Chaos.__module__ == "src.llm.chaos"
    assert {"LLMGateway", "TurnBudget", "Step"} <= set(dir(src.llm)) and set(src.llm.__all__) <= set(dir(src.llm))
    with pytest.raises(AttributeError):
        src.llm.NoSuchName
