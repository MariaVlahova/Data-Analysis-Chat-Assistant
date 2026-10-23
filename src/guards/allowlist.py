"""The schema allow-list: the tables and columns the agent may use (plan 05, decision 3.4, S7).

`schema_allowlist.yaml` is an allow-list, not a block list. A column that is not listed does not exist for the agent,
so a column that appears upstream later stays invisible until a human classifies it (fail closed). The file is
validated here, and again against the live schema by `SchemaCatalog.with_policy` (plan 03).

`Allowlist` also satisfies plan 03's `ColumnPolicy`: `table_names`, `allowed`, `blocked` and `describe`.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from src.config import Settings
from src.errors import ConfigError
from src.yaml_utils import load_yaml, validation_message

ALLOWLIST_FILE_NAME = "schema_allowlist.yaml"

# The four tables of the dataset. `data_access.catalog.TABLES` is this same tuple. The scope views join them to each
# other, so all four must be listed.
REQUIRED_TABLES = ("orders", "order_items", "products", "users")

# Columns the scope views read to apply the brand filter. They must be allowed, or the views cannot be built.
SCOPE_COLUMNS: dict[str, tuple[str, ...]] = {
    "products": ("id", "brand"),
    "order_items": ("order_id", "product_id", "user_id"),
    "orders": ("order_id",),
    "users": ("id",),
}

Name = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]


def fold(name: str) -> str:
    """The form names are compared in: compatibility-normalized and case-folded, so a full-width or upper-case
    spelling of a blocked name is still that name."""
    return unicodedata.normalize("NFKC", name).casefold()


class _TableEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed: list[Name] = Field(min_length=1)
    quasi_identifiers: list[Name] = []
    blocked: list[Name] = []


class _AllowlistFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset: str = Field(pattern=r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_]+$")
    reviewed_against_live: bool = False
    customer_id_columns: list[str] = Field(min_length=1)
    tables: dict[Name, _TableEntry]
    descriptions: dict[Name, dict[Name, str]] = {}


@dataclass(frozen=True)
class TablePolicy:
    allowed: tuple[str, ...]
    quasi_identifiers: tuple[str, ...] = ()
    blocked: tuple[str, ...] = ()


@dataclass(frozen=True, eq=False)  # the dict fields make a field-wise hash impossible
class Allowlist:
    dataset: str
    tables: dict[str, TablePolicy]  # keyed by the table name as written in the file
    customer_id_columns: tuple[tuple[str, str], ...]  # (table, column)
    descriptions: dict[tuple[str, str], str] = field(default_factory=dict)
    # False until a person has compared the file with the live schema and settled the PII split (Q4, Q11). Nothing in
    # the file can prove that: a column listed under the wrong heading would quietly weaken the customer-row rules.
    reviewed_against_live: bool = False

    # the ColumnPolicy that plan 03's catalog needs ----------------------------------------------------
    def table_names(self) -> list[str]:
        return list(self.tables)

    def allowed(self, table: str) -> list[str]:
        """An unknown table has no allowed columns."""
        policy = self._policy(table)
        return list(policy.allowed) if policy else []

    def blocked(self, table: str) -> list[str]:
        policy = self._policy(table)
        return list(policy.blocked) if policy else []

    def describe(self, table: str, column: str) -> str:
        return self.descriptions.get((fold(table), fold(column)), "")

    # what the SQL Guard needs -------------------------------------------------------------------------
    def quasi_identifiers(self, table: str) -> list[str]:
        policy = self._policy(table)
        return list(policy.quasi_identifiers) if policy else []

    def table_name(self, name: str) -> str | None:
        """The table as written in the file for any spelling of its name, or None when it is not an approved table."""
        return self._by_folded.get(fold(name))

    @cached_property
    def blocked_names(self) -> frozenset[str]:
        """Every blocked column name of every table, folded. The SQL Guard refuses any identifier with one of these."""
        return frozenset(fold(column) for policy in self.tables.values() for column in policy.blocked)

    @cached_property
    def customer_id_set(self) -> frozenset[tuple[str, str]]:
        return frozenset((fold(table), fold(column)) for table, column in self.customer_id_columns)

    @cached_property
    def quasi_identifier_set(self) -> frozenset[tuple[str, str]]:
        return frozenset(
            (fold(table), fold(column)) for table, policy in self.tables.items() for column in policy.quasi_identifiers
        )

    def customer_id_columns_of(self, table: str) -> list[str]:
        return [column for owner, column in self.customer_id_columns if fold(owner) == fold(table)]

    @cached_property
    def _by_folded(self) -> dict[str, str]:
        return {fold(name): name for name in self.tables}

    def _policy(self, table: str) -> TablePolicy | None:
        name = self.table_name(table)
        return self.tables[name] if name else None


def allowlist_path(settings: Settings) -> Path:
    return settings.resolve(ALLOWLIST_FILE_NAME)


def load_allowlist(settings: Settings, path: Path | None = None) -> Allowlist:
    """Read and validate `schema_allowlist.yaml`. Raises `ConfigError` naming the file, the line and the key."""
    path = path or allowlist_path(settings)
    try:
        data = load_yaml(path, "allow-list file")
    except ConfigError as exc:
        raise ConfigError(exc.message, exc.fix or f"Restore {path.name} from the repository: without it no column can be used.") from None
    try:
        parsed = _AllowlistFile.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(validation_message(path, exc), fix=f"Fix the entry in {path}.") from None

    allowlist = Allowlist(
        dataset=parsed.dataset,
        tables={
            table: TablePolicy(tuple(entry.allowed), tuple(entry.quasi_identifiers), tuple(entry.blocked))
            for table, entry in parsed.tables.items()
        },
        customer_id_columns=tuple(_split_column(ref) for ref in parsed.customer_id_columns),
        descriptions={
            (fold(table), fold(column)): text.strip()
            for table, columns in parsed.descriptions.items()
            for column, text in columns.items()
        },
        reviewed_against_live=parsed.reviewed_against_live,
    )
    problems = _problems(allowlist, parsed, settings)
    if problems:
        raise ConfigError(
            f"{path.name}: " + "; ".join(problems) + ".",
            fix=f"Fix {path}. A column must be either allowed or blocked, never both, and the scope columns must be allowed.",
        )
    return allowlist


def _split_column(reference: str) -> tuple[str, str]:
    table, _, column = reference.partition(".")
    return table.strip(), column.strip()


def _problems(allowlist: Allowlist, parsed: _AllowlistFile, settings: Settings) -> list[str]:
    problems: list[str] = []
    if fold(parsed.dataset) != fold(settings.bigquery.dataset):
        problems.append(f"it is written for dataset {parsed.dataset}, but bigquery.dataset is {settings.bigquery.dataset}")

    listed = {fold(name) for name in allowlist.tables}
    if len(listed) != len(allowlist.tables):
        problems.append("a table is listed twice (names are compared without regard to case)")
    for table in REQUIRED_TABLES:
        if table not in listed:
            problems.append(f"table {table} is missing")
    for table in allowlist.tables:
        if fold(table) not in REQUIRED_TABLES:
            problems.append(f"table {table} is not one of {', '.join(REQUIRED_TABLES)}")

    for table, policy in allowlist.tables.items():
        for label, columns in (("allowed", policy.allowed), ("quasi_identifiers", policy.quasi_identifiers), ("blocked", policy.blocked)):
            folded = [fold(column) for column in columns]
            if len(set(folded)) != len(folded):
                problems.append(f"{table}.{label} lists a column twice")
        allowed = {fold(column) for column in policy.allowed}
        for column in policy.blocked:
            if fold(column) in allowed:
                problems.append(f"{table}.{column} is both allowed and blocked")
        for column in policy.quasi_identifiers:
            if fold(column) not in allowed:
                problems.append(f"{table}.{column} is a quasi-identifier but not allowed")

    # The SQL Guard refuses a blocked name wherever it appears, whichever table it came from. So a name that is
    # blocked in one table and allowed in another could never be used in the second.
    for table, policy in allowlist.tables.items():
        for column in policy.allowed:
            if fold(column) in allowlist.blocked_names:
                problems.append(f"{table}.{column} is allowed, but the same name is blocked in another table")

    for reference in parsed.customer_id_columns:
        table, column = _split_column(reference)
        if not allowlist.table_name(table) or fold(column) not in {fold(c) for c in allowlist.allowed(table)}:
            problems.append(f"customer_id_columns names {reference}, which is not an allowed column")

    for table, columns in SCOPE_COLUMNS.items():
        allowed = {fold(column) for column in allowlist.allowed(table)}
        problems.extend(f"{table}.{column} must be allowed (the brand filter needs it)" for column in columns if column not in allowed)

    for table, columns in parsed.descriptions.items():
        allowed = {fold(column) for column in allowlist.allowed(table)}
        problems.extend(f"descriptions names {table}.{column}, which is not an allowed column" for column in columns if fold(column) not in allowed)
    return problems
