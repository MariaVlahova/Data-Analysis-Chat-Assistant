"""Run scenarios against the real app (`scenario run`).

Each scenario gets its own data folder under the run's output folder (its own database, traces, logs and saved reports), so scenarios do not
see each other and a rerun starts clean. The session is the one `chat` starts (`open_session`), with the settings of the run, so what is
tested is the program a user gets: the same startup checks, guards, graph, tools and traces. The model is the one the environment names.

For each turn the runner collects the evidence (what the user saw, the trace, the metrics row, every prompt) and `checks.py` judges it.
A scenario fails when any invariant or `expect:` check fails; `advisory:` checks only warn.
"""
from __future__ import annotations

import json
import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from rich.console import Console

from src.cli.main import ChaosLevel, open_session
from src.access.users import load_users
from src.cli.render import Renderer
from src.cli.commands import UNNUMBERED_TURN, ChatSession
from src.config import Settings
from src.errors import AgentError, ConfigError, ErrorKind, crash_summary, friendly, new_trace_id
from src.llm.chaos import Chaos
from src.guards.allowlist import load_allowlist
from src.logging_setup import shutdown_logging
from src.scenarios import checks as C
from src.scenarios.checks import Check, Facts, TurnEvidence
from src.scenarios.recording import RecordingGateway
from src.scenarios.schema import ChaosSpec, Scenario
from src.scenarios.truth import Canaries, Macros, Truth

log = logging.getLogger(__name__)
Say = Callable[[str], None]


@dataclass
class TurnRecord:
    index: int
    say: str
    outcome: str
    text: str
    trace_id: str
    seconds: float
    checks: list[Check] = field(default_factory=list)


@dataclass
class ScenarioResult:
    id: str
    title: str
    user: str
    status: str  # pass | fail | skip
    skip_reason: str = ""
    turns: list[TurnRecord] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)  # about the scenario as a whole
    seconds: float = 0.0
    folder: str = ""

    def all_checks(self) -> list[Check]:
        return [*(check for turn in self.turns for check in turn.checks), *self.checks]

    def count(self, status: str) -> int:
        return sum(1 for check in self.all_checks() if check.status == status)


@dataclass
class Played:
    """What one conversation left behind: the evidence of each turn, the facts to judge it with, the folder the app wrote to, and the check of
    that folder's files for personal data (made after the session closed)."""

    evidence: list[TurnEvidence]
    facts: Facts
    folder: Path
    files_check: Check
    error: str = ""  # why the conversation stopped before its last line (a hook of the caller failed); the turns before it are still here


@dataclass
class RunReport:
    run_id: str
    environment: str
    model: str
    data: str  # "sample data" or "BigQuery"
    folder: str
    scenarios: list[ScenarioResult] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def failed(self) -> bool:
        return any(result.status == "fail" for result in self.scenarios)


def unmet_requirement(scenario: Scenario, settings: Settings) -> str:
    """Why a scenario cannot run with these settings, or ''."""
    if "sample_data" in scenario.requires and settings.bigquery.backend != "local":
        return "needs the sample data (bigquery.backend: local); this run reads BigQuery"
    if "golden" in scenario.requires and not settings.golden.enabled:
        return "needs the Golden Bucket (golden.enabled: true)"
    return ""


def quiet_renderer() -> Renderer:
    return Renderer(Console(quiet=True), Console(quiet=True, stderr=True))


class ScenarioRunner:
    def __init__(self, settings: Settings, out_dir: Path, *, say: Say = lambda text: None) -> None:
        self._settings = settings
        self._out = out_dir
        self._say = say
        # The right answers come from the data itself, whichever data it is: BigQuery, or the sample data through the local stand-in. Raises
        # ConfigError when it cannot be read, as the chat would not start either.
        users = load_users(settings)
        self._truth: Truth = Truth.from_settings(settings, other_users_brands=[b for name in users.names for b in users.get(name).brands])
        # Only the sample data has a list of customers' details to look for; against BigQuery the pattern scan alone looks for personal data.
        self._canaries: Canaries | None = Canaries(settings.resolve(settings.bigquery.local_db)) if settings.bigquery.backend == "local" else None

    @property
    def macros(self) -> Macros:
        """The macros of this data, for a caller that judges what `play` returns (`eval/`). Not the `Truth` itself: its figures bypass the guards."""
        return self._truth.macros

    def close(self) -> None:
        self._truth.close()

    # a run -----------------------------------------------------------------------------------------

    def run(self, scenarios: list[Scenario], run_id: str) -> RunReport:
        backend = self._settings.llm_backend
        report = RunReport(
            run_id, self._settings.app.environment, backend.model or "gemini models of llm.models",
            "sample data" if self._settings.bigquery.backend == "local" else "BigQuery", str(self._out),
        )
        started = time.perf_counter()
        for number, scenario in enumerate(scenarios, 1):
            self._say(f"[{number}/{len(scenarios)}] {scenario.id}: {scenario.title} (as {scenario.user})")
            result = self.run_scenario(scenario)
            report.scenarios.append(result)
            self._say(f"    {result.status.upper()}" + (f" ({result.skip_reason})" if result.skip_reason else
                      f" in {result.seconds:.0f} s: {result.count('pass')} passed, {result.count('fail')} failed, {result.count('warn')} warning(s)"))
        report.seconds = time.perf_counter() - started
        return report

    def run_scenario(self, scenario: Scenario) -> ScenarioResult:
        why = unmet_requirement(scenario, self._settings)
        if why:
            return ScenarioResult(scenario.id, scenario.title, scenario.user, "skip", skip_reason=why)
        result = ScenarioResult(scenario.id, scenario.title, scenario.user, "pass", folder=str(self._out / scenario.id))
        started = time.perf_counter()
        try:
            self._execute(scenario, result)
        except ConfigError as exc:  # the chat would not have started either
            result.checks.append(Check("start the chat", C.FAIL, exc.message + (f" Fix: {exc.fix}" if exc.fix else "")))
        except Exception as exc:  # noqa: BLE001 - one broken scenario must not stop the rest
            log.error("scenario %s crashed: %s", scenario.id, crash_summary(exc))
            result.checks.append(Check("run the scenario", C.FAIL, f"the runner crashed ({type(exc).__name__}); see the log"))
        result.seconds = time.perf_counter() - started
        result.status = "fail" if result.count(C.FAIL) else "pass"
        return result

    # one scenario ----------------------------------------------------------------------------------

    def _execute(self, scenario: Scenario, result: ScenarioResult) -> None:
        def judge(index: int, ev: TurnEvidence, facts: Facts) -> None:
            turn = scenario.turns[index - 1]
            allowed = [self._truth.expand_text(name, facts.user.brands) for name in [*turn.expect.allow_mentions, *turn.advisory.allow_mentions]]
            record_ = TurnRecord(index, ev.say, ev.outcome, ev.text, ev.trace_id, ev.seconds)
            record_.checks = [
                *C.invariants(ev, facts, list(dict.fromkeys(allowed))),
                *C.expectations(turn.expect, ev, facts),
                *C.expectations(turn.advisory, ev, facts, advisory=True),
            ]
            result.turns.append(record_)
            failed = [check for check in record_.checks if check.status == C.FAIL]
            self._say(f"      {ev.outcome} in {ev.seconds:.0f} s" + (f"; FAILED: {', '.join(check.name for check in failed)}" if failed else ""))

        played = self.play(
            scenario.id, scenario.user, [turn.say for turn in scenario.turns], setup=scenario.setup, chaos=scenario.chaos, after_turn=judge,
        )
        result.checks.append(C.confirmation_invariant(played.evidence))
        result.checks.append(played.files_check)
        if played.error:
            result.checks.append(Check("run the scenario", C.FAIL, played.error))

    def play(
        self, name: str, user: str, lines: Sequence[str], *, setup: Sequence[str] = (), chaos: ChaosSpec | None = None,
        chaos_engine: Chaos | None = None, before_turn: Callable[[int, str], None] | None = None,
        after_turn: Callable[[int, TurnEvidence, Facts], None] | None = None, restart_after: Sequence[int] = (),
    ) -> Played:
        """One conversation, as a user types it: the session `chat` starts, in its own data folder `<out>/<name>`, one line after another, nothing
        printed. This is what a scenario, a golden case and an attack all run on (`eval/`); what differs is only who judges the evidence.

        `before_turn(index, line)` runs just before a line is typed (a test with a scripted model gives it that turn's replies). `after_turn(index,
        evidence, facts)` runs right after each turn, while the session is open: a check on the state the turn left (the saved reports) cannot be made
        later. `chaos_engine` is a scripted `Chaos` that replaces the random one of `chaos`. `restart_after` lists the turns (1-based) after which the app
        is closed and started again on the same conversation, as `chat --conversation` does in a new process: what carried over must come back from the
        checkpoint on disk. Raises `ConfigError` when the chat would not start. Closes the session and the log files."""
        folder = self._out / name
        folder.mkdir(parents=True, exist_ok=True)
        # The schema and brand list were read once when the run began. Each conversation starts with that copy, so it does not read them from the
        # data again: against BigQuery that would be two small queries and four metadata calls per conversation.
        shared = self._settings.data_path / "cache"
        if shared.is_dir():
            shutil.copytree(shared, folder / "cache", dirs_exist_ok=True)
        settings = self._settings.model_copy(update={"app": self._settings.app.model_copy(update={"data_dir": str(folder)})})
        try:
            if "seed_reports" in setup:
                from src.localdata.reports import seed_saved_reports

                seed_saved_reports(settings)
            recorders: list[RecordingGateway] = []

            def record(gateway):
                recorders.append(RecordingGateway(gateway))
                return recorders[-1]

            level = ChaosLevel(chaos.level) if chaos else None

            def start(conversation: str | None) -> ChatSession:
                return open_session(
                    quiet_renderer(), user, None, debug=False, chaos=level, chaos_seed=chaos.seed if chaos else None,
                    conversation=conversation, settings=settings, gateway_wrapper=record, chaos_engine=chaos_engine,
                )

            session = start(None)
            evidence: list[TurnEvidence] = []
            stopped = ""
            try:
                facts = self._facts(settings, session)
                for index, line in enumerate(lines, 1):
                    say = self._truth.expand_text(line, session.user.brands)  # "{other_brand}" is a name from this data
                    self._say(f"    turn {index}/{len(lines)}: {say}")
                    try:
                        if before_turn is not None:
                            before_turn(index, say)
                        ev = self._ask(session, recorders[-1], say, index)
                        evidence.append(ev)
                        if after_turn is not None:
                            after_turn(index, ev, facts)
                        if index in restart_after and index < len(lines):
                            conversation = session.conversation_id
                            self._say(f"    the app is started again on conversation {conversation}")
                            session.close()
                            shutdown_logging()  # a new process sets its logging up again
                            session = start(conversation)
                    except Exception as exc:  # noqa: BLE001 - a hook of the caller failed: what the turns before it showed is still evidence, so it is kept
                        stopped = f"the conversation stopped at turn {index}: {crash_summary(exc)}"
                        log.error("%s: %s", name, stopped)  # `_ask` turns the app's own failures into evidence, so what reaches here is a hook of the caller
                        break
            finally:
                session.close()
            return Played(evidence, facts, folder, self._files_check(folder), stopped)
        finally:
            shutdown_logging()

    def _facts(self, settings: Settings, session: ChatSession) -> Facts:
        from src.reports.store import ReportStore

        allowlist = load_allowlist(settings)
        blocked = tuple(sorted({column for table in allowlist.table_names() for column in allowlist.blocked(table)}))
        store = ReportStore.from_settings(settings, hash_ids=lambda ids: "scenario")
        return Facts(
            settings, session.user, blocked, self._truth, self._canaries,
            saved_reports=lambda: len(store.list_reports(session.user.user_id)),
            latest_report_title=lambda: next((row.title for row in store.find(session.user.user_id, "", "last")), None),
        )

    def _ask(self, session: ChatSession, recorder: RecordingGateway, say: str, index: int) -> TurnEvidence:
        """One typed line, through the agent, inside a trace, as `handle_line` does it. Nothing is printed."""
        trace_id = new_trace_id()
        first_call = len(recorder.calls)
        started = time.perf_counter()
        outcome, text, error_kind, turn = "crashed", "", None, None
        try:
            with session.tracer.start_turn(
                session.user.user_hash, session.conversation_id, UNNUMBERED_TURN, trace_id=trace_id, question=say, chaos=bool(session.chaos),
            ) as turn:
                session.turn_count = session.db.next_turn(session.conversation_id)
                turn.set_turn_id(session.turn_id)
                answer = session.agent.run(
                    text=say, conversation_id=session.conversation_id, turn_id=session.turn_id, trace_id=turn.trace_id,
                    user_hash=session.user.user_hash,
                )
                outcome = "awaiting_confirmation" if answer.awaiting_confirmation else (answer.outcome or "answered")
                text = friendly(answer.error, turn.trace_id) if answer.error is not None else answer.text
                error_kind = answer.error.kind.value if answer.error is not None else None
        except Exception as exc:  # noqa: BLE001 - what the CLI's boundary would catch; the evidence says it happened
            error = AgentError.from_exception(exc, trace_id)
            outcome = "failed" if isinstance(exc, AgentError) and error.kind is not ErrorKind.INTERNAL else "crashed"
            text, error_kind = friendly(error, trace_id), error.kind.value
        row = dict(turn.row) if turn is not None else {}
        seconds = time.perf_counter() - started
        spans, trace_text = self._read_trace(session, trace_id)
        return TurnEvidence(
            index, say, outcome, text, error_kind, trace_id, seconds, spans, row,
            [call.text for call in recorder.calls[first_call:]], trace_text, self._read_state(session),
        )

    @staticmethod
    def _read_state(session: ChatSession) -> dict:
        """The conversation's checkpoint after the turn, for the checks on what carries over (`eval/trajectory.py`). {} when it cannot be read: those
        checks then say they could not be made, and a missing state is never a pass."""
        state_of = getattr(session.agent, "state_of", None)
        if state_of is None:
            return {}
        try:
            return state_of(session.conversation_id)
        except Exception as exc:  # noqa: BLE001 - a checkpoint that cannot be read costs the checks on it, not the run
            log.error("could not read the checkpoint after the turn: %s", crash_summary(exc))
            return {}

    @staticmethod
    def _read_trace(session: ChatSession, trace_id: str) -> tuple[list[dict], str]:
        try:
            raw = session.tracer.trace_path(trace_id).read_text(encoding="utf-8")
        except OSError:
            return [], ""
        spans = []
        for line in raw.split("\n"):
            try:
                spans.append(json.loads(line))
            except ValueError:
                continue
        return spans, raw

    def _files_check(self, folder: Path) -> Check:
        """After the scenario: no customer detail in any file the app wrote (logs, traces, metrics, the database and its checkpoints)."""
        if self._canaries is None:
            return Check("I1 no personal data in the files", C.SKIP, "no sample data to know the customers' details")
        problems: list[str] = []
        count = 0
        for path in sorted(folder.rglob("*")):
            if not path.is_file() or path.suffix == ".duckdb":
                continue
            count += 1
            kinds = self._canaries.find(path.read_bytes().decode("latin-1"))
            problems += [f"{kind} in {path.relative_to(folder).as_posix()}" for kind in kinds]
        return Check("I1 no personal data in the files", C.FAIL if problems else C.PASS, "; ".join(problems) or f"{count} file(s) read")
