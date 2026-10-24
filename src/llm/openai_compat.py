"""A client for any server that speaks the OpenAI chat-completions protocol: LM Studio, Ollama, vLLM (the `local` environment).

It sits behind `LLMGateway` exactly where the Gemini SDK sits, so routing, the turn budget, the breaker, retries, token accounting,
chaos and the spans are the same for both. It does one thing: send one request and return the reply in the gateway's terms
(`CompatReply`: text, tokens, finish reason).

Structured output uses `response_format: json_schema` with the same pydantic schema the Gemini call gets, which LM Studio turns into a
grammar, so the reply is valid JSON for the schema. The gateway still parses and checks it, and re-asks once, as it does for Gemini.

Like the gateway, this module never keeps a server's error text: a server error can quote the request. A failure carries the HTTP code and
a fixed word for it.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel

_THINK = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)
_STATUS_WORDS = {
    400: "BAD_REQUEST", 401: "UNAUTHENTICATED", 403: "PERMISSION_DENIED", 404: "NOT_FOUND", 408: "REQUEST_TIMEOUT", 422: "UNPROCESSABLE",
    429: "RESOURCE_EXHAUSTED", 500: "INTERNAL", 502: "BAD_GATEWAY", 503: "UNAVAILABLE", 504: "GATEWAY_TIMEOUT",
}
# OpenAI's `finish_reason` in the words the gateway already knows (it was written for Gemini's).
_FINISH = {"stop": "STOP", "length": "MAX_TOKENS", "content_filter": "SAFETY", "tool_calls": "STOP", "function_call": "STOP"}


class CompatHTTPError(Exception):
    """The server answered with an error status, or with something that is not a chat completion. `status` is a fixed word."""

    def __init__(self, code: int) -> None:
        self.code = code
        self.status = _STATUS_WORDS.get(code, "")
        super().__init__(f"HTTP {code}")  # the server's text is not kept


@dataclass(frozen=True)
class CompatReply:
    text: str
    tokens_in: int = 0
    tokens_out: int = 0
    finish: str | None = None
    blocked: str | None = None


def _as_text(contents: Any) -> str:
    """The prompt as one user message. The agent always sends text; a list of parts (the Gemini SDK allows one) is joined."""
    if isinstance(contents, str):
        return contents
    if isinstance(contents, (list, tuple)):
        return "\n\n".join(item if isinstance(item, str) else str(getattr(item, "text", item)) for item in contents)
    return str(contents)


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


class CompatClient:
    def __init__(self, base_url: str, api_key: str | None = None, *, transport: httpx.BaseTransport | None = None) -> None:
        """`transport` is the test seam: an `httpx.MockTransport` stands in for the server."""
        self._base = base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = httpx.Client(headers=headers, transport=transport)

    @classmethod
    def from_backend(cls, backend: Any, *, transport: httpx.BaseTransport | None = None) -> "CompatClient":
        key = os.environ.get(backend.api_key_env, "").strip() if backend.api_key_env else ""
        return cls(backend.base_url, key or None, transport=transport)

    def close(self) -> None:
        self._http.close()

    # the calls -------------------------------------------------------------------------------------

    def list_models(self, timeout: float = 10.0) -> set[str]:
        """The model IDs the server offers (`GET /models`), for `doctor --live`."""
        response = self._http.get(f"{self._base}/models", timeout=timeout)
        if response.status_code != 200:
            raise CompatHTTPError(response.status_code)
        try:
            return {str(item["id"]) for item in response.json()["data"]}
        except (ValueError, KeyError, TypeError):
            raise CompatHTTPError(502) from None

    def chat(
        self, *, model: str, system: str, contents: Any, schema: type[BaseModel] | None, temperature: float, max_tokens: int, timeout: float,
    ) -> CompatReply:
        body: dict[str, Any] = {
            "model": model, "temperature": temperature, "max_tokens": max_tokens, "stream": False,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": _as_text(contents)}],
        }
        if schema is not None:
            body["response_format"] = {
                "type": "json_schema", "json_schema": {"name": schema.__name__, "strict": True, "schema": schema.model_json_schema()},
            }
        response = self._http.post(f"{self._base}/chat/completions", json=body, timeout=timeout)
        if response.status_code != 200:
            raise CompatHTTPError(response.status_code)
        return _read(response)


def _read(response: httpx.Response) -> CompatReply:
    try:
        payload = response.json()
        choice = payload["choices"][0]
        message = choice.get("message") or {}
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise CompatHTTPError(502) from None  # a 200 that is not a chat completion is the server's fault, and may pass
    content = message.get("content")
    text = _THINK.sub("", content).strip() if isinstance(content, str) else ""
    usage = payload.get("usage") if isinstance(payload, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    raw_finish = choice.get("finish_reason")
    finish = _FINISH.get(raw_finish, str(raw_finish).upper()) if raw_finish else None
    blocked = "the reply was blocked (content filter)" if raw_finish == "content_filter" else (
        "the model declined the request" if message.get("refusal") else None
    )
    return CompatReply(text, _count(usage.get("prompt_tokens")), _count(usage.get("completion_tokens")), finish, blocked)
