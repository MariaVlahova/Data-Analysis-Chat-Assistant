"""The glossary (plan 10): business words with one agreed meaning, sent to the model with the question that uses them.

`glossary.yaml` is a list of entries. A question that contains a term or one of its aliases (whole words, any case) gets that
entry's definition in the prompts, inside `<untrusted_data>` like every other piece of retrieved text. An entry marked `locked: true`
is already enforced in code (`agent/prompts/locked_rules.py`); it stays in the file so the owner reads every definition in one
place (Q6), and is not sent a second time.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from src.errors import ConfigError
from src.yaml_utils import parse_yaml, read_text

MAX_DEFINITIONS = 5  # per question: a prompt full of definitions teaches nothing


class GlossaryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    term: str = Field(min_length=2, max_length=60)
    aliases: list[str] = []
    definition: str = Field(min_length=10, max_length=500)
    locked: bool = False

    @field_validator("aliases")
    @classmethod
    def _no_blank_alias(cls, aliases: list[str]) -> list[str]:
        if any(len(alias.strip()) < 2 for alias in aliases):
            raise ValueError("an alias must have at least two characters")
        return aliases

    def words(self) -> list[str]:
        return [self.term, *self.aliases]


def _pattern(words: list[str]) -> re.Pattern[str]:
    alternatives = "|".join(re.escape(" ".join(word.lower().split())).replace(r"\ ", r"\s+") for word in words)
    return re.compile(rf"(?<![a-z0-9])(?:{alternatives})(?![a-z0-9])")


@dataclass(frozen=True)
class Glossary:
    entries: tuple[GlossaryEntry, ...] = ()

    def lookup(self, question: str) -> list[str]:
        """`term: definition` for each entry the question uses, in file order, locked entries left out."""
        text = " ".join(question.lower().split())
        found = [
            f"{entry.term}: {entry.definition}" for entry in self.entries
            if not entry.locked and _pattern(entry.words()).search(text)
        ]
        return found[:MAX_DEFINITIONS]


def load_glossary(path: Path) -> tuple[Glossary, list[str]]:
    """The glossary and the problems found. An entry that is not valid is skipped, and the rest are kept: the glossary is
    optional, so a mistake in it never stops the chat. A missing file is an empty glossary."""
    if not path.is_file():
        return Glossary(), [f"The glossary file was not found: {path.name}"]
    try:
        data = parse_yaml(read_text(path, "glossary"), path)
    except ConfigError as exc:
        return Glossary(), [exc.message]
    if not isinstance(data, list):
        return Glossary(), [f"{path.name}: the file must be a list of entries (term, aliases, definition)"]
    entries: list[GlossaryEntry] = []
    problems: list[str] = []
    seen: set[str] = set()
    for number, item in enumerate(data, 1):
        try:
            entry = GlossaryEntry.model_validate(item)
        except ValidationError as exc:
            problems.append(f"{path.name} entry {number}: " + "; ".join(error["msg"] for error in exc.errors(include_url=False)))
            continue
        key = entry.term.casefold()
        if key in seen:
            problems.append(f"{path.name} entry {number}: the term '{entry.term}' is defined twice, so the second one is skipped")
            continue
        seen.add(key)
        entries.append(entry)
    return Glossary(tuple(entries)), problems
