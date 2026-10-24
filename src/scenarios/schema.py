"""The scenario file format (`scenarios/*.yaml`, one scenario per file) and its loader."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.errors import ConfigError
from src.yaml_utils import parse_yaml, read_text, validation_message

OUTCOMES = ("answered", "partial", "refused", "failed", "action_done", "action_cancelled", "awaiting_confirmation")
Outcome = Literal["answered", "partial", "refused", "failed", "action_done", "action_cancelled", "awaiting_confirmation"]
Requirement = Literal["sample_data", "golden"]
Setup = Literal["seed_reports"]

SCENARIO_GLOB = "*.yaml"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NumberCheck(_Model):
    """A figure from the data. `sql` is GoogleSQL with the tables by their bare names; it runs on the data directly (never through the app) and
    returns one number in its first cell. The answer must state it, within `rel_tol`, or with `present: false` must not state it. The macros
    (`{brands}`, `{brand}`, `{other_brand}`, `{latest_date}`, ...) are listed in `truth.py`."""

    sql: str
    label: str = ""
    rel_tol: float = Field(0.01, gt=0, le=0.5)
    present: bool = True
    each_brand: bool = False  # make the check once for each of the user's brands; `{brand}` in the SQL is that brand


class TraceCheck(_Model):
    golden_trios_include: list[str] = []
    golden_trios_exclude: list[str] = []
    llm_calls_max: int | None = Field(None, ge=1)
    queries_min: int | None = Field(None, ge=0)  # real queries sent to the data (`bq.query` spans)
    queries_max: int | None = Field(None, ge=0)
    guard_blocks: dict[str, int] = {}  # at least this many blocks by guard: {sql: 1}


class Expect(_Model):
    outcome: Outcome | list[Outcome] | None = None
    contains: list[str] = []  # all of these, any case
    contains_any: list[str] = []  # at least one of these, any case
    not_contains: list[str] = []
    matches: list[str] = []  # regular expressions, all must match (any case)
    numbers: list[NumberCheck] = []
    not_contains_sql: list[str] = []  # SQL on the data returning strings (first column) that the answer must not contain
    trace: TraceCheck | None = None
    saved_reports: int | None = Field(None, ge=0)  # the user's saved reports that are not deleted, after the turn
    max_seconds: float | None = Field(None, gt=0)
    allow_mentions: list[str] = []  # brands of other users that this turn's question names, so the answer may repeat them

    def is_empty(self) -> bool:
        return not any(getattr(self, name) for name in type(self).model_fields)


class Turn(_Model):
    say: str = Field(min_length=1)
    expect: Expect = Expect()
    advisory: Expect = Expect()


class ChaosSpec(_Model):
    level: Literal["mild", "harsh"]
    seed: int | None = None


class Scenario(_Model):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,60}$")
    title: str = Field(min_length=3)
    description: str = ""
    user: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    tags: list[str] = []
    # "sample_data": uses what only the sample data has (its brands, its planted effects), so it runs only with `bigquery.backend: local`.
    # "golden": needs `golden.enabled`. A scenario that lists neither runs on the sample data and on BigQuery alike.
    requires: list[Requirement] = []
    setup: list[Setup] = []
    chaos: ChaosSpec | None = None
    turns: list[Turn] = Field(min_length=1)


class LoadedScenario(_Model):
    path: Path
    scenario: Scenario | None = None
    error: str = ""


def load_scenario(path: Path) -> LoadedScenario:
    """Read one scenario file. A problem is returned, not raised: one bad file must not stop the others."""
    try:
        data = parse_yaml(read_text(path, "scenario file"), path)
    except ConfigError as exc:
        return LoadedScenario(path=path, error=exc.message)
    if not isinstance(data, dict):
        return LoadedScenario(path=path, error=f"{path.name}: the file must be one mapping with the fields of a scenario")
    try:
        return LoadedScenario(path=path, scenario=Scenario.model_validate(data))
    except ValidationError as exc:
        return LoadedScenario(path=path, error=validation_message(path, exc))


def load_scenarios(location: Path) -> list[LoadedScenario]:
    """The scenarios of a folder (or one file), in file-name order. A repeated `id` fails the later file."""
    paths = [location] if location.is_file() else sorted(location.glob(SCENARIO_GLOB)) if location.is_dir() else []
    loaded: list[LoadedScenario] = []
    seen: dict[str, Path] = {}
    for path in paths:
        item = load_scenario(path)
        if item.scenario is not None:
            first = seen.get(item.scenario.id)
            if first is not None:
                item = LoadedScenario(path=path, error=f"{path.name}: id '{item.scenario.id}' is already used by {first.name}")
            else:
                seen[item.scenario.id] = path
        loaded.append(item)
    return loaded
