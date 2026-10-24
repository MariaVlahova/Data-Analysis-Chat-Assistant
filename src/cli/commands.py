"""Chat session state, slash commands and the top-level error boundary (NFR19, F36).

`run_turn()` hands the line to the agent graph (plan 07, `src/agent/`). The graph has its own error boundary, so a failing
step ends in an honest message; this module's boundary is the catch-all behind it.
Every question runs inside `Tracer.start_turn` (plan 02): the trace file and the metrics row are written
even when the turn crashes.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from src.access.users import UserContext
from src.cli.render import ProgressFn, Renderer
from src.config import Settings
from src.errors import REFUSAL_KINDS, AgentError, ConfigError, ErrorKind, friendly, new_trace_id
from src.observability import Tracer, TurnTrace, alerts, viewer
from src.personas import PersonaStore
from src.storage.db import Database, turn_id

if TYPE_CHECKING:  # annotations only: importing them would load BigQuery, pandas and the Google SDK for every CLI command
    from src.agent.runtime import AgentRuntime
    from src.data_access.catalog import SchemaCatalog
    from src.llm.chaos import Chaos

log = logging.getLogger(__name__)

UNNUMBERED_TURN = "t-?"  # the turn ID in a trace whose turn number could not be allocated


@dataclass
class ChatSession:
    settings: Settings
    user: UserContext
    db: Database
    renderer: Renderer
    conversation_id: str
    debug: bool = False
    chaos: str | None = None  # the --chaos level; the `chaos: true` flag of the metrics row
    chaos_seed: int | None = None  # the seed actually used, drawn when --chaos-seed was not given
    chaos_engine: Chaos | None = None  # plan 04: handed to the gateway, the SQL writer node and SafeQueryRunner (plan 07)
    turn_count: int = 0  # turns so far in this conversation, stored in the database
    personas: PersonaStore | None = None  # None when persona.enabled is false
    persona_name: str | None = None
    tracer: Tracer | None = None  # built from the settings when not given
    last_trace_id: str | None = None  # the newest turn of this chat, for a bare /trace
    catalog: SchemaCatalog | None = None  # plan 03: brands, latest date and live schema, read at startup
    agent: AgentRuntime | None = None  # plan 07: the graph, its guards and tools, built once per chat
    checkpointer: Any = None  # plan 08: the SqliteSaver behind the agent, closed with the session

    def __post_init__(self) -> None:
        if self.tracer is None:
            self.tracer = Tracer.from_settings(self.settings)

    @property
    def turn_id(self) -> str:
        return turn_id(self.turn_count)

    def close(self) -> None:
        """Close what the session opened: the checkpoint connection, then the database."""
        from src.agent.checkpointer import close_checkpointer

        try:
            close_checkpointer(self.checkpointer)
        finally:
            self.db.close()  # the database closes even if the checkpoint connection would not


TurnFn = Callable[..., str]  # (session, text, *, trace_id, progress) -> answer markdown


def run_turn(session: ChatSession, text: str, *, trace_id: str, progress: ProgressFn) -> str:
    """One typed line, through the agent graph. Returns the answer to print.

    A refusal or a failure is raised as its `AgentError`, so the boundary in `handle_line` shows it in its own panel with the
    trace ID, the same way as any other failure. When the graph is waiting for a confirmation, this line is the reply.
    """
    if session.agent is None:
        raise AgentError(ErrorKind.INTERNAL, detail="the chat has no agent")
    result = session.agent.run(
        text=text, conversation_id=session.conversation_id, turn_id=session.turn_id, trace_id=trace_id,
        user_hash=session.user.user_hash, progress=progress,
    )
    if result.error is not None:
        raise result.error
    return result.text


# Slash commands ---------------------------------------------------------------------------------

SlashHandler = Callable[[ChatSession, str], "bool | None"]  # return False to leave the chat


@dataclass(frozen=True)
class SlashCommand:
    name: str
    usage: str
    summary: str
    handler: SlashHandler


SLASH_COMMANDS: dict[str, SlashCommand] = {}


def register_slash(name: str, usage: str, summary: str) -> Callable[[SlashHandler], SlashHandler]:
    """Register (or replace) a slash command. Later plans call this with the same name to take over."""

    def decorator(handler: SlashHandler) -> SlashHandler:
        SLASH_COMMANDS[name] = SlashCommand(name, usage, summary, handler)
        return handler

    return decorator


@register_slash("help", "/help", "Show this list")
def _help(session: ChatSession, arg: str) -> None:
    width = max(len(c.usage) for c in SLASH_COMMANDS.values())
    session.renderer.info("Commands:\n" + "\n".join(f"  {c.usage.ljust(width)}  {c.summary}" for c in SLASH_COMMANDS.values()))
    session.renderer.info("Anything else is sent to the assistant as a question.")


@register_slash("quit", "/quit", "Leave the chat")
def _quit(session: ChatSession, arg: str) -> bool:
    return False


@register_slash("new", "/new", "Start a new conversation")
def _new(session: ChatSession, arg: str) -> None:
    # A confirmation still waiting in this conversation is dropped, not carried out (plan 08): nothing is changed.
    if session.agent is not None and session.agent.abandon_pending(session.conversation_id):
        session.renderer.info("The action waiting for your confirmation was cancelled. Nothing was changed.")
    session.conversation_id = session.db.create_conversation(session.user.user_id)
    session.turn_count = 0
    session.renderer.info(f"Started a new conversation: {session.conversation_id}")


@register_slash("trace", "/trace [id] [--raw]", "Show the steps of a turn (default: the last one)")
def _trace(session: ChatSession, arg: str) -> None:
    words = arg.split()
    raw = "--raw" in words
    ids = [word for word in words if not word.startswith("--")]
    unknown = [word for word in words if word.startswith("--") and word != "--raw"]
    if unknown or len(ids) > 1:
        session.renderer.info("Usage: /trace [id] [--raw]")
        return
    viewer.show_trace(
        session.renderer.console, session.settings, ids[0] if ids else None,
        raw=raw, user=session.user, default_id=session.last_trace_id,
    )


@register_slash("metrics", "/metrics [--last N]", "Show totals and rates of your turns")
def _metrics(session: ChatSession, arg: str) -> None:
    words = arg.split()
    last: int | None = None
    if words:
        if len(words) != 2 or words[0] != "--last" or not words[1].isdecimal() or int(words[1]) < 1:
            session.renderer.info("Usage: /metrics [--last N]")
            return
        last = int(words[1])
    viewer.show_metrics(session.renderer.console, session.settings, last, user=session.user)


def dispatch_slash(session: ChatSession, text: str) -> bool:
    """Run a slash command. Returns False when the chat should end. Unknown commands print the help."""
    name, _, arg = text[1:].partition(" ")
    command = SLASH_COMMANDS.get(name.strip().lower())
    if command is None:
        SLASH_COMMANDS["help"].handler(session, "")
        return True
    return command.handler(session, arg.strip()) is not False


# One line of input ------------------------------------------------------------------------------

def report_failure(session: ChatSession, exc: BaseException, trace_id: str) -> None:
    """Show a friendly message and the trace ID. The screen never gets a traceback unless `--debug`.

    The log gets the error kind, the trace ID and, for a crash, the exception class and where it
    happened. It never gets the exception text: it can carry data that no pattern catches. The error
    detail lives in the trace (masked), which `/trace <id>` shows.
    """
    err = AgentError.from_exception(exc, trace_id)
    shown_id = err.trace_id or trace_id  # an ID the raising code already stored wins
    if isinstance(exc, AgentError):
        log.warning("turn ended kind=%s trace_id=%s rule_id=%s", err.kind.value, shown_id, err.rule_id)
    else:
        log.error("turn crashed trace_id=%s %s", shown_id, err.detail)
    message = friendly(err, trace_id)
    if err.kind in REFUSAL_KINDS:
        session.renderer.refusal(message)
    else:
        session.renderer.error(message)
    if session.debug and not isinstance(exc, AgentError):
        session.renderer.traceback()


def handle_line(session: ChatSession, line: str, turn_fn: TurnFn = run_turn) -> bool:
    """Handle one line from the user. Returns False when the chat should end.

    This is the top-level boundary: any exception becomes a friendly message and the chat goes on.
    Only KeyboardInterrupt passes through, and the caller quits on it.
    """
    text = line.strip()
    if not text:
        return True
    trace_id = new_trace_id()
    turn: TurnTrace | None = None
    try:
        if text.startswith("/"):
            return dispatch_slash(session, text)
        # The trace file and the metrics row are written when this block ends, also when the turn raises.
        # The turn number comes from the database inside the block, so a database failure is traced too:
        # the trace ID the user is shown always has a stored trace and a row.
        with session.tracer.start_turn(
            session.user.user_hash, session.conversation_id, UNNUMBERED_TURN,
            trace_id=trace_id, question=text, chaos=bool(session.chaos),
            persona_version=_persona_version(session),
        ) as turn:
            session.last_trace_id = turn.trace_id
            session.turn_count = session.db.next_turn(session.conversation_id)
            turn.set_turn_id(session.turn_id)
            with session.renderer.progress("Thinking...") as progress:
                answer = turn_fn(session, text, trace_id=turn.trace_id, progress=progress)
        session.renderer.answer(answer)  # the answer prints once, after the progress is gone
    except Exception as exc:  # noqa: BLE001 - the boundary must catch everything (KeyboardInterrupt is not an Exception)
        report_failure(session, exc, trace_id)
    finally:
        _warn_on_pii_alert(session, turn)
    return True


def _persona_version(session: ChatSession) -> str | None:
    """`id@vN+sha8` of the persona in use, for the trace (F41). A broken persona file means none."""
    if session.personas is None or not session.persona_name:
        return None
    try:
        return session.personas.get(session.persona_name).label
    except ConfigError:
        return None  # the turn carries on without a persona (decision 3.11)


def _warn_on_pii_alert(session: ChatSession, turn: TurnTrace | None) -> None:
    """One warning line after a turn whose row has PII at output (NFR20 stand-in)."""
    if turn is None:
        return
    try:
        warning = alerts.turn_alert(turn.row)
        if warning:
            session.renderer.warning(warning)
    except Exception:  # noqa: BLE001 - a warning line must never turn into a crash
        log.error("could not print the PII alert for trace_id=%s", turn.trace_id)
