"""The local model backend (the `local` environment): the OpenAI-compatible client, the gateway on top of it, and the environment profiles.

A server is played by `httpx.MockTransport`, so nothing here touches the network. The gateway is the real one, with a fake clock, so the retry,
breaker and rate-limit code is the same code that runs against Gemini.
"""
import json

import httpx
import pytest
from pydantic import BaseModel
from typer.testing import CliRunner

from src.cli.main import app, banner
from src.config import ENVIRONMENTS, LOCAL_MODEL, LlmBackend, LlmConfig, Settings, load_settings
from src.errors import AgentError, ConfigError, ErrorKind
from src.llm.fake import FakeClock
from src.llm.gateway import REASK_NOTE, SETUP_MESSAGE, LLMGateway
from src.llm.budget import TurnBudget
from src.llm.openai_compat import CompatClient, CompatHTTPError
from src.llm.routing import Router, Step

BASE = "http://model.test/v1"
GOOD = '{"sql": "SELECT 1", "interpretation": "all orders"}'
runner = CliRunner(env={"COLUMNS": "250"})


class Answer(BaseModel):
    sql: str
    interpretation: str


def completion(text: str = GOOD, *, finish: str = "stop", prompt_tokens: int = 40, completion_tokens: int = 12, **message) -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text, **message}, "finish_reason": finish}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    })


class Server:
    """A scripted server: each request takes the next reply (a response, or an exception to raise). It keeps the requests it saw."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, BaseException):
            raise reply
        return reply

    @property
    def bodies(self) -> list[dict]:
        return [json.loads(request.content) for request in self.requests]

    def client(self, api_key=None) -> CompatClient:
        return CompatClient(BASE, api_key, transport=httpx.MockTransport(self))


def local_settings(**llm) -> Settings:
    return Settings(app={"environment": "local"}, llm=LlmConfig(**llm))


def gateway_for(server: Server, settings: Settings | None = None, **options):
    clock = FakeClock()
    gateway = LLMGateway(
        settings or local_settings(), compat_client=server.client(), clock=clock, sleep=clock.sleep, jitter=lambda: 1.0, **options,
    )
    return gateway, clock


def ask(gateway, clock, step=Step.SQL_WRITE, *, calls=8, seconds=60):
    return gateway.generate(step, system="Write SQL.", contents="revenue by brand", schema=Answer, budget=TurnBudget(calls=calls, seconds=seconds, bytes_=0, clock=clock))


# the client ----------------------------------------------------------------------------------------------------------------

def test_a_request_is_a_chat_completion_with_the_json_schema_the_step_expects():
    server = Server(completion())
    reply = server.client().chat(model="m", system="be brief", contents="hello", schema=Answer, temperature=0.0, max_tokens=300, timeout=5)
    (body,) = server.bodies
    assert str(server.requests[0].url) == f"{BASE}/chat/completions"
    assert body["model"] == "m" and body["temperature"] == 0.0 and body["max_tokens"] == 300 and body["stream"] is False
    assert body["messages"] == [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hello"}]
    schema = body["response_format"]["json_schema"]
    assert body["response_format"]["type"] == "json_schema" and schema["name"] == "Answer" and schema["strict"] is True
    assert set(schema["schema"]["properties"]) == {"sql", "interpretation"}
    assert (reply.text, reply.tokens_in, reply.tokens_out, reply.finish) == (GOOD, 40, 12, "STOP")


def test_free_text_has_no_response_format():
    server = Server(completion("hello"))
    server.client().chat(model="m", system="s", contents="c", schema=None, temperature=0.3, max_tokens=10, timeout=5)
    assert "response_format" not in server.bodies[0]


def test_a_list_of_parts_is_sent_as_one_message():
    server = Server(completion())
    server.client().chat(model="m", system="s", contents=["one", "two"], schema=None, temperature=0, max_tokens=10, timeout=5)
    assert server.bodies[0]["messages"][1]["content"] == "one\n\ntwo"


def test_a_key_is_sent_only_when_there_is_one():
    with_key, without = Server(completion()), Server(completion())
    with_key.client("secret-key").chat(model="m", system="s", contents="c", schema=None, temperature=0, max_tokens=1, timeout=1)
    without.client().chat(model="m", system="s", contents="c", schema=None, temperature=0, max_tokens=1, timeout=1)
    assert with_key.requests[0].headers["authorization"] == "Bearer secret-key" and "authorization" not in without.requests[0].headers


def test_the_key_is_read_from_the_variable_the_config_names(monkeypatch):
    monkeypatch.setenv("MY_LOCAL_KEY", " abc ")
    server = Server(completion())
    backend = LlmBackend(provider="openai_compatible", model="m", api_key_env="MY_LOCAL_KEY")
    CompatClient.from_backend(backend, transport=httpx.MockTransport(server)).chat(model="m", system="s", contents="c", schema=None, temperature=0, max_tokens=1, timeout=1)
    assert server.requests[0].headers["authorization"] == "Bearer abc"


@pytest.mark.parametrize("raw,mapped", [("stop", "STOP"), ("length", "MAX_TOKENS"), ("tool_calls", "STOP")])
def test_the_finish_reason_is_given_in_the_words_the_gateway_knows(raw, mapped):
    reply = Server(completion(finish=raw)).client().chat(model="m", system="s", contents="c", schema=None, temperature=0, max_tokens=1, timeout=1)
    assert reply.finish == mapped


def test_a_thinking_block_is_not_part_of_the_answer():
    reply = Server(completion("<think>let me work it out\nstep by step</think>\n" + GOOD)).client().chat(model="m", system="s", contents="c", schema=Answer, temperature=0, max_tokens=9, timeout=1)
    assert reply.text == GOOD


def test_a_content_filter_and_a_refusal_are_blocks():
    filtered = Server(completion("", finish="content_filter")).client().chat(model="m", system="s", contents="c", schema=None, temperature=0, max_tokens=1, timeout=1)
    refused = Server(completion("", refusal="I cannot help with that.")).client().chat(model="m", system="s", contents="c", schema=None, temperature=0, max_tokens=1, timeout=1)
    assert filtered.blocked and "content filter" in filtered.blocked and refused.blocked == "the model declined the request"
    assert "cannot help" not in refused.blocked  # what the model said is not kept


def test_missing_usage_counts_as_zero():
    reply = Server(httpx.Response(200, json={"choices": [{"message": {"content": "x"}, "finish_reason": "stop"}]})).client().chat(
        model="m", system="s", contents="c", schema=None, temperature=0, max_tokens=1, timeout=1)
    assert (reply.tokens_in, reply.tokens_out) == (0, 0)


@pytest.mark.parametrize("code,word", [(400, "BAD_REQUEST"), (401, "UNAUTHENTICATED"), (404, "NOT_FOUND"), (429, "RESOURCE_EXHAUSTED"), (503, "UNAVAILABLE"), (418, "")])
def test_an_error_status_becomes_a_typed_error_with_a_fixed_word_and_never_the_servers_text(code, word):
    server = Server(httpx.Response(code, json={"error": {"message": "the prompt said canary@example.test"}}))
    with pytest.raises(CompatHTTPError) as caught:
        server.client().chat(model="m", system="s", contents="c", schema=None, temperature=0, max_tokens=1, timeout=1)
    assert caught.value.code == code and caught.value.status == word and "canary" not in str(caught.value)


@pytest.mark.parametrize("response", [
    httpx.Response(200, text="<html>not json</html>"), httpx.Response(200, json={"choices": []}), httpx.Response(200, json={"nope": 1}),
    httpx.Response(200, json=[1, 2]),
])
def test_a_200_that_is_not_a_chat_completion_is_a_bad_gateway(response):
    with pytest.raises(CompatHTTPError) as caught:
        Server(response).client().chat(model="m", system="s", contents="c", schema=None, temperature=0, max_tokens=1, timeout=1)
    assert caught.value.code == 502


def test_list_models_reads_the_ids():
    server = Server(httpx.Response(200, json={"data": [{"id": "qwen/qwen3-30b-a3b-2507"}, {"id": "other"}]}))
    assert server.client().list_models() == {"qwen/qwen3-30b-a3b-2507", "other"}
    assert str(server.requests[0].url) == f"{BASE}/models"
    with pytest.raises(CompatHTTPError):
        Server(httpx.Response(500)).client().list_models()


# the gateway on the local backend ------------------------------------------------------------------------------------------

def test_a_structured_call_goes_to_the_local_model_and_needs_no_gemini_key():
    server = Server(completion(prompt_tokens=100, completion_tokens=25))
    gateway, clock = gateway_for(server)
    assert gateway._settings.secrets.gemini_api_key is None
    result = ask(gateway, clock)
    assert result.parsed == Answer(sql="SELECT 1", interpretation="all orders") and result.model_used == LOCAL_MODEL
    assert (result.tokens_in, result.tokens_out, result.attempts, result.fallback_used) == (100, 25, 1, False)
    body = server.bodies[0]
    assert body["model"] == LOCAL_MODEL and body["temperature"] == 0 and body["max_tokens"] == 2048  # the SQL writer's settings, as for Gemini


@pytest.mark.parametrize("step", list(Step))
def test_every_step_uses_the_one_local_model_and_there_is_no_sibling(step):
    assert Router(local_settings().llm, local_settings().llm_backend).chain(step) == (LOCAL_MODEL,)


def test_the_gemini_chains_are_unchanged_for_every_other_environment():
    llm = LlmConfig()
    for environment in ("default", "develop", "staging", "prod"):
        settings = Settings(app={"environment": environment})
        router = Router(settings.llm, settings.llm_backend)
        assert router.chain(Step.INPUT_GUARD) == (llm.models.light, llm.models.main)
        assert router.chain(Step.SQL_WRITE) == (llm.models.main, llm.models.light)


def test_a_burst_is_not_throttled_by_the_gemini_free_tier_limit():
    """The Gemini default is 10 requests a minute. A local server has no such limit: 15 calls go through without a wait."""
    server = Server(completion())
    gateway, clock = gateway_for(server)
    before = clock()
    budget = TurnBudget(calls=20, seconds=600, bytes_=0, clock=clock)
    for _ in range(15):
        gateway.generate(Step.SQL_WRITE, system="s", contents="c", schema=Answer, budget=budget)
    assert len(server.requests) == 15 and clock() == before


def test_the_rpm_of_the_backend_still_bounds_a_burst():
    settings = Settings(app={"environment": "local"}, llm=LlmConfig(environments={"local": {"rpm": 2}}))
    server = Server(completion())
    gateway, clock = gateway_for(server, settings)
    before = clock()
    budget = TurnBudget(calls=20, seconds=600, bytes_=0, clock=clock)
    for _ in range(3):
        gateway.generate(Step.SQL_WRITE, system="s", contents="c", schema=Answer, budget=budget)
    assert clock() - before >= 59  # the third call waited for a slot


def test_a_429_is_retried_after_a_wait_and_then_succeeds():
    server = Server(httpx.Response(429), completion())
    gateway, clock = gateway_for(server)
    result = ask(gateway, clock)
    assert result.attempts == 2 and len(server.requests) == 2


def test_a_server_error_that_keeps_coming_is_llm_unavailable_after_the_configured_attempts():
    server = Server(httpx.Response(503))
    gateway, clock = gateway_for(server)
    with pytest.raises(AgentError) as caught:
        ask(gateway, clock)
    assert caught.value.kind is ErrorKind.LLM_UNAVAILABLE and f"{LOCAL_MODEL}: server_error" in caught.value.detail
    assert len(server.requests) == 3  # llm.retry.attempts


def test_a_connection_refused_is_unavailable_not_a_crash():
    server = Server(httpx.ConnectError("refused"))
    gateway, clock = gateway_for(server)
    with pytest.raises(AgentError) as caught:
        ask(gateway, clock)
    assert caught.value.kind is ErrorKind.LLM_UNAVAILABLE and f"{LOCAL_MODEL}: network" in caught.value.detail


def test_a_slow_server_is_a_timeout_that_is_retried():
    server = Server(httpx.ReadTimeout("slow"), completion())
    gateway, clock = gateway_for(server)
    assert ask(gateway, clock).attempts == 2


def test_the_attempt_timeout_is_the_config_value_cut_to_the_turn():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"]["read"])
        return completion()

    clock = FakeClock()
    gateway = LLMGateway(local_settings(request_timeout_s=300), compat_client=CompatClient(BASE, transport=httpx.MockTransport(handler)), clock=clock, sleep=clock.sleep)
    gateway.generate(Step.SQL_WRITE, system="s", contents="c", schema=Answer, budget=TurnBudget(calls=3, seconds=500, bytes_=0, clock=clock))
    gateway.generate(Step.SQL_WRITE, system="s", contents="c", schema=Answer, budget=TurnBudget(calls=3, seconds=40, bytes_=0, clock=clock))
    assert seen == [300, 40]


def test_a_model_the_server_does_not_have_is_a_setup_problem_not_a_retry():
    server = Server(httpx.Response(404, json={"error": "model not found"}))
    gateway, clock = gateway_for(server)
    with pytest.raises(AgentError) as caught:
        ask(gateway, clock)
    assert caught.value.kind is ErrorKind.LLM_UNAVAILABLE and caught.value.user_message == SETUP_MESSAGE
    assert len(server.requests) == 1


def test_a_request_the_server_refuses_is_a_setup_problem_and_ends_the_call():
    server = Server(httpx.Response(400))
    gateway, clock = gateway_for(server)
    with pytest.raises(AgentError) as caught:
        ask(gateway, clock)
    assert caught.value.user_message == SETUP_MESSAGE and len(server.requests) == 1


def test_a_reply_that_is_not_the_schema_is_asked_again_once_with_the_note():
    server = Server(completion('{"sql": "SELECT 1"}'), completion())
    gateway, clock = gateway_for(server)
    result = ask(gateway, clock)
    assert result.parsed.sql == "SELECT 1" and result.attempts == 2
    assert REASK_NOTE not in server.bodies[0]["messages"][0]["content"] and REASK_NOTE in server.bodies[1]["messages"][0]["content"]


def test_a_second_bad_reply_is_llm_bad_output():
    server = Server(completion("not json at all"))
    gateway, clock = gateway_for(server)
    with pytest.raises(AgentError) as caught:
        ask(gateway, clock)
    assert caught.value.kind is ErrorKind.LLM_BAD_OUTPUT and len(server.requests) == 2


def test_a_reply_cut_at_the_token_cap_is_asked_again_with_a_bigger_cap():
    server = Server(completion('{"sql": "SELECT', finish="length"), completion())
    gateway, clock = gateway_for(server)
    ask(gateway, clock)
    assert [body["max_tokens"] for body in server.bodies] == [2048, 4096]


def test_a_json_wrapped_in_a_code_fence_is_read():
    server = Server(completion("```json\n" + GOOD + "\n```"))
    gateway, clock = gateway_for(server)
    assert ask(gateway, clock).parsed.sql == "SELECT 1"


def test_a_refusal_is_a_block_and_is_not_asked_again():
    server = Server(completion("", refusal="no"))
    gateway, clock = gateway_for(server)
    with pytest.raises(AgentError) as caught:
        ask(gateway, clock)
    assert caught.value.kind is ErrorKind.LLM_BLOCKED and len(server.requests) == 1


def test_a_call_over_the_turn_budget_never_reaches_the_server():
    server = Server(completion())
    gateway, clock = gateway_for(server)
    budget = TurnBudget(calls=1, seconds=60, bytes_=0, clock=clock)
    gateway.generate(Step.SQL_WRITE, system="s", contents="c", schema=Answer, budget=budget)
    with pytest.raises(AgentError) as caught:
        gateway.generate(Step.SQL_WRITE, system="s", contents="c", schema=Answer, budget=budget)
    assert caught.value.kind is ErrorKind.BUDGET_EXHAUSTED and len(server.requests) == 1


def test_available_models_come_from_the_servers_list():
    server = Server(httpx.Response(200, json={"data": [{"id": LOCAL_MODEL}]}))
    gateway, _ = gateway_for(server)
    assert gateway.available_models() == {LOCAL_MODEL} and gateway.provider == "openai_compatible"


# the environments ---------------------------------------------------------------------------------------------------------

def test_the_shipped_defaults_are_gemini_everywhere_but_local():
    settings = Settings()
    assert ENVIRONMENTS == ("default", "develop", "staging", "prod", "local")
    assert {name: backend.provider for name, backend in settings.llm.environments.items()} == {
        "default": "gemini", "develop": "gemini", "staging": "gemini", "prod": "gemini", "local": "openai_compatible",
    }
    local = settings.llm.environments["local"]
    assert local.model == "qwen/qwen3-30b-a3b-2507" and local.base_url == "http://localhost:1234/v1" and settings.app.environment == "default"
    assert settings.llm_backend.provider == "gemini"


@pytest.mark.parametrize("environment,provider", [("default", "gemini"), ("develop", "gemini"), ("staging", "gemini"), ("prod", "gemini"), ("local", "openai_compatible")])
def test_each_environment_picks_its_backend(environment, provider):
    assert Settings(app={"environment": environment}).llm_backend.provider == provider


def test_a_listed_environment_changes_only_what_it_lists_and_the_rest_keep_their_defaults():
    settings = Settings(llm=LlmConfig(environments={"local": {"base_url": "http://other:11434/v1"}, "prod": {"provider": "gemini"}}))
    local = settings.llm.environments["local"]
    assert local.base_url == "http://other:11434/v1" and local.model == LOCAL_MODEL and local.provider == "openai_compatible"
    assert settings.llm.environments["staging"].provider == "gemini"


def test_an_unknown_environment_or_a_compatible_backend_without_a_model_is_refused():
    with pytest.raises(ValueError, match="unknown environment"):
        LlmConfig(environments={"qa": {"provider": "gemini"}})
    with pytest.raises(ValueError, match="needs `model`"):
        LlmBackend(provider="openai_compatible")
    with pytest.raises(ValueError):
        Settings(app={"environment": "qa"})


def test_the_environment_comes_from_the_file_and_the_variable_wins(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("app:\n  environment: local\n", encoding="utf-8")
    assert load_settings(tmp_path / "config.yaml", load_env_file=False).llm_backend.provider == "openai_compatible"
    monkeypatch.setenv("OPSFLEET_ENV", "prod")
    assert load_settings(tmp_path / "config.yaml", load_env_file=False).app.environment == "prod"
    (tmp_path / "bare.yaml").write_text("golden: {enabled: false}\n", encoding="utf-8")
    assert load_settings(tmp_path / "bare.yaml", load_env_file=False).app.environment == "prod"  # the variable also fills a file with no app section


def test_a_bad_environment_variable_is_a_startup_problem_that_names_it(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("app: {}\n", encoding="utf-8")
    monkeypatch.setenv("OPSFLEET_ENV", "qa")
    with pytest.raises(ConfigError) as caught:
        load_settings(tmp_path / "config.yaml", load_env_file=False)
    assert "OPSFLEET_ENV" in caught.value.message and "local" in caught.value.fix


def test_the_shipped_local_config_uses_the_local_model_with_budgets_for_its_speed():
    from tests.golden_support import REPO_ROOT

    settings = load_settings(REPO_ROOT / "config.local.yaml", load_env_file=False)
    assert settings.app.environment == "local" and settings.llm_backend.model == "qwen/qwen3-30b-a3b-2507"
    assert settings.llm.request_timeout_s == 300 and settings.budget.simple.seconds >= 300 and settings.budget.simple.calls == 8
    shipped = load_settings(REPO_ROOT / "config.yaml", load_env_file=False)
    assert shipped.app.environment == "default" and shipped.llm_backend.provider == "gemini" and shipped.budget.simple.seconds == 60


# the rest of the program ----------------------------------------------------------------------------------------------------

def test_the_banner_names_the_local_model():
    class Session:
        settings = Settings(app={"environment": "local"})
        user = type("U", (), {"display_name": "Alice", "role": "brand_manager", "scope_mode": "brands", "brands": ("A",)})()
        conversation_id = "conv-1"
        chaos = None

    text = banner(Session())
    assert "LOCAL MODEL: qwen/qwen3-30b-a3b-2507 at http://localhost:1234/v1 (environment local), not Gemini." in text
    Session.settings = Settings()
    assert "LOCAL MODEL" not in banner(Session())


def test_doctor_skips_the_gemini_key_and_checks_the_local_server_with_live(project, monkeypatch):
    from src.llm import gateway as gateway_module

    server = Server(httpx.Response(200, json={"data": [{"id": LOCAL_MODEL}]}), completion('{"ok": true}', prompt_tokens=20, completion_tokens=5))
    monkeypatch.setattr(gateway_module.CompatClient, "from_backend", classmethod(lambda cls, backend, **kw: server.client()))
    (project / "config.yaml").write_text("app:\n  environment: local\n", encoding="utf-8")
    result = runner.invoke(app, ["doctor", "--live", "--config", str(project / "config.yaml")])
    assert "[skip] GEMINI_API_KEY: not needed: environment 'local' uses qwen/qwen3-30b-a3b-2507 at http://localhost:1234/v1" in result.output
    assert f"[ ok ] Local model: all 1 configured model(s) exist at http://localhost:1234/v1: {LOCAL_MODEL}" in result.output
    assert f"[ ok ] Local model call: {LOCAL_MODEL} answered with structured JSON" in result.output
    assert "Gemini models" not in result.output


def test_doctor_says_how_to_start_the_server_when_it_is_not_there(project, monkeypatch):
    from src.llm import gateway as gateway_module

    server = Server(httpx.ConnectError("refused"))
    monkeypatch.setattr(gateway_module.CompatClient, "from_backend", classmethod(lambda cls, backend, **kw: server.client()))
    (project / "config.yaml").write_text("app:\n  environment: local\n", encoding="utf-8")
    result = runner.invoke(app, ["doctor", "--live", "--config", str(project / "config.yaml")])
    assert "[FAIL] Local model: could not list the models" in result.output and "load the model, then start the local server" in result.output


def test_doctor_names_the_models_the_server_does_list_when_ours_is_missing(project, monkeypatch):
    from src.llm import gateway as gateway_module

    server = Server(httpx.Response(200, json={"data": [{"id": "llama"}, {"id": "mistral"}]}))
    monkeypatch.setattr(gateway_module.CompatClient, "from_backend", classmethod(lambda cls, backend, **kw: server.client()))
    (project / "config.yaml").write_text("app:\n  environment: local\n", encoding="utf-8")
    result = runner.invoke(app, ["doctor", "--live", "--config", str(project / "config.yaml")])
    assert f"the server does not list: {LOCAL_MODEL}" in result.output and "llama, mistral" in result.output
    assert "[skip] Local model call: skipped because a model ID is missing" in result.output


def test_nothing_outside_the_llm_package_talks_to_a_chat_completions_endpoint():
    """Like the Gemini SDK, the HTTP route to a model is the gateway's alone: it holds the budget, the retries and the breaker."""
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "src"
    offenders = [
        str(path.relative_to(src)) for path in src.rglob("*.py")
        if (src / "llm") not in path.parents and "chat/completions" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
