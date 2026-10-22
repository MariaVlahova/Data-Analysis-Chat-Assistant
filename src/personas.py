"""The static company persona (decision 3.9).

One persona file lives in `personas/<name>.yaml`. It is not managed: there is no editor UI, approval
or rollback (R8 is out of scope). The file is re-read when it changes on disk, so a hand edit shows
up in the next answer. The persona text goes only to the Composer prompt. Locked rules always win
over it (plan 07).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from src.errors import ConfigError
from src.yaml_utils import decode_utf8, parse_yaml, validation_message

PERSONA_DIR_NAME = "personas"
_PERSONA_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")  # same rule as PersonaFile.id


class PersonaFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]*$")]
    version: int = Field(ge=1)
    tone: str = Field(min_length=1, max_length=2000)
    sections: list[Annotated[str, StringConstraints(min_length=1)]] = []
    max_words: int = Field(ge=1)


@dataclass(frozen=True)
class Persona:
    file: PersonaFile
    label: str  # `id@vN+sha8`, goes into the trace (F41)

    def prompt_block(self) -> str:
        lines = [f"Tone: {self.file.tone}", f"Keep answers under {self.file.max_words} words."]
        if self.file.sections:
            lines.append("Preferred sections: " + ", ".join(self.file.sections) + ".")
        return "\n".join(lines)


class PersonaStore:
    """Reads persona files, caching by modification time and size."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self._cache: dict[str, tuple[tuple[int, int], Persona]] = {}

    def names(self) -> list[str]:
        return sorted(p.stem for p in self.directory.glob("*.yaml"))

    def get(self, name: str) -> Persona:
        def unknown() -> ConfigError:
            return ConfigError(
                f"Unknown persona '{name}'. Available: {', '.join(self.names()) or 'none'}.",
                fix=f"Add {self.directory / (name + '.yaml')} or pick an existing persona.",
            )

        if not _PERSONA_NAME.match(name):
            raise unknown()  # a name like '../users' must never reach the filesystem
        path = self.directory / f"{name}.yaml"
        try:
            stat = path.stat()
        except OSError:
            raise unknown() from None
        stamp = (stat.st_mtime_ns, stat.st_size)
        cached = self._cache.get(name)
        if cached and cached[0] == stamp:
            return cached[1]

        try:
            raw = path.read_bytes()
        except OSError as exc:  # locked by an editor, removed since the stat, or a folder
            raise ConfigError(
                f"The persona file could not be read: {path} ({exc.strerror or exc.__class__.__name__})",
                fix="Close the editor that has it open, or check that the file exists.",
            ) from None
        data = parse_yaml(decode_utf8(raw, path, "persona file"), path)
        try:
            file = PersonaFile.model_validate(data)
        except ValidationError as exc:
            raise ConfigError(validation_message(path, exc), fix=f"Fix the persona file {path}.") from None
        if file.id != name:
            raise ConfigError(f"{path.name}: id '{file.id}' must match the file name '{name}'.")
        persona = Persona(file=file, label=f"{file.id}@v{file.version}+{hashlib.sha256(raw).hexdigest()[:8]}")
        self._cache[name] = (stamp, persona)
        return persona
