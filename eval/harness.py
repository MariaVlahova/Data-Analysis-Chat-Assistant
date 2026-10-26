"""Play a golden case, an attack or a chaos scenario on the real app, and keep what is needed to judge it (plan 09).

`Harness.play_*` runs one conversation through `ScenarioRunner.play`: the session `chat` starts, the same startup checks, guards, graph, tools and traces,
in its own data folder, with the model the configuration names. A live run uses the real model. A test installs a scripted one before it calls `play_*`
(`tests/test_attack_offline.py`), so the same harness proves the machinery with no network.

What comes back is a `Sample`: the evidence of every turn, the facts to judge it with, the folder the app wrote to, and for a golden case the figures of its
reference SQL. The judging is `eval/metrics.py`; nothing here decides pass or fail.

The reference SQL runs through the SAME SQL Guard and query runner as the same user (`SQLGuard.check`, then `SafeQueryRunner.run`), so its scope is the
user's by construction. This is not the bypass that `ReferenceQueries` is: a reference the guard refuses is a mistake in the case, and is reported as that.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

import pandas as pd

from eval.datasets import Attack, ChaosScenario, GoldenCase, GoldenTurn, TrajectoryCase
from src.access.users import UserContext, load_users
from src.config import Settings
from src.data_access.safe_runner import SafeQueryRunner
from src.errors import AgentError, ConfigError, crash_summary
from src.guards.allowlist import load_allowlist
from src.guards.sql_guard import SQLGuard
from src.llm.chaos import Chaos
from src.scenarios import checks as C
from src.scenarios.checks import Check, Facts, TurnEvidence
from src.scenarios.runner import Played, ScenarioRunner
from src.scenarios.schema import ChaosSpec
from src.scenarios.truth import Macros

Say = Callable[[str], None]
BeforeTurn = Callable[[int, str], None]


class NoColumn(Exception):
    """A case names a column that its reference SQL does not return. The message is ours (the column and the ones returned), so it may be kept."""


@dataclass
class TurnReference:
    """The right answer of one golden turn, from its reference SQL."""

    values: list[float] = field(default_factory=list)  # the first `top_n` cells of `compare.column`
    rows: int | None = None  # how many rows the reference returned
    others: dict[str, list[float]] = field(default_factory=dict)  # `exclude_figures_of`: the same values, for each of those users
    hidden: list[str] = field(default_factory=list)  # the values the answer must not name
    visible: list[str] = field(default_factory=list)  # the groups the answer may name: the reference's own values of the hidden column, when it returns it
    error: str = ""  # why the reference could not be made (the guard refused it, the query failed); a mistake in the case


@dataclass
class Sample:
    """One conversation, played and ready to be judged."""

    kind: str  # golden | attack | chaos | trajectory
    id: str
    spec: GoldenCase | Attack | ChaosScenario  # a TrajectoryCase is a GoldenCase
    user: UserContext
    lines: list[str]
    played: Played | None = None  # None when the chat did not start (`error` says why)
    saved_reports: list[int | None] = field(default_factory=list)  # the user's saved reports, after each turn (None: not read)
    references: list[TurnReference] = field(default_factory=list)  # golden only: one per turn
    seconds: float = 0.0
    error: str = ""
    crashed: bool = False  # the error is a crash, not the chat refusing to start (an unknown user, a startup check)
    report_titles: list[str | None] = field(default_factory=list)  # trajectory only: the title of the newest saved report after each turn that asks (None: not read)
    _invariants: dict[int, dict[str, Check]] = field(default_factory=dict, repr=False)

    @property
    def evidence(self) -> list[TurnEvidence]:
        return self.played.evidence if self.played else []

    @property
    def facts(self) -> Facts | None:
        return self.played.facts if self.played else None

    def allowed_mentions(self, index: int) -> list[str]:
        """The brands of other users that this turn's question names, so the answer may repeat them, with the macros filled in."""
        facts = self.facts
        if facts is None or facts.truth is None:
            return []
        names: list[str] = []
        if isinstance(self.spec, GoldenCase):
            names = list(self.spec.turns[index].allow_mentions) if index < len(self.spec.turns) else []
        elif isinstance(self.spec, Attack):
            names = list(self.spec.allow_mentions)
        return list(dict.fromkeys(facts.truth.expand_text(name, facts.user.brands) for name in names))

    def invariants(self, index: int) -> dict[str, Check]:
        """I1, I2, I3, I5 and I6 of one turn, by their prefix. Made once: the scan reads every prompt and the whole trace."""
        if index not in self._invariants:
            checks = C.invariants(self.evidence[index], self.facts, self.allowed_mentions(index))  # type: ignore[arg-type]
            self._invariants[index] = {check.name.split(" ", 1)[0]: check for check in checks}
        return self._invariants[index]


class Harness:
    def __init__(self, settings: Settings, out_dir: Path, *, say: Say = lambda text: None) -> None:
        """Raises `ConfigError` when the data cannot be read, as the chat would not start either."""
        self._settings = settings
        self._say = say
        self._out = out_dir
        self._runner = ScenarioRunner(settings, out_dir, say=say)
        self.users = load_users(settings)
        self._guard = SQLGuard(settings, load_allowlist(settings))
        self._query = SafeQueryRunner(settings)

    def close(self) -> None:
        self._runner.close()

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def macros(self) -> Macros:
        """The macros of this data (`src/scenarios/truth.py`). No right answers: those come from reference SQL through the guard (`reference`)."""
        return self._runner.macros

    def user(self, name: str) -> UserContext:
        return self.users.get(name)

    # the three kinds -------------------------------------------------------------------------------------------

    def play_golden(self, case: GoldenCase, *, before_turn: BeforeTurn | None = None) -> Sample:
        sample = self._sample("golden", case, case.user, [turn.say for turn in case.turns])
        saved: list[int | None] = []

        def after(index: int, ev: TurnEvidence, facts: Facts) -> None:
            wanted = case.turns[index - 1].saved_reports is not None and facts.saved_reports is not None
            saved.append(facts.saved_reports() if wanted else None)  # type: ignore[misc]

        self._play(sample, case.id, setup=case.setup, before_turn=before_turn, after_turn=after)
        sample.saved_reports = saved
        if sample.played is not None:
            sample.references = [self.reference(turn, sample.user) for turn in case.turns]
        return sample

    def play_trajectory(self, case: TrajectoryCase, *, before_turn: BeforeTurn | None = None) -> Sample:
        """A golden case's way of playing, plus the app started again where the case says, and the title of the newest saved report after the turns that ask."""
        sample = self._sample("trajectory", case, case.user, [turn.say for turn in case.turns])
        saved: list[int | None] = []
        titles: list[str | None] = []

        def after(index: int, ev: TurnEvidence, facts: Facts) -> None:
            turn = case.turns[index - 1]
            saved.append(facts.saved_reports() if turn.saved_reports is not None and facts.saved_reports is not None else None)  # type: ignore[misc]
            asked = turn.memory is not None and turn.memory.saved_title_of is not None and facts.latest_report_title is not None
            titles.append(facts.latest_report_title() if asked else None)  # type: ignore[misc]

        self._play(sample, case.id, setup=case.setup, before_turn=before_turn, after_turn=after, restart_after=case.restart_after)
        sample.saved_reports, sample.report_titles = saved, titles
        if sample.played is not None:
            sample.references = [self.reference(turn, sample.user) for turn in case.turns]
        return sample

    def play_attack(self, attack: Attack, *, before_turn: BeforeTurn | None = None) -> Sample:
        sample = self._sample("attack", attack, attack.user, attack.lines)
        self._play(sample, attack.id, setup=attack.setup, before_turn=before_turn)
        return sample

    def play_chaos(
        self, scenario: ChaosScenario, *, before_turn: BeforeTurn | None = None, chaos_engine: Chaos | None = None,
    ) -> Sample:
        """`chaos_engine` is the scripted engine of an offline run; a live run has only the random profile of the scenario."""
        sample = self._sample("chaos", scenario, scenario.user, scenario.lines())
        spec = ChaosSpec(level=scenario.chaos["level"], seed=scenario.chaos.get("seed")) if scenario.chaos else None
        self._play(sample, scenario.id, chaos=spec, chaos_engine=chaos_engine, before_turn=before_turn)
        return sample

    def play_lines(
        self, name: str, user: str, lines: Sequence[str], *, setup: Sequence[str] = (), chaos_engine: Chaos | None = None, before_turn: BeforeTurn | None = None,
    ) -> Played:
        """Any conversation in the folder `name`, for a check that is none of the three kinds (the same folder twice is a restart of the app). Not judged."""
        return self._runner.play(name, user, lines, setup=setup, chaos_engine=chaos_engine, before_turn=before_turn)

    # one conversation -----------------------------------------------------------------------------------------

    def _sample(self, kind: str, spec: GoldenCase | Attack | ChaosScenario, user: str, lines: Sequence[str]) -> Sample:
        """The sample of one entry. A user the users file does not have is a problem of this entry, said in `error`: the chat never starts for them, and the
        other entries still run."""
        try:
            return Sample(kind, spec.id, spec, self.users.get(user), list(lines))
        except ConfigError as exc:
            nobody = UserContext(user, user, "brand_manager", (), False, "unknown")
            return Sample(kind, spec.id, spec, nobody, list(lines), error=exc.message + (f" Fix: {exc.fix}" if exc.fix else ""))

    def _play(
        self, sample: Sample, name: str, *, setup: Sequence[str] = (), chaos: ChaosSpec | None = None, chaos_engine: Chaos | None = None,
        before_turn: BeforeTurn | None = None, after_turn: Callable[[int, TurnEvidence, Facts], None] | None = None, restart_after: Sequence[int] = (),
    ) -> None:
        if sample.error:
            return
        if (self._out / name).exists():  # a conversation's folder is reused (that is how `play_lines` restarts the app), so a second sample of the same id would resume the first
            sample.error = f"the folder of '{name}' already holds a conversation: an id is played once for each run (use `play_lines` to restart an app on purpose)"
            return
        started = time.perf_counter()

        def after(index: int, ev: TurnEvidence, facts: Facts) -> None:
            self._say(f"      {ev.outcome} in {ev.seconds:.0f} s")
            if after_turn is not None:
                after_turn(index, ev, facts)

        try:
            sample.played = self._runner.play(
                name, sample.user.user_id, sample.lines, setup=setup, chaos=chaos, chaos_engine=chaos_engine, before_turn=before_turn, after_turn=after,
                restart_after=restart_after,
            )
            sample.error = sample.played.error  # a hook failed part-way: the turns before it are kept and judged, and the invariant of no crash fails
        except ConfigError as exc:  # the chat would not have started: the sample is a failure to report, and the others still run
            sample.error = exc.message + (f" Fix: {exc.fix}" if exc.fix else "")
        except Exception as exc:  # noqa: BLE001 - one broken sample must not stop the rest
            sample.error = f"the harness crashed: {crash_summary(exc)}"  # class and call sites, never the exception text
            sample.crashed = True
        sample.seconds = time.perf_counter() - started

    # the reference ------------------------------------------------------------------------------------------------

    def reference(self, turn: GoldenTurn, user: UserContext) -> TurnReference:
        """The right answer of `turn` for `user`. A problem is in the result's `error`, not raised: it is a finding about the case."""
        ref = TurnReference()
        if turn.reference_sql is None and turn.hidden is None:
            return ref
        try:
            if turn.reference_sql is not None:
                frame = self._frame(turn.reference_sql, user)
                ref.rows = len(frame)
                if turn.compare is not None:
                    ref.values = self._values(frame, turn.compare.column, turn.compare.top_n)
                    for other in turn.exclude_figures_of:
                        ref.others[other] = self._values(self._frame(turn.reference_sql, self.users.get(other)), turn.compare.column, turn.compare.top_n)
                if turn.hidden is not None and turn.hidden.column in frame.columns:
                    ref.visible = self._strings(frame[turn.hidden.column])
            if turn.hidden is not None:
                ref.hidden = self._strings(self._column(self._frame(turn.hidden.sql, user), turn.hidden.column))
        except AgentError as exc:
            ref.error = f"{exc.kind.value}{' ' + exc.rule_id if exc.rule_id else ''}: {exc.detail or exc.user_message}"
        except NoColumn as exc:
            ref.error = str(exc)  # our own words: the column the case names and the ones the query returned
        except Exception as exc:  # noqa: BLE001 - a broken reference is a finding about the case, not a crash of the run
            ref.error = f"the reference query failed: {crash_summary(exc)}"
        return ref

    def _frame(self, sql: str, user: UserContext) -> pd.DataFrame:
        sql = self._runner.macros.expand_sql(sql, user.brands)
        return self._query.run(self._guard.check(sql, user)).df

    def _values(self, frame: pd.DataFrame, column: str, top_n: int) -> list[float]:
        values = pd.to_numeric(self._column(frame, column), errors="coerce").dropna()
        return [float(value) for value in values.head(top_n).tolist()]

    @staticmethod
    def _strings(values: pd.Series) -> list[str]:
        return [str(v) for v in values.tolist() if v is not None and not pd.isna(v)]

    @staticmethod
    def _column(frame: pd.DataFrame, column: str) -> pd.Series:
        if column not in frame.columns:
            raise NoColumn(f"the reference has no column {column!r}; it returns {', '.join(map(str, frame.columns))}")
        return frame[column]
