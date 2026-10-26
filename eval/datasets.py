"""The three data files of the evaluation and their loaders (plan 09).

| File | One entry is | Run by |
|---|---|---|
| `datasets/golden_cases.yaml` | a conversation with the reference SQL that says what the right figures are | `run_golden` |
| `datasets/attacks.yaml` | an attempt to get personal data, another brand's data or an unconfirmed delete | `run_attacks` |
| `datasets/chaos_scenarios.yaml` | a run of turns under injected faults | `run_chaos` |
| `datasets/trajectories.yaml` | a scripted conversation that expects a path through the graph and a state the memory keeps | `run_trajectory` |

A problem in a file is returned, not raised, so one bad entry does not hide the others (same rule as `scenarios/`). The reference SQL is written in
GoogleSQL with the four tables by their bare names and may use the macros of `src/scenarios/truth.py` (`{brands}`, `{latest_date}`,
`{last_month_start}`, `{this_month_start}`).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from eval.trajectory import is_token
from src.errors import ConfigError
from src.scenarios.schema import Outcome
from src.yaml_utils import parse_yaml, read_text, validation_message

DATASETS = Path(__file__).resolve().parent / "datasets"
GOLDEN_FILE, ATTACKS_FILE, CHAOS_FILE, TRAJECTORY_FILE = "golden_cases.yaml", "attacks.yaml", "chaos_scenarios.yaml", "trajectories.yaml"

ID = r"^[a-z0-9][a-z0-9-]{1,60}$"
TurnKind = Literal["simple", "why", "report", "tool"]  # "tool": a save, list, delete or undo, which has no SQL (the metrics row says so)
# What the answer must carry, by name. The code writes the first two around every answer (the envelope); the others are what a case says it must show.
MustState = Literal["how_i_read", "scope_line", "action_items", "small_group_notice", "row_cap_note", "no_data", "data_ends"]
Forbid = Literal["pii", "other_brands"]
Requirement = Literal["sample_data", "golden"]
Setup = Literal["seed_reports"]
Intent = Literal[
    "data_question", "why_question", "report_request", "schema_question", "out_of_scope", "save_report", "list_reports", "delete_reports", "undo_delete",
]
Category = Literal[
    "direct_pii", "indirect_pii", "injection_question", "injection_data", "brand_scope", "role_spoofing", "confirmation_spoofing", "destructive_scope",
    "cost_loop", "small_group", "off_topic",
]
DEFAULT_OUTCOMES: tuple[str, ...] = ("answered", "partial")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# golden cases ----------------------------------------------------------------------------------------------------------------------

class Compare(_Model):
    """The key figures: the first `top_n` values of `column` in the reference result (its ORDER BY decides which). Each must be stated in the
    answer, within `rel_tolerance`. Numbers are compared, never sentences (decision 3.13)."""

    column: str
    top_n: int = Field(5, ge=1, le=50)
    rel_tolerance: float = Field(0.01, gt=0, le=0.5)


class Hidden(_Model):
    """Values the answer must not name (the groups the Result Guard hides): the first column named `column` of this SQL."""

    sql: str
    column: str


class Budget(_Model):
    """A limit tighter than the configured one for the turn kind. The seconds of the configuration are used when it says none: they differ per
    model (a Gemini turn and a 30B model on a laptop), so one file serves both."""

    calls: int | None = Field(None, ge=1)
    seconds: float | None = Field(None, gt=0)


class GoldenTurn(_Model):
    say: str = Field(min_length=1)
    turn_kind: TurnKind | None = None  # how the app must classify it; None: not checked
    outcome: list[Outcome] = list(DEFAULT_OUTCOMES)  # the outcomes that are right for this turn
    reference_sql: str | None = None  # written by us in the analyst role (Q19); it runs through the SAME SQL Guard as the user, so scope matches by construction
    reference_rows: int | None = Field(None, ge=0)  # the reference must return exactly this many rows (a case that depends on "no data" says 0)
    compare: Compare | None = None
    must_state: list[MustState] = []
    says: list[str] = []  # all of these, any case
    says_any: list[str] = []  # at least one of these
    not_says: list[str] = []
    hidden: Hidden | None = None
    forbid: list[Forbid] = []  # invariants this turn must have decided and passed (a check that was skipped does not count)
    exclude_figures_of: list[str] = []  # the same reference run as these users: their figures must not be in this answer, and must differ from this user's
    saved_reports: int | None = Field(None, ge=0)  # the user's saved reports that are not deleted, after the turn
    max_queries: int | None = Field(None, ge=0)  # real queries sent to the data
    budget: Budget | None = None
    allow_mentions: list[str] = []  # brands of other users that this turn's question names, so the answer may repeat them

    @model_validator(mode="after")
    def _reference_goes_with_what_uses_it(self) -> "GoldenTurn":
        if (self.compare or self.exclude_figures_of or self.reference_rows is not None) and not self.reference_sql:
            raise ValueError("`compare`, `exclude_figures_of` and `reference_rows` need a `reference_sql`")
        if self.exclude_figures_of and not self.compare:
            raise ValueError("`exclude_figures_of` needs `compare`: those are the figures it excludes")
        if not self.outcome:
            raise ValueError("`outcome` lists at least one outcome")
        return self


# The fields a one-turn case may write at its top level instead of under `turns`.
_FLAT_FIELDS = tuple(name for name in GoldenTurn.model_fields if name != "say")


class GoldenCase(_Model):
    id: str = Field(pattern=ID)
    title: str = Field(min_length=3)
    story: str = ""  # the user story or requirement it covers: "US1", "F15"
    notes: str = ""  # why the case was changed after a run (a changed case is a new `cases_hash`); read by people, not scored
    user: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    requires: list[Requirement] = []
    setup: list[Setup] = []
    turns: list[GoldenTurn] = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _flat_one_turn_case(cls, data: Any) -> Any:
        """`question:` and the turn's fields at the top: a case of one turn, as the plan writes it."""
        if isinstance(data, dict) and "question" in data and "turns" not in data:
            data = dict(data)
            turn = {"say": data.pop("question")}
            for name in _FLAT_FIELDS:
                if name in data:
                    turn[name] = data.pop(name)
            data["turns"] = [turn]
        return data


# trajectories ------------------------------------------------------------------------------------------------------------------------

class PathExpect(_Model):
    """The path one turn takes, read from the spans of its trace (`eval/trajectory.py`). `through`: these spans in this order, with others allowed between
    them. `never`: none of these. A name is a graph node (`planner`), a span (`bq.query`, `guard.output`, `tool.saved_reports.delete`) or a model call as
    `llm.<step>` (`llm.sql_write`); a name no trace can hold is refused when the file loads, so a typo cannot pass by never matching."""

    through: list[str] = []
    never: list[str] = []

    @field_validator("through", "never")
    @classmethod
    def _names_a_span(cls, names: list[str]) -> list[str]:
        unknown = [name for name in names if not is_token(name)]
        if unknown:
            raise ValueError(f"not a span a trace can hold: {', '.join(unknown)}")
        return names


class MemoryExpect(_Model):
    """What this turn must show about the conversation's memory. Turn numbers start at 1 and point at an earlier turn.

    - `standalone_has`: the question as the Input Guard rewrote it (the state's `standalone_question`) holds all of these texts.
    - `standalone_has_any`: and at least one of these.
    - `recalls`: this answer states every key figure that turn N's answer stated (turn N needs `compare`).
    - `same_labels_as`: this answer names exactly the `Customer #NNNNN` labels turn N's did.
    - `saved_title_of`: the report this turn saved has the title of turn N's answer, so "save that" saved what was shown.
    """

    standalone_has: list[str] = []
    standalone_has_any: list[str] = []
    recalls: int | None = Field(None, ge=1)
    same_labels_as: int | None = Field(None, ge=1)
    saved_title_of: int | None = Field(None, ge=1)


# Fields of a golden turn that score the WORDING of an answer. A trajectory case judges the path and the memory, so it takes none of them: a field that is
# accepted and never looked at would let a case believe it checks something.
_WORDING_FIELDS = ("must_state", "says", "says_any", "not_says", "hidden", "forbid", "exclude_figures_of")


class TrajectoryTurn(GoldenTurn):
    """A golden turn, narrowed: `outcome`, `turn_kind`, `saved_reports`, `max_queries` and `budget` work as in a golden case; `reference_sql` with `compare` gives
    the figures a later turn recalls (they are not scored here, as they are in `golden_cases.yaml`)."""

    path: PathExpect | None = None
    memory: MemoryExpect | None = None

    @model_validator(mode="after")
    def _no_wording_checks(self) -> "TrajectoryTurn":
        given = [name for name in _WORDING_FIELDS if getattr(self, name)]
        if given:
            raise ValueError(f"{', '.join(given)}: a trajectory case does not score wording; put it in a golden case")
        return self


class TrajectoryCase(GoldenCase):
    """A scripted conversation for the path and the memory. `restart_after`: the turns (1-based) after which the app is closed and started again on the same
    conversation, so what carries over has to come back from the checkpoint on disk."""

    turns: list[TrajectoryTurn] = Field(min_length=1)
    restart_after: list[int] = []

    @model_validator(mode="after")
    def _points_at_turns_that_exist(self) -> "TrajectoryCase":
        count = len(self.turns)
        for number in self.restart_after:
            if not 1 <= number < count:
                raise ValueError(f"restart_after {number}: the app can only be started again between two of the {count} turn(s)")
        for number, turn in enumerate(self.turns, 1):
            memory = turn.memory
            for field_name in ("recalls", "same_labels_as", "saved_title_of"):
                target = getattr(memory, field_name, None) if memory else None
                if target is None:
                    continue
                if not 1 <= target < number:
                    raise ValueError(f"turn {number}: {field_name} {target} must be an earlier turn")
                if field_name == "recalls" and self.turns[target - 1].compare is None:
                    raise ValueError(f"turn {number}: recalls turn {target}, which has no `compare` to give the figures")
            if memory and memory.saved_title_of is not None and turn.saved_reports is None:
                raise ValueError(f"turn {number}: saved_title_of needs `saved_reports`, so the turn is known to save")
        return self


# attacks -----------------------------------------------------------------------------------------------------------------------------

class ModelScript(_Model):
    """OFFLINE ONLY: what a model that was talked into it replies, so the test can ask whether the code still holds. A live run ignores it and
    asks the real model. `sql` is the SQL writer's reply and `body` the Composer's draft."""

    intent: Intent = "data_question"
    in_scope: bool = True
    injection_suspected: bool = False
    selector: dict[str, Any] | None = None
    sql: str | None = None
    body: str | None = None


class Attack(_Model):
    id: str = Field(pattern=ID)
    category: Category
    user: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    prompt: str | list[str]  # a list is a conversation: the last line is the attack, the lines before prepare it
    expect: Literal["refused", "answered_safely"]
    note: str = ""
    setup: list[Setup] = []
    allow_mentions: list[str] = []
    model: ModelScript | list[ModelScript] = ModelScript()

    @property
    def lines(self) -> list[str]:
        return [self.prompt] if isinstance(self.prompt, str) else list(self.prompt)

    def script(self, index: int) -> ModelScript:
        """What the hijacked model does on line `index` (0-based). One script serves every line; a list gives one per line."""
        if isinstance(self.model, list):
            return self.model[min(index, len(self.model) - 1)]
        return self.model

    @model_validator(mode="after")
    def _scripts_match_the_lines(self) -> "Attack":
        if isinstance(self.model, list) and len(self.model) != len(self.lines):
            raise ValueError("a list of `model` scripts has one script for each line of `prompt`")
        return self


# chaos ------------------------------------------------------------------------------------------------------------------------------

class Fault(_Model):
    """One scripted fault for the offline run (`Chaos.scripted()`): `kind` model or bigquery, `fault` as named in `src/llm/chaos.py`."""

    kind: Literal["llm", "bq", "sql"]
    fault: str
    times: int = Field(1, ge=1)
    step: str | None = None  # an llm fault can aim at one step


class ChaosExpect(_Model):
    outcomes: list[Outcome] | None = None  # the outcomes a turn may end in
    breaker_opened: bool | None = None  # at least one model call opened a circuit breaker
    all_answered: bool | None = None  # every turn ends answered or partial


class ChaosScenario(_Model):
    id: str = Field(pattern=ID)
    title: str = Field(min_length=3)
    description: str = ""
    user: str = Field("alice", pattern=r"^[A-Za-z0-9_.-]+$")
    chaos: dict[str, Any] | None = None  # {level: harsh, seed: 11}: the random profile; absent for a scripted scenario
    faults: list[Fault] = []
    questions: list[str] = Field(min_length=1)
    turns: int = Field(1, ge=1, le=100)  # the questions are asked in turn, over and over, until there are this many turns
    expect: ChaosExpect = ChaosExpect()

    @field_validator("chaos")
    @classmethod
    def _chaos_is_level_and_seed(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return value
        extra = set(value) - {"level", "seed"}
        if extra or value.get("level") not in ("mild", "harsh"):
            raise ValueError("`chaos` is {level: mild|harsh, seed: N}")
        if "seed" in value and (isinstance(value["seed"], bool) or not isinstance(value["seed"], int)):
            raise ValueError("the chaos `seed` is a whole number")
        return value

    @model_validator(mode="after")
    def _one_kind_of_chaos(self) -> "ChaosScenario":
        if (self.chaos is None) == (not self.faults):
            raise ValueError("a scenario has either a random `chaos` profile or a list of scripted `faults`, not both and not neither")
        return self

    def lines(self) -> list[str]:
        return [self.questions[n % len(self.questions)] for n in range(self.turns)]


# loading -----------------------------------------------------------------------------------------------------------------------------

class Loaded(BaseModel):
    """What came out of one entry of a file: the model, or why it is not one."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    index: int
    item: Any = None
    error: str = ""


def _load(path: Path, model: type[BaseModel], what: str) -> list[Loaded]:
    try:
        data = parse_yaml(read_text(path, what), path)
    except ConfigError as exc:
        return [Loaded(index=0, error=exc.message)]
    if not isinstance(data, list):
        return [Loaded(index=0, error=f"{path.name}: the file must be a list of entries")]
    loaded: list[Loaded] = []
    seen: set[str] = set()
    for index, entry in enumerate(data, 1):
        try:
            item = model.model_validate(entry)
        except ValidationError as exc:
            loaded.append(Loaded(index=index, error=validation_message(path, exc)))
            continue
        if item.id in seen:  # type: ignore[attr-defined]
            loaded.append(Loaded(index=index, error=f"{path.name}: id '{item.id}' is used twice"))  # type: ignore[attr-defined]
            continue
        seen.add(item.id)  # type: ignore[attr-defined]
        loaded.append(Loaded(index=index, item=item))
    return loaded


def _items(loaded: list[Loaded]) -> list[Any]:
    """The entries, or the first problem as a `ConfigError`: a run on a broken file would score less than it claims to."""
    for entry in loaded:
        if entry.error:
            raise ConfigError(entry.error)
    return [entry.item for entry in loaded]


def load_golden(path: Path | None = None) -> list[GoldenCase]:
    return _items(_load(path or DATASETS / GOLDEN_FILE, GoldenCase, "golden cases file"))


def load_attacks(path: Path | None = None) -> list[Attack]:
    return _items(_load(path or DATASETS / ATTACKS_FILE, Attack, "attacks file"))


def load_chaos(path: Path | None = None) -> list[ChaosScenario]:
    return _items(_load(path or DATASETS / CHAOS_FILE, ChaosScenario, "chaos scenarios file"))


def load_trajectories(path: Path | None = None) -> list[TrajectoryCase]:
    return _items(_load(path or DATASETS / TRAJECTORY_FILE, TrajectoryCase, "trajectory cases file"))


def select(items: list[Any], ids: list[str]) -> list[Any]:
    """The entries named in `ids` (all when none is named). An unknown ID is an error: a typo must not run nothing and pass."""
    if not ids:
        return items
    known = {item.id for item in items}
    unknown = [name for name in ids if name not in known]
    if unknown:
        raise ConfigError(f"No entry has the id {', '.join(repr(name) for name in unknown)}.", fix="Known ids: " + ", ".join(sorted(known)))
    return [item for item in items if item.id in ids]
