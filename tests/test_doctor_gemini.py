"""The Gemini checks of `doctor --live` (plan 04): the model IDs exist (A14) and one small structured call works."""
import re

import pytest
from typer.testing import CliRunner

from src.cli import doctor_gemini
from src.cli.doctor_core import DoctorContext
from src.cli.doctor_gemini import check_gemini_call, check_gemini_models
from src.cli.main import app
from src.config import Secrets, Settings
from src.llm.fake import FakeClock, FakeGenaiClient, rate_limited, sdk_error, unavailable
from src.llm.gateway import LLMGateway

runner = CliRunner(env={"COLUMNS": "250"})
LITE, FLASH = "gemini-3.5-flash-lite", "gemini-3.8-flash"
PONG = '{"ok": true}'


def context(client: FakeGenaiClient | None, *, live: bool = True, settings: Settings | None = None) -> DoctorContext:
    ctx = DoctorContext(settings=settings or Settings(), live=live)
    if client is not None:
        clock = FakeClock()
        ctx.shared["gateway"] = LLMGateway(ctx.settings, client=client, clock=clock, sleep=clock.sleep, jitter=lambda: 1.0)
    return ctx


def healthy() -> FakeGenaiClient:
    return FakeGenaiClient([LITE, FLASH, "gemini-embedding-001"]).reply(PONG, times=None)


def test_without_live_both_checks_are_skipped_and_say_how_to_run_them():
    ctx = context(healthy(), live=False)
    for check in (check_gemini_models, check_gemini_call):
        result = check(ctx)
        assert result.status == "skip" and "doctor --live" in result.detail


def test_without_a_key_the_live_checks_are_skipped_not_failed():
    ctx = DoctorContext(settings=Settings(secrets=Secrets()), live=True)
    assert check_gemini_models(ctx).status == "skip" and "GEMINI_API_KEY" in check_gemini_models(ctx).detail


def test_without_a_config_they_are_skipped():
    assert check_gemini_models(DoctorContext(settings=None, live=True)).status == "skip"


def test_all_configured_model_ids_exist():
    result = check_gemini_models(context(healthy()))
    assert result.status == "ok" and LITE in result.detail and FLASH in result.detail


def test_a_missing_model_id_fails_with_the_models_the_key_does_have_and_the_call_check_stops():
    client = FakeGenaiClient([LITE, "gemini-2.5-flash", "gemini-embedding-001", "imagen-4"]).reply(PONG, times=None)
    ctx = context(client)
    result = check_gemini_models(ctx)
    assert result.status == "fail" and FLASH in result.detail and LITE not in result.detail
    assert "gemini-2.5-flash" in result.fix and "imagen" not in result.fix and "embedding" not in result.fix and "llm.models" in result.fix
    call = check_gemini_call(ctx)
    assert call.status == "skip" and "model ID is missing" in call.detail
    assert client.requests == []  # no quota spent on a call that cannot work


def test_a_failing_model_list_is_a_failure_with_the_code_not_the_message():
    class Broken(FakeGenaiClient):
        def __init__(self):
            super().__init__()
            self.models.list = lambda: (_ for _ in ()).throw(sdk_error(403, "PERMISSION_DENIED"))

    result = check_gemini_models(context(Broken()))
    assert result.status == "fail" and "HTTP 403" in result.detail and "scripted failure" not in result.detail


def test_the_call_check_makes_one_structured_call_and_reports_the_model_tokens_and_time():
    client = healthy()
    result = check_gemini_call(context(client))
    assert result.status == "ok"
    assert LITE in result.detail and "10 tokens in, 5 out" in result.detail and "1 attempt(s)" in result.detail
    assert len(client.requests) == 1 and client.requests[0].config.response_schema is not None


def test_a_sibling_answering_is_a_warning():
    client = FakeGenaiClient([LITE, FLASH]).fail(unavailable(), model=LITE, times=3).reply(PONG, model=FLASH)
    result = check_gemini_call(context(client))
    assert result.status == "warn" and FLASH in result.detail and "sibling" in result.detail and "rate_limits" in result.fix


def test_a_failed_call_names_the_error_kind():
    client = FakeGenaiClient([LITE, FLASH]).fail(rate_limited(), times=None)
    result = check_gemini_call(context(client))
    assert result.status == "fail" and "LLM_RATE_LIMITED" in result.detail


def test_a_dead_service_costs_the_call_check_three_attempts_per_model_and_no_more():
    client = FakeGenaiClient([LITE, FLASH]).fail(unavailable(), times=None)
    result = check_gemini_call(context(client))
    assert result.status == "fail" and "LLM_UNAVAILABLE" in result.detail
    assert len(client.requests) == 6


# through the command ----------------------------------------------------------------------------

@pytest.fixture
def ready(project, monkeypatch):
    import google.auth

    monkeypatch.setattr(google.auth, "default", lambda *a, **k: (object(), "adc-project"))
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
    (project / "users.yaml").write_text('users:\n  alice: {name: A, role: brand_manager, brands: ["Brand A"]}\n', encoding="utf-8")


def line(output: str, name: str) -> str:
    return next(row for row in output.splitlines() if re.match(rf"\[.{{4}}\] {re.escape(name)}", row))


def test_a_plain_doctor_run_shows_the_gemini_checks_as_skipped_and_still_passes(config_path, ready):
    result = runner.invoke(app, ["doctor", "--config", str(config_path)])
    assert result.exit_code == 0, result.output
    assert line(result.output, "Gemini models").startswith("[skip]") and line(result.output, "Gemini call").startswith("[skip]")
    assert "All checks passed." in result.output


def test_doctor_live_runs_them(config_path, ready, monkeypatch):
    monkeypatch.setattr(doctor_gemini, "_gateway", lambda ctx: context(healthy(), settings=ctx.settings).shared["gateway"])
    result = runner.invoke(app, ["doctor", "--config", str(config_path), "--live"])
    assert result.exit_code == 0, result.output
    assert line(result.output, "Gemini models").startswith("[ ok ]") and line(result.output, "Gemini call").startswith("[ ok ]")


def test_doctor_live_exits_1_when_a_model_is_missing(config_path, ready, monkeypatch):
    client = FakeGenaiClient(["gemini-2.5-flash"])
    monkeypatch.setattr(doctor_gemini, "_gateway", lambda ctx: context(client, settings=ctx.settings).shared["gateway"])
    result = runner.invoke(app, ["doctor", "--config", str(config_path), "--live"])
    assert result.exit_code == 1
    assert line(result.output, "Gemini models").startswith("[FAIL]") and "Fix: Set llm.models" in result.output


def test_the_live_checks_run_after_the_bigquery_checks():
    from src.cli.doctor import CHECKS

    names = [fn.__name__ for fn in CHECKS]
    assert names.index("check_gemini_models") == names.index("check_string_columns") + 1
    assert names[-1] == "check_gemini_call"
