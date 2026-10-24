"""`tool_router`, `prepare_action`, `hitl_gate` and `execute_action` (plan 07, F22 to F24, decision 3.6 and 3.14).

A save, a list, a delete or an undo needs no SQL and no answer, so the Input Guard sends it here directly. The graph never
names a tool: the router asks the registry which tool handles the intent, so a new tool needs no graph change.

  tool_router       intent -> tool, through the registry. Refuses a role the tool does not allow. A gated action with no usable
                    selector gets a question back, not a guess. The wording (verb, noun) comes from the tool's manifest.
  prepare_action    NO SIDE EFFECTS. For a gated (destructive or external-send) tool it calls the finder, builds the preview
                    in code (titles and dates, never IDs), the confirmation level, and the hash of the exact ID set. The IDs
                    stay in this function: state holds the hash only.
  hitl_gate         `interrupt()`: the preview is the confirmation prompt, so the user confirms in one turn (NFR17). LangGraph
                    runs this node again from its start when it resumes, so nothing before the `interrupt` call has an effect:
                    that is why the preparation is its own node.
  execute_action    compares the reply the user typed (it arrives as the resume value, from the CLI) with the expected text,
                    in code. Finds the targets again and compares the hash: a change in between is STALE_CONFIRMATION and
                    sends the user back to the gate with the new preview. Checks the user's role again (it is read from
                    `users.yaml` on the resume path too). Then it runs the tool with an idempotency key, and stops waiting
                    for it after the manifest's `timeout_s` (TOOL_TIMEOUT).

Only the user's typed reply confirms. Nothing a model wrote can: the reply exists in state only because `hitl_gate` put the
resume value there, and the resume value comes from the CLI's input line.

Every step of a gated action goes to the audit log through `deps.audit` (plan 08, F26): requested, confirmed, cancelled, stale.
A row has the user hash, a count and the ID-set hash, never a title. The effect's own row is written by the tool, in the
transaction of the effect. An audit write that fails stops the step before anything is changed.
"""
from __future__ import annotations

import contextvars
import threading
from dataclasses import replace
from typing import Any, Mapping

from langgraph.types import interrupt

from src.access.users import UserContext
from src.agent.boundary import node
from src.agent.deps import FINAL_TEXT, Deps
from src.agent.envelope import answer_body
from src.agent.nodes.policy import current_user
from src.agent.state import PendingAction, TurnState, with_error
from src.errors import AgentError, ErrorKind, crash_summary, default_message
from src.observability import event, guard_decision, span, turn_metrics
from src.storage.audit import AuditEntry
from src.tools.base import LastAnswer, Tool, ToolContext, ToolManifest, ToolResult, idempotency_key
from src.tools.confirmation import confirmation_for, id_set_hash, preview_text, reply_confirms

ROLE_REFUSAL = "Your role cannot do that."
READ_TIMEOUT = "That took too long to look up. Nothing was changed. Please try again."  # a read tool changed nothing, so this can say so


# The wording of a gated tool comes from its manifest (`verb`, `noun`), so the graph needs no change for a new tool (NFR24).
def clarify_text(manifest: ToolManifest) -> str:
    noun, verb = manifest.noun, manifest.action_verb
    return (
        f"Which {noun}s do you mean? For example: \"{verb} the {noun}s from this conversation\", "
        f"\"{verb} {noun}s mentioning Client X\", \"{verb} my last {noun}\" or \"{verb} all my {noun}s\"."
    )


def cancelled_text(manifest: ToolManifest) -> str:
    """"Cancelled. Nothing was deleted." The past tense is the manifest's own; a tool that does not name one gets "changed"."""
    return f"Cancelled. Nothing was {manifest.action_past or 'changed'}."


def no_match_text(manifest: ToolManifest) -> str:
    return f"I found no {manifest.noun}s matching that."


def stale_and_gone_text(manifest: ToolManifest) -> str:
    return f"Nothing was changed. The {manifest.noun}s changed after I showed you the preview, and I find no matching {manifest.noun}s now."


def _context(deps: Deps, state: TurnState) -> ToolContext:
    last = state.get("last_answer")
    return ToolContext(
        user=current_user(deps, state), conversation_id=state.get("conversation_id", ""), turn_id=state.get("turn_id", ""),
        trace_id=state.get("trace_id", ""), last_answer=LastAnswer(last["title"], last["text"], answer_body(last["text"])) if last else None,
    )


def _tool(deps: Deps, state: TurnState) -> Tool:
    pending = state.get("pending_action") or {}
    tool = deps.registry.get(pending.get("tool", ""))
    if tool is None:
        raise AgentError(ErrorKind.INTERNAL, detail="the pending action names a tool that is not registered")
    return tool


def _audit(
    deps: Deps, ctx: ToolContext, manifest: ToolManifest, step: str, outcome: str, *,
    target_hash: str | None = None, count: int | None = None, key: str | None = None, detail: str | None = None,
) -> None:
    """One audit row for a step of a gated action: `delete_requested`, `delete_confirmed`, ... The verb comes from the manifest.

    A row that cannot be written is TOOL_FAILED, before anything is changed: no action goes on without a record."""
    entry = AuditEntry(
        f"{manifest.audit_verb}_{step}", ctx.user.user_hash, outcome, target_hash=target_hash, count=count,
        trace_id=ctx.trace_id, turn_id=ctx.turn_id, idempotency_key=key, detail=detail,
    )
    try:
        deps.audit(entry)
    except Exception as exc:  # noqa: BLE001 - whatever stops the record stops the step
        raise AgentError(ErrorKind.TOOL_FAILED, detail=f"audit write failed: {crash_summary(exc)}") from None


def _role_refusal(user: UserContext, manifest: ToolManifest) -> AgentError | None:
    """The refusal when the user's role is not one the tool allows, else None. Checked at the request and again at the
    confirmation, because the role is read from `users.yaml` each time and may have changed in between."""
    if user.role in manifest.allowed_roles:
        return None
    return AgentError(ErrorKind.GUARD_VIOLATION, ROLE_REFUSAL, detail=f"role not allowed for {manifest.name}", rule_id="TOOL_ROLE")


def _run_tool(tool: Tool, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Run the tool and stop waiting for it after its manifest's `timeout_s`.

    The tool runs on a daemon thread, with this thread's context so its spans stay in the turn's trace. A timeout does not stop
    it: Python cannot, and it may still finish. So the error does not claim that nothing was changed, and it tells the user to
    look before trying again: the idempotency key holds the turn ID (Q23), so only a retry of the same request finds the first
    effect, and the user's own retry in a later turn is a new request. A tool must not rely on thread-bound resources (a SQLite
    connection needs `check_same_thread=False`).
    """
    box: dict[str, Any] = {}
    context = contextvars.copy_context()

    def target() -> None:
        try:
            box["result"] = context.run(tool.run, args, ctx)
        except BaseException as exc:  # noqa: BLE001 - handed back to the caller's thread
            box["error"] = exc

    worker = threading.Thread(target=target, name=f"tool-{tool.manifest.name}", daemon=True)
    worker.start()
    worker.join(tool.manifest.timeout_s)
    if worker.is_alive():
        said = READ_TIMEOUT if tool.manifest.risk_class == "read" else None
        raise AgentError(ErrorKind.TOOL_TIMEOUT, said, detail=f"{tool.manifest.name} did not finish within {tool.manifest.timeout_s:g} s")
    if "error" in box:
        raise box["error"]
    return box["result"]


def _call(tool: Tool, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """One call of any tool, a finder too: a span, the manifest's timeout, and every failure as a typed error (TOOL_FAILED,
    TOOL_TIMEOUT). A tool that raises is never an INTERNAL error, and no tool can hold the turn longer than its timeout."""
    manifest = tool.manifest
    with span(f"tool.{manifest.name}", "tool", risk_class=manifest.risk_class, timeout_s=manifest.timeout_s):
        try:
            return _run_tool(tool, args, ctx)
        except AgentError:
            raise
        except Exception as exc:  # noqa: BLE001 - no half-done effect is claimed: the tool's own transaction decides
            raise AgentError(ErrorKind.TOOL_FAILED, detail=crash_summary(exc)) from None


def _find(deps: Deps, tool: Tool, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """What a gated tool would change, from its finder. No finder, nothing found."""
    finder = deps.registry.finder_for(tool)
    return _call(finder, args, ctx) if finder else ToolResult("", 0, ())


def make_tool_router(deps: Deps):
    @node(deps, "tool_router")
    def tool_router(state: TurnState) -> dict:
        user = current_user(deps, state)
        tool = deps.registry.for_intent(state.get("intent"))
        if tool is None:
            raise AgentError(ErrorKind.INTERNAL, detail="no tool handles this intent")
        manifest = tool.manifest
        refusal = _role_refusal(user, manifest)
        if refusal is not None:
            return {"errors": with_error(state, refusal), "answer": ROLE_REFUSAL, "outcome": "refused"}
        selector = state.get("selector")
        if manifest.gated and selector is None:  # the model did not say which items: ask, never guess
            return {"answer": clarify_text(manifest), "outcome": "answered"}
        pending: PendingAction = {
            "tool": manifest.name, "args": {"selector": selector}, "intent": state.get("intent") or "",
            "request_turn_id": state.get("turn_id", ""),
        }
        return {"pending_action": pending}

    return tool_router


def make_prepare_action(deps: Deps):
    threshold = deps.settings.delete.large_batch_threshold

    @node(deps, "prepare_action")
    def prepare_action(state: TurnState) -> dict:
        tool = _tool(deps, state)
        manifest = tool.manifest
        if not manifest.gated:
            return {}  # a read or a write: no confirmation, straight to the tool
        pending: PendingAction = dict(state["pending_action"])  # type: ignore[assignment]
        ctx = _context(deps, state)
        found = _find(deps, tool, pending["args"], ctx)
        if not found.targets:
            # Nothing matched. The finder's message is a tool's text (it may hold stored titles), so it goes through the Output Guard
            # like every tool message: held here, and the graph goes to `output_guard`, not straight to `finish`.
            _audit(deps, ctx, manifest, "requested", "no_match", count=0)
            deps.runtime(state).hold(FINAL_TEXT, found.message or no_match_text(manifest))
            return {"outcome": "answered", "pending_action": None}
        refs = [target.ref for target in found.targets]  # held in this function only
        selector = pending["args"].get("selector") or {}
        level = confirmation_for(len(refs), str(selector.get("kind", "")), threshold, manifest.noun, manifest.action_verb)
        preview = preview_text(found.targets, manifest.noun)
        deps.output_guard.check(preview)  # titles are stored text: the same last look as any answer
        pending.update({
            "id_set_hash": id_set_hash(refs, deps.hash_key), "count": len(refs), "level": level.level, "preview": preview,
            "prompt": level.prompt, "expected_reply": level.expected, "note": None,
        })
        _audit(deps, ctx, manifest, "requested", "ok", target_hash=pending["id_set_hash"], count=len(refs))
        event("hitl.requested", level=level.level, count=len(refs), id_set_hash=pending["id_set_hash"][:12])
        return {"pending_action": pending}

    return prepare_action


def make_hitl_gate(deps: Deps):
    @node(deps, "hitl_gate", kind="hitl")
    def hitl_gate(state: TurnState) -> dict:
        pending = state["pending_action"]
        shown = (pending["note"] + "\n\n" if pending.get("note") else "") + pending["preview"]
        reply = interrupt({"preview": shown, "prompt": pending["prompt"], "level": pending["level"]})
        return {"reply": reply if isinstance(reply, str) else None, "restart_gate": False}

    return hitl_gate


def make_execute_action(deps: Deps):
    threshold = deps.settings.delete.large_batch_threshold

    @node(deps, "execute_action")
    def execute_action(state: TurnState) -> dict:
        tool = _tool(deps, state)
        manifest = tool.manifest
        pending: PendingAction = dict(state["pending_action"])  # type: ignore[assignment]
        runtime = deps.runtime(state)
        ctx = _context(deps, state)
        turn_metrics().set_intent(pending.get("intent"), "tool")
        args: dict[str, Any] = dict(pending["args"])
        done: dict[str, Any] = {"pending_action": None, "reply": None, "restart_gate": False}

        refusal = _role_refusal(ctx.user, manifest)  # the role may have changed since the request (users.yaml is read again)
        if refusal is not None:
            guard_decision("hitl", "block", rule="TOOL_ROLE")
            runtime.hold(FINAL_TEXT, ROLE_REFUSAL)
            return {**done, "errors": with_error(state, refusal), "outcome": "refused"}

        if manifest.gated:
            if not reply_confirms(pending["level"], pending["expected_reply"], state.get("reply")):
                guard_decision("hitl", "block", rule="NOT_CONFIRMED")
                _audit(deps, ctx, manifest, "cancelled", "cancelled", target_hash=pending["id_set_hash"], count=pending["count"])
                event("hitl.denied", level=pending["level"], count=pending["count"])
                runtime.hold(FINAL_TEXT, cancelled_text(manifest))
                return {**done, "outcome": "action_cancelled"}
            found = _find(deps, tool, args, ctx)
            refs = [target.ref for target in found.targets]
            if id_set_hash(refs, deps.hash_key) != pending["id_set_hash"]:  # the set changed after the preview (F24)
                guard_decision("hitl", "block", rule=ErrorKind.STALE_CONFIRMATION.value)
                _audit(deps, ctx, manifest, "stale", "stale", target_hash=pending["id_set_hash"], count=pending["count"],
                       detail=f"{len(refs)} found now")
                if not found.targets:
                    runtime.hold(FINAL_TEXT, stale_and_gone_text(manifest))
                    return {**done, "outcome": "action_cancelled"}
                selector = args.get("selector") or {}
                level = confirmation_for(len(refs), str(selector.get("kind", "")), threshold, manifest.noun, manifest.action_verb)
                fresh = {
                    **pending, "id_set_hash": id_set_hash(refs, deps.hash_key), "count": len(refs), "level": level.level,
                    "preview": preview_text(found.targets, manifest.noun), "prompt": level.prompt,
                    "expected_reply": level.expected, "note": default_message(ErrorKind.STALE_CONFIRMATION),
                }
                deps.output_guard.check(fresh["preview"])
                # The new preview is a request in its own right: the rows that follow (confirmed, executed) carry its hash.
                _audit(deps, ctx, manifest, "requested", "ok", target_hash=fresh["id_set_hash"], count=len(refs),
                       detail="new preview after a stale confirmation")
                return {"pending_action": fresh, "reply": None, "restart_gate": True}
            guard_decision("hitl", "pass", rule="CONFIRMED")
            event("hitl.approved", level=pending["level"], count=pending["count"], id_set_hash=pending["id_set_hash"][:12])
            args["ids"] = refs  # the IDs the node just found, passed to the tool and nowhere else
            # The same key for the same request, whenever and however often the confirmation arrives (F27).
            key: str | None = idempotency_key(state.get("conversation_id", ""), pending["request_turn_id"], manifest.name, pending["id_set_hash"])
            _audit(deps, ctx, manifest, "confirmed", "ok", target_hash=pending["id_set_hash"], count=pending["count"], key=key)
        else:
            key = idempotency_key(state.get("conversation_id", ""), state.get("turn_id", ""), manifest.name) if manifest.risk_class == "write" else None

        result = _call(tool, args, replace(ctx, idempotency_key=key))
        runtime.hold(FINAL_TEXT, result.message)
        return {**done, "outcome": "action_done"}

    return execute_action
