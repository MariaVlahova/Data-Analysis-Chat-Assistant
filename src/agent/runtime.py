"""The agent behind the CLI (plan 07): build it once per chat, run one turn per typed line.

`build_agent` assembles the pieces (gateway, runner, the three guards, the registry) into `Deps` and compiles the graph.
`AgentRuntime.run` is what `run_turn` in the CLI calls. It:

- cleans and masks what the user typed (`typed_text`) before it can reach state, a checkpoint on disk, a prompt or the history, as
  the traces already do: a checkpoint is kept for 90 days in the app database (plan 08);
- keeps only the newest checkpoint of the conversation once the turn has ended (`prune_thread`), so a conversation that is used
  every day does not grow without end; the pause at the gate is in the newest one;
- takes the one-run lock of the conversation (A19; production would use a database lock);
- makes the turn's `TurnBudget` with the simple limits (`upgrade()` raises them once the Input Guard has classified the turn);
- starts a new turn from `fresh_turn_input`, or, when the conversation is waiting at the HITL Gate, resumes it with the
  line the user typed (`Command(resume=...)`). The resume value is the only way a confirmation can arrive;
- turns the final state into a `TurnResult`: the answer to print, or the error behind a refusal or a failure, or the
  confirmation preview when the graph paused.

It also answers two questions for the CLI about a conversation that stopped at the HITL Gate, possibly in an earlier
process (plan 08, F28): `pending_confirmation` gives the preview to show again, and `abandon_pending` ends the wait without
doing the action.

It never prints. The CLI prints the answer once, after the progress is gone (S9).
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from src.access.users import UserContext
from src.agent.checkpointer import prune_thread
from src.agent.deps import Deps, TurnRuntime
from src.agent.graph import build_graph
from src.agent.nodes.input_guard import typed_text
from src.agent.state import fresh_turn_input
from src.cli.render import ProgressFn
from src.config import Settings
from src.errors import AgentError, ConfigError, ErrorKind, crash_summary
from src.llm.budget import TurnBudget
from src.storage.audit import AuditEntry

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
        self._saver = checkpointer if isinstance(checkpointer, SqliteSaver) else None  # the one that is kept on disk and pruned
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _lock_for(self, conversation_id: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(conversation_id, threading.Lock())

    def state_of(self, conversation_id: str) -> dict[str, Any]:
        """A copy of the newest checkpoint of the conversation: the graph state as the next turn will find it. For the evaluation, which asks
        whether what carries over between turns is what should (`eval/trajectory.py`). The result rows of each sub-question are left out: what
        reads this never needs them, and they stay in the node that ran the query. Empty when there is no checkpoint."""
        values = dict(self.graph.get_state({"configurable": {"thread_id": conversation_id}}).values or {})
        results = values.get("results") or {}
        values["results"] = {sub_id: {key: value for key, value in result.items() if key != "guarded"} for sub_id, result in results.items()}
        return values

    def awaiting_confirmation(self, conversation_id: str) -> bool:
        """True when the conversation is paused at the HITL Gate, with the prompt recorded, and the next line is the user's reply."""
        return _gate_interrupt(self.graph.get_state({"configurable": {"thread_id": conversation_id}})) is not None

    def pending_confirmation(self, conversation_id: str) -> str | None:
        """The preview and prompt of the confirmation this conversation is waiting for, or None. Read from the checkpoint, so it
        works in a new process after a restart: the CLI shows it again and the next typed line is the reply. It agrees with
        `awaiting_confirmation`: a line is only ever taken for a reply when the prompt it answers exists to be shown."""
        value = _gate_interrupt(self.graph.get_state({"configurable": {"thread_id": conversation_id}}))
        return _confirmation_text(value) if value is not None else None

    def abandon_pending(self, conversation_id: str) -> bool:
        """End the wait at the HITL Gate without doing the action (`/new`). Audited as cancelled, outcome `abandoned`. False when
        the conversation was not waiting. The conversation's thread then ends, so the old request cannot be confirmed later."""
        with self._lock_for(conversation_id):
            config = {"configurable": {"thread_id": conversation_id}}
            snapshot = self.graph.get_state(config)
            if _gate_interrupt(snapshot) is None:
                return False
            self._end_thread(config, snapshot)
            return True

    def _end_thread(self, config: dict[str, Any], snapshot: Any) -> None:
        """Close a thread that has unfinished steps, so nothing of it runs again, and note an abandoned request in the audit log.

        The thread is closed first and the row written after: if closing fails, no row says "abandoned" about a request that is
        still waiting. A row that cannot be written is logged and does not undo the close (nothing was changed by it)."""
        values = snapshot.values
        pending = values.get("pending_action") or {}
        at_gate = "hitl_gate" in snapshot.next
        # `finish` leads to the end of the graph, so the thread has nothing left to run. The next question starts a fresh turn.
        self.graph.update_state(config, {"pending_action": None, "reply": None, "restart_gate": False}, as_node="finish")
        # A row says "abandoned" only about a request that was shown (or about to be shown) and never confirmed: a gated tool, with its
        # preview built, stopped at the gate. A run cut off anywhere else may have made its effect already, and the effect has its own
        # row; a tool that is not gated was never a request for confirmation.
        tool = self.deps.registry.get(pending.get("tool", ""))
        if not (at_gate and tool is not None and tool.manifest.gated and pending.get("id_set_hash")):
            return
        verb = tool.manifest.audit_verb
        try:
            self.deps.audit(AuditEntry(
                f"{verb}_cancelled", values.get("user_hash", ""), "abandoned", target_hash=pending.get("id_set_hash"),
                count=pending.get("count"), trace_id=values.get("trace_id"), turn_id=values.get("turn_id"),
            ))
        except Exception as exc:  # noqa: BLE001 - see above
            log.error("could not record the abandoned %s_requested in the audit log: %s", verb, crash_summary(exc))

    def _prune(self, conversation_id: str) -> None:
        """Keep only the newest checkpoint of the conversation. Housekeeping: a failure is logged, never shown."""
        if self._saver is None:
            return
        try:
            prune_thread(self._saver, conversation_id)
        except Exception as exc:  # noqa: BLE001 - the next turn tries again
            log.error("could not prune the checkpoints of a conversation: %s", crash_summary(exc))

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
            text = typed_text(text)  # what was typed is stored in the checkpoint, so it is cleaned and masked first, as in a trace
            snapshot = self.graph.get_state(config)  # one look at the checkpoint per typed line
            if _gate_interrupt(snapshot) is not None:
                # The line the user typed is the reply. It is the resume value, never part of a new question.
                payload: Any = Command(resume=text, update={"turn_id": turn_id, "trace_id": trace_id})
            else:
                if snapshot.next:
                    # A run that was cut off (the process was killed, Ctrl+C) left steps behind that never ran, and the gate may be one
                    # of them without its prompt ever shown. Close that run: its steps would otherwise run again with this turn's input.
                    self._end_thread(config, snapshot)
                payload = fresh_turn_input(
                    conversation_id=conversation_id, turn_id=turn_id, trace_id=trace_id, user_hash=user_hash, question=text,
                )
            # `invoke` returns the final state, and a pause as `__interrupt__`: the checkpoint is not loaded again for the result.
            result = self._result(self.graph.invoke(payload, config), trace_id)
            self._prune(conversation_id)
            return result
        finally:
            self.deps.end(conversation_id)
            lock.release()

    def _result(self, values: dict[str, Any], trace_id: str) -> TurnResult:
        for interrupt in values.get("__interrupt__") or ():  # paused: show the preview, and the next line is the reply
            return TurnResult(_confirmation_text(interrupt.value), None, awaiting_confirmation=True)
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


def _gate_interrupt(snapshot: Any) -> dict[str, Any] | None:
    """What the HITL Gate asked, when the conversation is paused there with the question recorded. A checkpoint can also stop just
    before the gate has run (the process died between two steps): `hitl_gate` is next, but nothing was asked, so no line is a reply."""
    if "hitl_gate" not in snapshot.next:
        return None
    for task in snapshot.tasks:
        for interrupt in task.interrupts:
            return interrupt.value
    return None


def _confirmation_text(value: dict[str, Any]) -> str:
    """What the HITL Gate shows: the preview of what would change, then the prompt that says what to type."""
    return f"{value['preview']}\n\n{value['prompt']}"


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
    audit: Callable[[AuditEntry], None] | None = None,
    golden: Any = None,
    gateway_wrapper: Callable[[Any], Any] | None = None,
) -> AgentRuntime:
    """The agent for one chat session. Everything but `settings`, `user`, `catalog` and `allowlist` has a real default.

    The pseudonymizer is made here, so its fake customer IDs are fixed for this session and the object never leaves the Result
    Guard. `gateway` and `runner` are the test seams: the scripted `FakeGateway` and a `SafeQueryRunner` on the fake client.

    The default tools are the real Saved Reports (plan 08) on `data/db.sqlite3`, which `Database.migrate()` must have prepared, and
    the gate's events go to its audit log. A test that passes its own `registry` gets no audit unless it passes `audit` too. The
    default checkpointer is in memory; the chat passes a `SqliteSaver` (`checkpointer.open_checkpointer`) so a pause survives.

    `golden` is plan 10's `GoldenBundle` (similar solved questions and the glossary), or None for a run without it. It is used only
    when `golden.enabled` is true in the settings, so one flag switches the Golden Bucket off.

    `gateway_wrapper` receives the gateway and returns what the graph is given instead: the scenario runner's recorder, which passes every call
    through and keeps the prompts (the traces hold hashes, not the text).
    """
    # Imported here: the Google SDKs, BigQuery and pandas load only when a chat starts (`trace`, `metrics` and `doctor` stay light).
    from src.access.users import load_users, resolve_salt
    from src.data_access.safe_runner import SafeQueryRunner
    from src.agent.definition_check import build_definition_check
    from src.golden.glossary import load_glossary
    from src.guards.domain_words import build_domain_words
    from src.guards.output_guard import OutputGuard
    from src.guards.pseudonymizer import SessionPseudonymizer
    from src.guards.result_guard import ResultGuard
    from src.guards.sql_guard import SQLGuard
    from src.llm.gateway import LLMGateway
    from src.reports.store import ReportStore
    from src.tools.confirmation import id_hash_key, id_set_hash
    from src.tools.registry import ToolRegistry
    from src.tools.saved_reports import SavedReportsTools

    # The key of the ID-set hash comes from the installation's salt, so a confirmation still matches after a restart.
    hash_key = id_hash_key(resolve_salt(settings))
    output_guard = OutputGuard()
    if registry is None:
        store = ReportStore.from_settings(settings, hash_ids=lambda ids: id_set_hash(ids, hash_key))
        registry = ToolRegistry(SavedReportsTools(store, scan=output_guard.scan).tools())
        audit = audit or store.audit.append

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

    # The glossary of the Golden Bucket when it is open, else the file: it is a word list either way (plan 12, A1 and D2).
    glossary = getattr(golden, "glossary", None) or load_glossary(settings.resolve(settings.golden.glossary_file))[0]  # a bad file: no entries
    domain_words = None
    if settings.guards.require_domain_words:
        domain_words = build_domain_words(allowlist, [word for entry in glossary.entries for word in entry.words()])
    revenue_words = [word for entry in glossary.entries if entry.term.casefold() == "revenue" for word in entry.words()]  # the term and its aliases

    deps = Deps(
        settings=settings,
        gateway=(gateway_wrapper or (lambda inner: inner))(gateway or LLMGateway(settings, chaos=chaos)),
        runner=runner or SafeQueryRunner(settings, chaos=chaos),
        sql_guard=SQLGuard(settings, allowlist),
        result_guard=ResultGuard(settings, allowlist, SessionPseudonymizer()),
        output_guard=output_guard,
        catalog=catalog,
        allowlist=allowlist,
        registry=registry,
        load_user=load_user or reload_user,
        hash_key=hash_key,
        persona_text=persona_text,
        golden=golden.examples if golden is not None else None,
        glossary=golden.definitions if golden is not None else None,
        chaos=chaos,
        domain_words=domain_words,
        definition_check=build_definition_check(revenue_words),
        **({"clock": clock} if clock is not None else {}),
        **({"audit": audit} if audit is not None else {}),
    )
    return AgentRuntime(deps, checkpointer)
