"""The agent behind the CLI (plan 07): build it once per chat, run one turn per typed line.

`build_agent` assembles the pieces (gateway, runner, the three guards, the registry) into `Deps` and compiles the graph.
`AgentRuntime.run` is what `run_turn` in the CLI calls. It:

- takes the one-run lock of the conversation (A19; production would use a database lock);
- makes the turn's `TurnBudget` with the simple limits (`upgrade()` raises them once the Input Guard has classified the turn);
- starts a new turn from `fresh_turn_input`, or, when the conversation is waiting at the HITL Gate, resumes it with the
  line the user typed (`Command(resume=...)`). The resume value is the only way a confirmation can arrive;
- turns the final state into a `TurnResult`: the answer to print, or the error behind a refusal or a failure, or the
  confirmation preview when the graph paused.

It never prints. The CLI prints the answer once, after the progress is gone (S9).
"""
from __future__ import annotations

import hashlib
import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable

from langgraph.types import Command

from src.access.users import UserContext
from src.agent.deps import Deps, TurnRuntime
from src.agent.graph import build_graph
from src.agent.state import fresh_turn_input
from src.cli.render import ProgressFn
from src.config import Settings
from src.errors import AgentError, ConfigError, ErrorKind
from src.llm.budget import TurnBudget

log = logging.getLogger(__name__)

BUSY_MESSAGE = "I'm still working on your previous question. Please wait for it to finish."


@dataclass(frozen=True)
class TurnResult:
    text: str  # the answer; the confirmation preview and prompt when `awaiting_confirmation`; else the friendly message
    outcome: str | None  # answered, partial, refused, action_done, action_cancelled, failed; None while paused
    error: AgentError | None = None  # set for a refusal or a failure: the CLI shows it in its own panel, with the trace ID
    awaiting_confirmation: bool = False


class AgentRuntime:
    def __init__(self, deps: Deps, checkpointer: Any = None) -> None:
        self.deps = deps
        self.graph = build_graph(deps, checkpointer)
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _lock_for(self, conversation_id: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(conversation_id, threading.Lock())

    def awaiting_confirmation(self, conversation_id: str) -> bool:
        """True when the conversation is paused at the HITL Gate and the next line is the user's reply."""
        snapshot = self.graph.get_state({"configurable": {"thread_id": conversation_id}})
        return "hitl_gate" in snapshot.next

    def run(
        self, *, text: str, conversation_id: str, turn_id: str, trace_id: str, user_hash: str,
        progress: ProgressFn = lambda text: None,
    ) -> TurnResult:
        lock = self._lock_for(conversation_id)
        if not lock.acquire(blocking=False):
            raise AgentError(ErrorKind.INTERNAL, BUSY_MESSAGE, detail="a run is already active for this conversation", retryable=True)
        try:
            runtime = TurnRuntime(TurnBudget.from_settings(self.deps.settings.budget, self.deps.clock), progress)
            self.deps.begin(conversation_id, runtime)
            if self.deps.chaos is not None:
                self.deps.chaos.begin_turn()  # a run of faults never carries over from the last turn
            config = {"configurable": {"thread_id": conversation_id}}  # the thread is the conversation; nothing else goes here
            if self.awaiting_confirmation(conversation_id):
                # The line the user typed is the reply. It is the resume value, never part of a new question.
                payload: Any = Command(resume=text, update={"turn_id": turn_id, "trace_id": trace_id})
            else:
                payload = fresh_turn_input(
                    conversation_id=conversation_id, turn_id=turn_id, trace_id=trace_id, user_hash=user_hash, question=text,
                )
            # `invoke` returns the final state, and a pause as `__interrupt__`: the checkpoint is not loaded again for the result.
            return self._result(self.graph.invoke(payload, config), trace_id)
        finally:
            self.deps.end(conversation_id)
            lock.release()

    def _result(self, values: dict[str, Any], trace_id: str) -> TurnResult:
        for interrupt in values.get("__interrupt__") or ():  # paused: show the preview, and the next line is the reply
            value = interrupt.value
            return TurnResult(f"{value['preview']}\n\n{value['prompt']}", None, awaiting_confirmation=True)
        outcome = values.get("outcome")
        answer = values.get("answer") or ""
        if outcome in ("refused", "failed"):
            record = (values.get("errors") or [{}])[-1]
            try:
                kind = ErrorKind(record.get("kind"))
            except ValueError:
                kind = ErrorKind.INTERNAL
            error = AgentError(
                kind, record.get("message") or None, detail=record.get("detail", ""), rule_id=record.get("rule_id"), trace_id=trace_id,
            )
            return TurnResult(answer, outcome, error)
        return TurnResult(answer, outcome)


def build_agent(
    settings: Settings,
    *,
    user: UserContext,
    catalog: Any,
    allowlist: Any,
    personas: Any = None,
    persona_name: str | None = None,
    chaos: Any = None,
    gateway: Any = None,
    runner: Any = None,
    registry: Any = None,
    load_user: Callable[[], UserContext] | None = None,
    checkpointer: Any = None,
    clock: Callable[[], float] | None = None,
) -> AgentRuntime:
    """The agent for one chat session. Everything but `settings`, `user`, `catalog` and `allowlist` has a real default.

    The pseudonymizer is made here, so its fake customer IDs are fixed for this session and the object never leaves the Result
    Guard. `gateway` and `runner` are the test seams: the scripted `FakeGateway` and a `SafeQueryRunner` on the fake client.
    """
    # Imported here: the Google SDKs, BigQuery and pandas load only when a chat starts (`trace`, `metrics` and `doctor` stay light).
    from src.access.users import load_users, resolve_salt
    from src.data_access.safe_runner import SafeQueryRunner
    from src.guards.output_guard import OutputGuard
    from src.guards.pseudonymizer import SessionPseudonymizer
    from src.guards.result_guard import ResultGuard
    from src.guards.sql_guard import SQLGuard
    from src.llm.gateway import LLMGateway
    from src.tools.registry import ToolRegistry
    from src.tools.stub_reports import StubSavedReports

    if registry is None:
        registry = ToolRegistry(StubSavedReports(undo_days=settings.delete.undo_days).tools())

    def persona_text() -> str:
        if personas is None or not persona_name:
            return ""
        return personas.get(persona_name).prompt_block()  # a broken file raises; `context_assembly` carries on without it

    def reload_user() -> UserContext:
        try:
            directory = load_users(settings)
        except ConfigError:
            # A file that does not parse (or has a bad entry for anyone) is not a list we can trust: every user is blocked, and the
            # message says the list cannot be confirmed, not that this user has no brands.
            raise AgentError(ErrorKind.ACCESS_STORE_DOWN, detail="users.yaml is not valid") from None
        return directory.get(user.user_id)  # a user who left the file raises ConfigError: `policy_context` reads that as no access

    deps = Deps(
        settings=settings,
        gateway=gateway or LLMGateway(settings, chaos=chaos),
        runner=runner or SafeQueryRunner(settings, chaos=chaos),
        sql_guard=SQLGuard(settings, allowlist),
        result_guard=ResultGuard(settings, allowlist, SessionPseudonymizer()),
        output_guard=OutputGuard(),
        catalog=catalog,
        allowlist=allowlist,
        registry=registry,
        load_user=load_user or reload_user,
        hash_key=hashlib.sha256(("id-set|" + resolve_salt(settings)).encode("utf-8")).digest(),
        persona_text=persona_text,
        chaos=chaos,
        **({"clock": clock} if clock is not None else {}),
    )
    return AgentRuntime(deps, checkpointer)
