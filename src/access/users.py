"""Users and brand scope from `users.yaml` (decision 3.4, S14).

Prototype stand-in for the signed token that production would send (A4, Q39).
The one rule that matters: an empty brand list means NO access, never "all brands".
"""
from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, field_validator, model_validator

from src.config import Settings
from src.errors import AgentError, ConfigError, ErrorKind
from src.yaml_utils import parse_yaml, validation_message

# Roles beyond brand_manager and ceo are accepted but unused in the prototype (Q42 default).
Role = Literal["brand_manager", "ceo", "analyst", "admin"]
ScopeMode = Literal["all", "brands", "none"]

USERS_FILE_NAME = "users.yaml"
SALT_FILE_NAME = "user_hash_salt"
PLACEHOLDER_BRAND = re.compile(r"^<verified brand")  # the names shipped in users.yaml until real ones are chosen (plan 03)

class UserEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    role: Role
    brands: list[str] = []
    all_brands: bool = False

    @field_validator("brands")
    @classmethod
    def _no_blank_brands(cls, brands: list[str]) -> list[str]:
        if any(not b.strip() for b in brands):
            raise ValueError("a brand name must not be blank")
        return brands  # compared exactly later: case and spacing count (S1)

    @model_validator(mode="after")
    def _all_brands_only_for_ceo(self) -> "UserEntry":
        if self.all_brands and self.role != "ceo":
            raise ValueError("all_brands: true is only allowed for role: ceo")
        return self


UserId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]+$")]


class UsersFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    users: dict[UserId, UserEntry] = Field(min_length=1)


@dataclass(frozen=True)
class UserContext:
    user_id: str
    display_name: str
    role: str
    brands: tuple[str, ...]
    all_brands: bool
    user_hash: str = ""  # sha256(salt + user_id)[:12]; used in traces, metrics, audit and BQ labels

    @property
    def scope_mode(self) -> ScopeMode:
        """`all` only for the CEO with the explicit flag. An empty list is `none`, never `all` (S14)."""
        if self.all_brands and self.role == "ceo":
            return "all"
        if self.brands:
            return "brands"
        return "none"


def require_access(user: UserContext) -> None:
    """Block a user whose scope is `none` (S14). Called by the CLI and again by the graph."""
    if user.scope_mode == "none":
        raise AgentError(
            ErrorKind.NO_ACCESS,
            detail=f"scope_mode=none for user_hash={user.user_hash}",
        )


def make_user_hash(salt: str, user_id: str) -> str:
    return hashlib.sha256((salt + user_id).encode("utf-8")).hexdigest()[:12]


class UserDirectory:
    def __init__(self, users: dict[str, UserContext]) -> None:
        self._users = dict(users)

    @property
    def names(self) -> list[str]:
        return sorted(self._users)

    def get(self, user_id: str) -> UserContext:
        """Look a user up by login name. Case is ignored if that is unambiguous."""
        key = user_id.strip()
        if key in self._users:
            return self._users[key]
        folded = [name for name in self._users if name.casefold() == key.casefold()]
        if len(folded) == 1:
            return self._users[folded[0]]
        raise ConfigError(
            f"Unknown user '{user_id}'. Known users: {', '.join(self.names)}.",
            fix="Use --user with one of those names, or add the user to users.yaml.",
        )


def resolve_salt(settings: Settings) -> str:
    """Salt for `user_hash`: the config value, else a file generated once in the data folder."""
    if settings.app.user_hash_salt:
        return settings.app.user_hash_salt
    path = settings.data_path / SALT_FILE_NAME
    try:
        if path.is_file():
            salt = path.read_text(encoding="utf-8").strip()
            if salt:
                return salt
        path.parent.mkdir(parents=True, exist_ok=True)
        salt = secrets.token_hex(16)
        path.write_text(salt, encoding="utf-8")
        return salt
    except OSError as exc:
        raise ConfigError(
            f"The user-hash salt file could not be read or written: {path} ({exc.strerror or exc})",
            fix="Check that the data folder is writable, or set app.user_hash_salt in config.yaml.",
        ) from None


def load_user_directory(path: Path, salt: str) -> UserDirectory:
    """Read and validate `users.yaml`.

    A missing or unreadable file is ACCESS_STORE_DOWN: block (NFR15). A mistake inside the file
    (bad YAML, unknown key, all_brands on a non-CEO) is a ConfigError that names file, line and key.
    """
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        # Fail closed: a list we cannot read is not a list we can trust (NFR15).
        reason = "not valid UTF-8 text" if isinstance(exc, UnicodeDecodeError) else (exc.strerror or type(exc).__name__)
        raise AgentError(ErrorKind.ACCESS_STORE_DOWN, detail=f"{path.name} could not be read: {reason}") from None
    data = parse_yaml(text, path)
    try:
        parsed = UsersFile.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(validation_message(path, exc), fix=f"Fix the entry in {path}.") from None

    users = {
        user_id: UserContext(
            user_id=user_id,
            display_name=entry.name,
            role=entry.role,
            brands=tuple(entry.brands),
            all_brands=entry.all_brands,
            user_hash=make_user_hash(salt, user_id),
        )
        for user_id, entry in parsed.users.items()
    }
    return UserDirectory(users)


def users_path(settings: Settings) -> Path:
    return settings.resolve(USERS_FILE_NAME)


def load_users(settings: Settings, path: Path | None = None) -> UserDirectory:
    return load_user_directory(path or users_path(settings), resolve_salt(settings))
