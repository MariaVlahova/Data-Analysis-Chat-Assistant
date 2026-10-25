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
from typing import Annotated, Any, Callable, Optional

import typer

from src.access.users import load_users, require_access
from src.cli.commands import ChatSession, TurnFn, handle_line, run_turn
from src.cli.bigquery_cmd import bigquery_app
from src.cli.doctor import DoctorContext, render_checks, run_checks
from src.cli.golden_cmd import golden_app
from src.cli.local_data_cmd import local_data_app
from src.cli.render import Renderer
from src.cli.scenario_cmd import scenario_app
from src.config import Settings, load_settings
from src.errors import AgentError, ConfigError, ErrorKind, crash_summary, friendly, new_trace_id
from src.guards.allowlist import load_allowlist
from src.llm.chaos import Chaos  # light: the Google SDK loads only with src.llm.gateway
from src.logging_setup import setup_logging, shutdown_logging
from src.observability import Tracer, purge_old_traces, viewer
from src.personas import PersonaStore
from src.storage.db import Database

log = logging.getLogger("src.cli.main")  # not __name__: under `python -m` that is "__main__", outside the `src` logger

PENDING_SEARCH = 20  # how many of the user's recent conversations are looked at for one that is waiting for a confirmation

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
app.add_typer(local_data_app, name="local-data")  # generated sample data for running without Google Cloud
app.add_typer(golden_app, name="golden")  # plan 10: `golden validate`
app.add_typer(scenario_app, name="scenario")  # scripted test scenarios against the real app
app.add_typer(bigquery_app, name="bigquery")  # `bigquery init-users`: the first of the steps for real BigQuery


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
    settings: Settings | None = None,
    gateway_wrapper: Callable[[Any], Any] | None = None,
    chaos_engine: Chaos | None = None,
) -> ChatSession:
    """Everything that must be right before a chat can start. Raises ConfigError for a startup problem.

    `settings` and `gateway_wrapper` are for the scenario runner (`src/scenarios`), which starts the same session as `chat` does: it
    brings settings of its own (a data folder per run) and wraps the model gateway to record what each prompt held. `chaos_engine` is for
    tests and the evaluation: a `Chaos` they built and scripted themselves, used instead of one made from `chaos`."""
    if chaos_engine is not None and (chaos is not None or chaos_seed is not None):
        raise ValueError("`chaos_engine` is an engine the caller built: it replaces `chaos` and `chaos_seed`, and passing them too would describe a profile that is not running")
    if chaos_seed is not None and chaos is None:
        raise ConfigError("--chaos-seed has no effect without --chaos.", fix="Add --chaos mild or --chaos harsh, or leave out --chaos-seed.")
    if chaos_engine is None and chaos is not None:
        chaos_engine = Chaos.from_level(chaos.value, chaos_seed)  # without a seed one is drawn; it is shown in the banner
    settings = settings or load_settings(config)
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
    checkpointer = None
    try:
        # Cheap local checks first, so a mistake in them is not reported after a wait on the network.
        db.migrate()
        _housekeeping("the purge of deleted reports", lambda: _purge_deleted_reports(settings))
        personas, persona_name = _open_personas(settings)
        allowlist = load_allowlist(settings)  # plan 05: no valid allow-list, no column the agent may use
        if not allowlist.reviewed_against_live:
            if settings.guards.require_reviewed_allowlist:
                raise ConfigError(
                    "schema_allowlist.yaml has not been reviewed against the live schema, so the chat cannot start. "
                    "It was written from knowledge of the dataset, and a column under the wrong heading would quietly weaken the privacy rules.",
                    fix="Run: python -m src.cli.main doctor --refresh, then pytest -m live -s tests/test_live_sql_guard.py. When both pass and the "
                        "PII split is agreed with the owner, set reviewed_against_live: true in schema_allowlist.yaml "
                        "(or set guards.require_reviewed_allowlist: false in config.yaml to start anyway).",
                )
            log.warning("schema_allowlist.yaml has not been marked as reviewed against the live schema (reviewed_against_live)")
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
            from src.agent.checkpointer import open_checkpointer, waiting_conversations
            from src.agent.runtime import build_agent
            from src.data_access.brand_check import check_brand_mapping
            from src.data_access.catalog import load_catalog

            loaded = load_catalog(settings)
        for warning in loaded.warnings:
            log.warning("%s", warning)
            renderer.warning(warning)
        # A listed column that is not in BigQuery stops startup (the file is wrong). A live column that is in neither list
        # stays hidden and is logged. Only now can the catalog show the agent any column (plan 03's hand-off).
        catalog = loaded.catalog.with_policy(allowlist)
        # `doctor` audits every user and shows close matches. The chat checks this user and names no other brands.
        check_brand_mapping(directory, catalog, only_user=user.user_id, suggest_names=False)
        golden = _open_golden(settings, allowlist, catalog, renderer)
        # The agent (plan 07): gateway, runner, the three guards, the tools and the graph. Built before the conversation is
        # created, so a failure here (an sqlglot the guard was not checked against, for one) leaves no conversation behind.
        # The checkpoints are on disk (plan 08): a confirmation that was waiting when the last chat ended is waiting still.
        checkpointer = open_checkpointer(settings)
        _housekeeping("the purge of old checkpoints", lambda: _purge_old_checkpoints(settings, checkpointer))
        agent = build_agent(
            settings, user=user, catalog=catalog, allowlist=allowlist, personas=personas, persona_name=persona_name,
            chaos=chaos_engine, checkpointer=checkpointer, golden=golden, gateway_wrapper=gateway_wrapper,
        )

        if existing is None and not conversation:
            # No conversation was asked for: carry on with one that is waiting for the user's yes or no, if there is one. It is the
            # most recently active of them, whichever conversation was used since. The search is a convenience: if it fails, the
            # chat starts a new conversation, and the waiting one is still there for `--conversation`.
            waiting = _find_waiting_conversation(db, agent, checkpointer, user.user_id, waiting_conversations)
            if waiting is not None:
                existing, conversation = waiting, waiting["id"]
        if existing is not None:
            conversation_id, turn_count = conversation, existing["turn_count"]  # turn IDs carry on, never restart
        else:
            conversation_id, turn_count = db.create_conversation(user.user_id), 0
        tracer = Tracer.from_settings(settings, debug_prompts=debug_prompts)  # inside the try: if it fails, what is open is closed
    except BaseException:
        if checkpointer is not None:
            from src.agent.checkpointer import close_checkpointer

            close_checkpointer(checkpointer)
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
        chaos=(chaos_engine.level or "scripted") if chaos_engine else None,  # an engine the caller built keeps its own level; only a scripted one has none
        chaos_seed=chaos_engine.seed if chaos_engine else None,
        chaos_engine=chaos_engine,
        personas=personas,
        persona_name=persona_name,
        tracer=tracer,
        catalog=catalog,
        agent=agent,
        checkpointer=checkpointer,
    )


def _open_golden(settings: Settings, allowlist: Any, catalog: Any, renderer: Renderer) -> Any:
    """Plan 10: the solved questions and the glossary, or None when `golden.enabled` is false. Nothing in them can stop the chat from
    starting: they only add examples to a prompt, so a failure here is a warning and the chat goes on without them."""
    if not settings.golden.enabled:
        return None
    try:
        from src.golden.load import open_golden

        bundle = open_golden(settings, allowlist, catalog.brands)
    except Exception as exc:  # noqa: BLE001 - optional, see above
        log.error("the Golden Bucket could not be loaded: %s", crash_summary(exc))
        renderer.warning("The Golden Bucket could not be loaded, so answers will not use solved examples.")
        return None
    for warning in bundle.warnings:
        log.warning("%s", warning)
        renderer.warning(warning)
    return bundle


def _find_waiting_conversation(db: Database, agent: Any, checkpointer: Any, user_id: str, candidates: Callable[..., list[str]]) -> Any:
    """The user's most recently active conversation that is waiting at the HITL Gate, as its `conversations` row, or None. A
    checkpoint that cannot be read is skipped (and logged): one bad conversation must not stop the chat."""
    try:
        ids = candidates(checkpointer, user_id, PENDING_SEARCH)
    except Exception as exc:  # noqa: BLE001 - optional, see above
        log.error("could not look for a conversation waiting for a confirmation: %s", crash_summary(exc))
        return None
    for conversation_id in ids:
        try:
            if agent.pending_confirmation(conversation_id) is not None:
                return db.get_conversation(conversation_id, user_id)
        except Exception as exc:  # noqa: BLE001 - see above
            log.error("could not read the checkpoint of a conversation: %s", crash_summary(exc))
    return None


def _housekeeping(what: str, action: Callable[[], None]) -> None:
    """Startup cleanup is optional: if it fails (the database is busy, a trigger refuses a row), the chat still starts, and the
    next start tries again. The log gets where it failed, never the exception text."""
    try:
        action()
    except Exception as exc:  # noqa: BLE001 - nothing here may stop the chat
        log.error("startup cleanup failed (%s): %s", what, crash_summary(exc))


def _purge_deleted_reports(settings: Settings) -> None:
    """Reports deleted more than `delete.undo_days` ago are removed for good (F25)."""
    from src.reports.store import purge_expired

    removed = purge_expired(settings.db_path, undo_days=settings.delete.undo_days)
    if removed:
        log.info("removed %d deleted report(s) past their undo window", removed)


def _purge_old_checkpoints(settings: Settings, checkpointer: Any) -> None:
    """The checkpoints of conversations idle for more than `retention.checkpoint_days` (NFR21, 90 by default)."""
    from src.agent.checkpointer import purge_old_checkpoints

    removed = purge_old_checkpoints(checkpointer, settings.retention.checkpoint_days)
    if removed:
        log.info("deleted the checkpoints of %d conversation(s) idle for more than %d days", removed, settings.retention.checkpoint_days)


def _open_personas(settings: Settings) -> tuple[PersonaStore | None, str | None]:
    if not settings.persona.enabled:
        return None, None
    store = PersonaStore(settings.resolve("personas"))
    store.get(settings.persona.default)  # a missing default persona is a startup problem
    return store, settings.persona.default


def banner(session: ChatSession) -> str:
    user = session.user
    scope = "all brands" if user.scope_mode == "all" else "brands: " + ", ".join(user.brands)
    text = (
        f"Hi {user.display_name} ({user.role}). You can see {scope}.\n"
        f"Conversation {session.conversation_id}. Type /help for commands, /quit to leave."
    )
    if session.settings.bigquery.backend == "local":
        text += "\nLOCAL SAMPLE DATA: these answers come from generated test data in DuckDB, not from BigQuery."
    backend = session.settings.llm_backend
    if backend.provider != "gemini":
        text += f"\nLOCAL MODEL: {backend.model} at {backend.base_url} (environment {session.settings.app.environment}), not Gemini."
    if session.chaos:
        replayable = session.chaos in ("mild", "harsh") and session.chaos_seed is not None  # a scripted engine has no flag and no seed
        replay = f" Replay a run with --chaos {session.chaos} --chaos-seed {session.chaos_seed}." if replayable else ""
        seed = f" (seed {session.chaos_seed})" if session.chaos_seed is not None else ""
        text += (f"\nChaos mode {session.chaos} is on{seed}: faults are injected on purpose into model "
                 f"calls, queries and the SQL the model writes, to show how the agent copes." + replay)
    return text


def show_pending_confirmation(session: ChatSession) -> None:
    """A conversation that stopped at the HITL Gate (possibly in an earlier chat) shows its preview again. The next typed line is
    the reply, exactly as if the chat had never closed. `/new` drops it."""
    if session.agent is None:
        return
    try:
        pending = session.agent.pending_confirmation(session.conversation_id)
    except Exception as exc:  # noqa: BLE001 - the chat starts; a turn in this conversation reports a checkpoint it cannot read
        log.error("could not read the checkpoint of this conversation: %s", crash_summary(exc))
        return
    if pending:
        session.renderer.info("This conversation is waiting for your confirmation. Nothing has been changed yet.")
        session.renderer.answer(pending)
        session.renderer.info("Type your reply, or /new to cancel it and start fresh.")


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
        show_pending_confirmation(session)
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
            session.close()
        shutdown_logging()


# doctor -----------------------------------------------------------------------------------------

@app.command()
def doctor(
    config: Annotated[Optional[Path], typer.Option("--config", help="Path to config.yaml.")] = None,
    refresh: Annotated[bool, typer.Option("--refresh", help="Read the BigQuery schema and brand list again instead of using the saved copy.")] = False,
    live: Annotated[bool, typer.Option("--live", help="Also call Gemini: check the model IDs and make one small call (uses a little free-tier quota).")] = False,
) -> None:
    """Check the setup: Python, packages, config, users, keys, credentials, the database, BigQuery and (with --live) Gemini."""
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
        checks = run_checks(DoctorContext(settings=settings, config_error=error, refresh=refresh, live=live))
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
