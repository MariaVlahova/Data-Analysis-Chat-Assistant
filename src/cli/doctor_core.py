"""The types every `doctor` check shares. Kept apart from `doctor.py` so a check module can import them without
importing `doctor.py` back (no import cycle) and without loading anything heavy."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from src.config import Settings
from src.errors import ConfigError

Status = Literal["ok", "warn", "fail", "skip"]


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str = ""
    fix: str = ""


@dataclass
class DoctorContext:
    settings: Settings | None
    config_error: ConfigError | None = None
    adc_project: str | None = None  # project found in the default credentials, set by the credentials check
    refresh: bool = False  # `doctor --refresh`: read the schema and brand list again instead of using the saved copy
    shared: dict[str, Any] = field(default_factory=dict)  # work one check does for the next (the loaded catalog)


CheckFn = Callable[[DoctorContext], Check]
