"""The tool registry (plan 07, decision 3.14): register and look up tools. The graph never names a tool.

`for_intent` is how the Tool Router finds a tool, so adding a tool is one `register` call with a manifest that lists the
intents it handles. The Input Guard reads `intents()` to know what it may answer with.
"""
from __future__ import annotations

from typing import Iterable

from src.errors import ConfigError
from src.tools.base import Tool


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        self._by_intent: dict[str, str] = {}
        for tool in tools or ():
            self.register(tool)

    def register(self, tool: Tool) -> None:
        manifest = tool.manifest
        if manifest.name in self._tools:
            raise ConfigError(f"The tool {manifest.name} is registered twice.")
        for intent in manifest.intents:
            if intent in self._by_intent:
                raise ConfigError(f"The intent {intent} is claimed by {self._by_intent[intent]} and {manifest.name}.")
        self._tools[manifest.name] = tool
        for intent in manifest.intents:
            self._by_intent[intent] = manifest.name

    def validate(self, reserved_intents: Iterable[str] = ()) -> None:
        """Every gated tool names a finder that is registered and is a read tool: without one there is nothing to preview. No tool
        claims an intent in `reserved_intents` (the Input Guard's own, such as `data_question`): the schema would merge the two, and
        the graph would send every such message to the tool."""
        reserved = set(reserved_intents)
        for tool in self._tools.values():
            manifest = tool.manifest
            for intent in manifest.intents:
                if intent in reserved:
                    raise ConfigError(f"The tool {manifest.name} claims the intent {intent}, which the Input Guard already uses.")
            if manifest.gated:
                finder = self._tools.get(manifest.finder or "")
                if finder is None or finder.manifest.risk_class != "read":
                    raise ConfigError(f"The tool {manifest.name} needs a registered read tool as its finder.")

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def for_intent(self, intent: str | None) -> Tool | None:
        name = self._by_intent.get(intent or "")
        return self._tools[name] if name else None

    def finder_for(self, tool: Tool) -> Tool | None:
        return self._tools.get(tool.manifest.finder or "")

    def intents(self) -> tuple[str, ...]:
        return tuple(self._by_intent)

    def selector_intents(self) -> list[tuple[str, str]]:
        """(intent, noun) of each tool that acts on items it finds first (a gated tool): the intents that take a `selector`."""
        return [
            (intent, self._tools[name].manifest.noun) for intent, name in self._by_intent.items() if self._tools[name].manifest.gated
        ]

    def describe_intents(self) -> list[tuple[str, str]]:
        """(intent, what the tool does), for the Input Guard prompt."""
        return [(intent, self._tools[name].manifest.description) for intent, name in self._by_intent.items()]
