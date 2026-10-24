"""The trio (plan 10, Q43): question, SQL, report, with the notes and tags that make it safe to reuse.

One trio per YAML file. `status` moves draft -> approved -> retired and only `approved` trios are searchable. `brands` is `["*"]`
for a trio that holds nothing brand-specific, or the exact brands whose data the trio shows; the retrieval filter works on the whole
trio, so no part of a trio from another brand ever reaches a prompt (S5).

`view(step)` is what one pipeline step may see. The Planner gets the question and the notes, the SQL writer adds the SQL, the
Composer gets an excerpt of the report and nothing else. The prompt builders take only these, so a step cannot be handed more.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from src.errors import ConfigError
from src.yaml_utils import parse_yaml, read_text, validation_message

AGNOSTIC = "*"  # `brands: ["*"]`: the trio is not tied to a brand
REPORT_EXCERPT_CHARS = 600
Step = Literal["planner", "sql", "composer"]
Status = Literal["draft", "approved", "retired"]

TRIO_FILE_GLOB = "*.yaml"


class Trio(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
    version: int = Field(ge=1)  # grows when an analyst edits the trio
    status: Status
    question: str = Field(min_length=10, max_length=300)
    interpretation_notes: str = Field(min_length=1, max_length=600)
    sql: str = Field(min_length=10, max_length=4000)
    report: str = Field(min_length=1, max_length=4000)
    tables: list[str] = Field(min_length=1)  # `validate` checks them against the four approved tables
    brands: list[str] = Field(min_length=1)
    reviewed_by: str = Field(min_length=1)
    created_at: date

    @field_validator("tables")
    @classmethod
    def _plain_names(cls, tables: list[str]) -> list[str]:
        if any(not name.strip() for name in tables):
            raise ValueError("a table name must not be blank")
        return tables

    @field_validator("brands")
    @classmethod
    def _brand_list(cls, brands: list[str]) -> list[str]:
        if any(not brand.strip() for brand in brands):
            raise ValueError("a brand name must not be blank")
        if len(set(brands)) != len(brands):
            raise ValueError("a brand is listed twice")
        return brands  # compared exactly later: case and spacing count (S1)

    @model_validator(mode="after")
    def _agnostic_or_named(self) -> "Trio":
        if AGNOSTIC in self.brands and len(self.brands) != 1:
            raise ValueError(f'"{AGNOSTIC}" means any brand, so it cannot be mixed with brand names')
        return self

    @property
    def brand_agnostic(self) -> bool:
        return self.brands == [AGNOSTIC]

    @property
    def searchable(self) -> bool:
        return self.status == "approved"

    def view(self, step: Step) -> dict[str, str]:
        """Only what `step` may see. A new field of the trio is in no view until it is added here on purpose."""
        if step == "planner":
            return {"id": self.id, "question": self.question, "notes": self.interpretation_notes}
        if step == "sql":
            return {"id": self.id, "question": self.question, "notes": self.interpretation_notes, "sql": self.sql.strip()}
        if step == "composer":
            return {"id": self.id, "report": self.report.strip()[:REPORT_EXCERPT_CHARS]}
        raise ValueError(f"unknown step {step!r}")


@dataclass(frozen=True)
class LoadedFile:
    """One file of the seed folder: the trio, or why it could not be read."""

    path: Path
    trio: Trio | None
    error: str = ""


def load_trio_file(path: Path) -> LoadedFile:
    """Read one trio file. A problem is returned, never raised: one bad file must not stop the others."""
    try:
        data = parse_yaml(read_text(path, "trio file"), path)
    except ConfigError as exc:
        return LoadedFile(path, None, exc.message)
    if not isinstance(data, dict):
        return LoadedFile(path, None, f"{path.name}: the file must be one mapping with the fields of a trio")
    try:
        return LoadedFile(path, Trio.model_validate(data))
    except ValidationError as exc:
        return LoadedFile(path, None, validation_message(path, exc))


def load_trio_files(location: Path) -> list[LoadedFile]:
    """The trios of a folder (or of one file), in file-name order. A repeated `id` fails the later file."""
    paths = [location] if location.is_file() else sorted(location.glob(TRIO_FILE_GLOB)) if location.is_dir() else []
    loaded: list[LoadedFile] = []
    seen: dict[str, Path] = {}
    for path in paths:
        item = load_trio_file(path)
        if item.trio is not None:
            first = seen.get(item.trio.id)
            if first is not None:
                item = LoadedFile(path, None, f"{path.name}: id '{item.trio.id}' is already used by {first.name}")
            else:
                seen[item.trio.id] = path
        loaded.append(item)
    return loaded
