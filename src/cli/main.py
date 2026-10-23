"""Typer + Rich CLI: `python -m src.cli.main chat --user alice`.

Exit codes: 0 normal, 1 a failed `doctor` check, a `trace` ID that was not found or an unexpected startup
crash, 2 a startup problem (bad config, unknown user, no access, failed database setup). Startup problems print one clear
sentence and the fix. Traces never reach the screen unless `--debug`.
"""
from __future__ import annotations

import logging
import sys
from enum import Enum
from pathlib import Path
from typing import Annotated, Optional

import typer

from src.access.users import load_users, require_access
from src.cli.commands import ChatSession, TurnFn, handle_line, run_turn
from src.cli.doctor import DoctorContext, render_checks, run_checks
from src.cli.render import Renderer
from src.config import Settings, load_settings
from src.errors import AgentError, ConfigError, ErrorKind, crash_summary, friendly, new_trace_id
from src.logging_setup import setup_logging, shutdown_logging
from src.observability import Tracer, purge_old_traces, viewer
from src.personas import PersonaStore
from src.storage.db import Database

log = logging.getLogger("src.cli.main")  # not __name__: under `python -m` that is "__main__", outside the `src` logger

STARTUP_CRASH_MESSAGE = (
    "Something went wrong and the chat had to close. Please start it again. "
    "If it keeps happening, share the trace ID."
)

app = typer.Typer(
    name="opsfleet",
    help="Data Analysis Chat Assistant: ask questions about the theLook e-commerce data.",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,  # never print a traceback with local variables to an executive
)


class ChaosLevel(str, Enum):
    mild = "mild"
    harsh = "harsh"


@app.callback()
def _root() -> None:
    """Data Analysis Chat Assistant: ask questions about the theLook e-commerce data."""
    # Never crash on a character the console cannot encode (for example a redirected Windows console).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


# chat -------------------------------------------------------------------------------------------

def open_session(
    renderer: Renderer,
    user_name: str,
    config: Path | None,
    *,
    debug: bool,
    chaos: ChaosLevel | None,
    chaos_seed: int | None,
    conversation: str | None,
    debug_prompts: bool = False,
) -> ChatSession:
    """Everything that must be right before a chat can start. Raises ConfigError for a startup problem."""
    settings = load_settings(config)
    setup_logging(settings, debug)
    removed = purge_old_traces(settings.traces_path, settings.retention.trace_days)  # NFR22: 90 days by default
    if removed:
        log.info("deleted %d trace file(s) older than %d days", removed, settings.retention.trace_days)

    try:
        directory = load_users(settings)
    except AgentError as exc:  # ACCESS_STORE_DOWN: block (NFR15)
        log.error("access store down: %s", exc.detail)
        raise ConfigError(
            f"The user list could not be read ({exc.detail}), so the chat cannot start.",
            fix="Check that users.yaml exists next to config.yaml and can be read.",
        ) from None
    user = directory.get(user_name)

    try:
        require_access(user)  # S14: no brands means no access, never "all brands"
    except AgentError:
        log.warning("refused to start chat: scope none for user_hash=%s", user.user_hash)
        raise ConfigError(
            f"No brands are assigned to this user ('{user.user_id}'), so the chat cannot start.",
            fix="Add brands for this user in users.yaml. Only the CEO can have all_brands: true.",
        ) from None

    db = Database.from_settings(settings)
    try:
        # Cheap local checks first, so a mistake in them is not reported after a wait on the network.
        db.migrate()
        personas, persona_name = _open_personas(settings)
        existing = None
        if conversation:
            existing = db.get_conversation(conversation, user.user_id)
            if existing is None:
                raise ConfigError(
                    f"Conversation '{conversation}' was not found for user '{user.user_id}'.",
                    fix="Leave out --conversation to start a new one.",
                )

        # S1: a typo in a brand name must stop startup, not become an empty scope that looks like "no sales".
        # With BigQuery down a recent saved copy is used (and said so); otherwise this raises and we stop.
        # Imported here: BigQuery and pandas load only when a chat starts, so `trace`, `metrics` and a broken-install
        # `doctor` never pay for them. The check is the last step before the conversation is created, so a failure
        # leaves no conversation behind.
        with renderer.progress("Checking the data connection..."):  # the imports are part of the wait
            from src.data_access.brand_check import check_brand_mapping
            from src.data_access.catalog import load_catalog

            loaded = load_catalog(settings)
        for warning in loaded.warnings:
            log.warning("%s", warning)
            renderer.warning(warning)
        # `doctor` audits every user and shows close matches. The chat checks this user and names no other brands.
        check_brand_mapping(directory, loaded.catalog, only_user=user.user_id, suggest_names=False)

        if existing is not None:
            conversation_id, turn_count = conversation, existing["turn_count"]  # turn IDs carry on, never restart
        else:
            conversation_id, turn_count = db.create_conversation(user.user_id), 0
    except BaseException:
        db.close()
        raise

    return ChatSession(
        settings=settings,
        user=user,
        db=db,
        renderer=renderer,
        conversation_id=conversation_id,
        turn_count=turn_count,
        debug=debug,
        chaos=chaos.value if chaos else None,
        chaos_seed=chaos_seed,
        personas=personas,
        persona_name=persona_name,
        tracer=Tracer.from_settings(settings, debug_prompts=debug_prompts),
        catalog=loaded.catalog,
    )


def _open_personas(settings: Settings) -> tuple[PersonaStore | None, str | None]:
    if not settings.persona.enabled:
        return None, None
    store = PersonaStore(settings.resolve("personas"))
    store.get(settings.persona.default)  # a missing default persona is a startup problem
    return store, settings.persona.default


def banner(session: ChatSession) -> str:
    user = session.user
    scope = "all brands" if user.scope_mode == "all" else "brands: " + ", ".join(user.brands)
    return (
        f"Hi {user.display_name} ({user.role}). You can see {scope}.\n"
        f"Conversation {session.conversation_id}. Type /help for commands, /quit to leave."
    )


def chat_loop(session: ChatSession, turn_fn: TurnFn | None = None) -> None:
    """Read lines until /quit, end of input or Ctrl+C. Everything else is handled inside `handle_line`."""
    renderer = session.renderer
    turn_fn = turn_fn or run_turn  # looked up now, so plan 07 (or a test) can replace the module's run_turn
    while True:
        try:
            line = renderer.prompt(session.user.display_name)
        except (EOFError, KeyboardInterrupt):
            renderer.console.print()
            break
        try:
            if not handle_line(session, line, turn_fn):
                break
        except KeyboardInterrupt:
            renderer.console.print()
            break
    renderer.goodbye()


@app.command()
def chat(
    user: Annotated[str, typer.Option("--user", "-u", help="Who is asking, a name from users.yaml (for example alice).")],
    chaos: Annotated[Optional[ChaosLevel], typer.Option("--chaos", help="Inject faults to show the resilience.")] = None,
    chaos_seed: Annotated[Optional[int], typer.Option("--chaos-seed", help="Seed for --chaos, so a run can be replayed.")] = None,
    conversation: Annotated[Optional[str], typer.Option("--conversation", help="Continue an earlier conversation by its ID.")] = None,
    config: Annotated[Optional[Path], typer.Option("--config", help="Path to config.yaml (else $OPSFLEET_CONFIG, else ./config.yaml).")] = None,
    debug: Annotated[bool, typer.Option("--debug", help="Show log lines and tracebacks on screen.")] = False,
    debug_prompts: Annotated[bool, typer.Option("--debug-prompts", help="Store prompt text (masked) in traces. Off by default.")] = False,
) -> None:
    """Start a chat for a user."""
    renderer = Renderer()
    session: ChatSession | None = None
    try:
        session = open_session(
            renderer, user, config, debug=debug, chaos=chaos, chaos_seed=chaos_seed, conversation=conversation,
            debug_prompts=debug_prompts,
        )
        renderer.banner(banner(session))
        chat_loop(session)
    except ConfigError as exc:
        renderer.startup_problem(exc.message, exc.fix)
        raise typer.Exit(2) from None
    except Exception as exc:  # noqa: BLE001 - the interface never crashes (NFR19)
        trace_id = new_trace_id()
        # Class and location only: the exception text can carry data that no masking pattern catches.
        log.error("chat crashed trace_id=%s %s", trace_id, crash_summary(exc))
        renderer.error(friendly(AgentError(ErrorKind.INTERNAL, STARTUP_CRASH_MESSAGE, trace_id=trace_id)))
        if debug:
            renderer.traceback()
        raise typer.Exit(1) from None
    finally:
        if session is not None:
            session.db.close()
        shutdown_logging()


# doctor -----------------------------------------------------------------------------------------

@app.command()
def doctor(
    config: Annotated[Optional[Path], typer.Option("--config", help="Path to config.yaml.")] = None,
    refresh: Annotated[bool, typer.Option("--refresh", help="Read the BigQuery schema and brand list again instead of using the saved copy.")] = False,
) -> None:
    """Check the setup: Python, packages, config, users, keys, credentials, the database and BigQuery."""
    renderer = Renderer()
    try:
        settings, error = load_settings(config), None
    except ConfigError as exc:
        settings, error = None, exc
    except Exception as exc:  # noqa: BLE001 - a diagnostic must still print its checks
        settings, error = None, ConfigError(f"The config could not be loaded ({crash_summary(exc)}).")
    try:
        if settings is not None:
            setup_logging(settings)  # the BigQuery checks log to the masked app.log, not to the screen
        checks = run_checks(DoctorContext(settings=settings, config_error=error, refresh=refresh))
    finally:
        shutdown_logging()
    render_checks(renderer, checks)
    raise typer.Exit(1 if any(c.status == "fail" for c in checks) else 0)


# metrics and trace (read-only, for the operator) ------------------------------------------------

def _load_settings_or_exit(renderer: Renderer, config: Path | None) -> Settings:
    try:
        return load_settings(config)
    except ConfigError as exc:
        renderer.startup_problem(exc.message, exc.fix)
        raise typer.Exit(2) from None


@app.command()
def metrics(
    last: Annotated[Optional[int], typer.Option("--last", min=1, help="Only the last N turns.")] = None,
    config: Annotated[Optional[Path], typer.Option("--config", help="Path to config.yaml.")] = None,
) -> None:
    """Show totals and rates from runs.jsonl, and the month's BigQuery use against the free tier."""
    renderer = Renderer()
    viewer.show_metrics(renderer.console, _load_settings_or_exit(renderer, config), last)


@app.command()
def trace(
    trace_id: Annotated[Optional[str], typer.Argument(help="The 8-character trace ID. Default: the last turn.")] = None,
    raw: Annotated[bool, typer.Option("--raw", help="Print the stored spans as JSON.")] = False,
    config: Annotated[Optional[Path], typer.Option("--config", help="Path to config.yaml.")] = None,
) -> None:
    """Show the steps of one turn by its trace ID (any user: this is the operator view)."""
    renderer = Renderer()
    found = viewer.show_trace(renderer.console, _load_settings_or_exit(renderer, config), trace_id, raw=raw)
    raise typer.Exit(0 if found else 1)


if __name__ == "__main__":
    app()
