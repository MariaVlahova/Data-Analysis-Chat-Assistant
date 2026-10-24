"""The checks every trio passes before it can be retrieved: the Ingest Job in miniature (plan 10, `golden validate`).

Production runs these when an analyst puts a trio in the bucket (validate, dry-run, Cloud Sensitive Data Protection, embed). Here the
chat runs the offline ones when it loads the seed folder, and `golden validate` adds the BigQuery dry run:

| Check      | What fails a trio                                                                                              |
|------------|----------------------------------------------------------------------------------------------------------------|
| Schema     | a missing or mistyped field, an unknown field, a repeated `id`                                                  |
| Tables     | a table outside the four approved ones                                                                          |
| SQL        | the SQL Guard refuses it (under a user who sees all brands), or the dry run fails or is over the byte cap       |
| Report     | personal data in the question, notes, report or SQL                                                             |
| Brands     | a brand that is not in the data; a brand name in the text that is not among the trio's own brands (S5)          |
| Duplicates | (a warning) nearly the same question as another trio                                                            |

A trio that fails is not loaded. Warnings are shown and do not stop it: an instruction-like phrase, a number in the report of a
brand-agnostic trio (it can carry another brand's figure), a `tables` list that does not match the SQL, a file name that is not
the id.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

from src import pii
from src.access.users import UserContext
from src.errors import AgentError
from src.golden.index import tokenize
from src.golden.schema import LoadedFile, Trio, load_trio_files
from src.guards.allowlist import REQUIRED_TABLES
from src.guards.output_guard import OutputGuard
from src.guards.query import GuardedQuery
from src.guards.sql_guard import SQLGuard

Level = Literal["fail", "warn"]

# The SQL Guard is run as a user who sees every brand: the trio's SQL must be valid on its own, and the brand filter is added
# for each real user at query time.
VALIDATION_USER = UserContext("golden-validate", "Golden validate", "ceo", (), True, "golden-validate")

DUPLICATE_SIMILARITY = 0.8  # Jaccard overlap of the question words
MIN_BRAND_NAME = 3  # a shorter name is too likely to be an ordinary word to look for

INSTRUCTION_LIKE = re.compile(
    r"ignore\s+(?:all\s+|any\s+|the\s+|your\s+)?(?:previous|prior|above|earlier)|disregard\s+(?:the\s+|all\s+|your\s+)?(?:previous|above|rules|instructions)|"
    r"system\s+prompt|you\s+are\s+now|developer\s+mode|reveal\s+(?:your|the)\s+(?:rules|instructions|prompt)|override\s+(?:the\s+|your\s+)?(?:rules|guard|filter)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Issue:
    level: Level
    check: str  # Schema, Tables, SQL, Report, Brands or Duplicates
    message: str


@dataclass
class TrioResult:
    path: Path
    trio: Trio | None
    issues: list[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.trio is not None and not any(issue.level == "fail" for issue in self.issues)

    @property
    def name(self) -> str:
        return self.trio.id if self.trio is not None else self.path.name

    def fails(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.level == "fail"]

    def warnings(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.level == "warn"]


@dataclass(frozen=True)
class ValidationContext:
    sql_guard: SQLGuard | None = None  # None: the SQL is not checked
    output_guard: OutputGuard = field(default_factory=OutputGuard)
    brands: frozenset[str] | None = None  # None: the brand list is not known, so brands are not checked (a warning says so)
    dry_run: Callable[[GuardedQuery], object] | None = None  # None: no BigQuery dry run


def _sql_issue(err: AgentError) -> str:
    rule = f" {err.rule_id}" if err.rule_id else ""
    return f"{err.kind.value}{rule}: {err.detail or err.user_message}"


def _brand_pattern(name: str) -> re.Pattern[str]:
    return re.compile(rf"(?<!\w){re.escape(name)}(?!\w)")


def check_trio(trio: Trio, ctx: ValidationContext) -> list[Issue]:
    issues: list[Issue] = []
    add = lambda level, check, message: issues.append(Issue(level, check, message))  # noqa: E731

    bad_tables = sorted(set(trio.tables) - set(REQUIRED_TABLES))
    if bad_tables:
        add("fail", "Tables", f"not an approved table: {', '.join(bad_tables)} (approved: {', '.join(REQUIRED_TABLES)})")

    if ctx.sql_guard is not None:
        try:
            guarded = ctx.sql_guard.check(trio.sql, VALIDATION_USER)
        except AgentError as err:
            add("fail", "SQL", _sql_issue(err))
        else:
            if set(guarded.tables) != set(trio.tables):
                add("warn", "SQL", f"`tables` lists {sorted(trio.tables)} but the SQL reads {sorted(guarded.tables)}")
            if ctx.dry_run is not None:
                try:
                    ctx.dry_run(guarded)
                except AgentError as err:
                    add("fail", "SQL", f"dry run: {err.kind.value}: {err.detail or err.user_message}")

    texts = {"question": trio.question, "notes": trio.interpretation_notes, "report": trio.report}
    for name, text in texts.items():
        found = sorted({finding.type for finding in ctx.output_guard.scan(text)})
        if found:
            add("fail", "Report", f"personal data in the {name}: {', '.join(found)}")
    found = sorted({finding.type for finding in pii.scan(trio.sql)})
    if found:
        add("fail", "Report", f"personal data in the SQL: {', '.join(found)}")  # a literal such as an e-mail address
    for name, text in texts.items():
        if INSTRUCTION_LIKE.search(text):
            add("warn", "Report", f"the {name} has an instruction-like phrase; retrieved text is data, but an analyst should look")
    if trio.brand_agnostic and re.search(r"\d", trio.report):
        add("warn", "Report", "a brand-agnostic report holds a number; it could be another brand's figure (S5). Write it in words")

    if ctx.brands is None:
        if not trio.brand_agnostic:
            add("warn", "Brands", "not checked: the brand list is not known (BigQuery could not be read)")
    else:
        unknown = [brand for brand in trio.brands if brand != "*" and brand not in ctx.brands]
        if unknown:
            add("fail", "Brands", "not a brand in the data: " + ", ".join(f'"{brand}"' for brand in unknown))
        whole_text = "\n".join([*texts.values(), trio.sql])
        own = set(trio.brands)
        named = sorted(
            brand for brand in ctx.brands
            if len(brand.strip()) >= MIN_BRAND_NAME and brand not in own and _brand_pattern(brand).search(whole_text)
        )
        if named:
            add("fail", "Brands", "the text names " + ", ".join(f'"{brand}"' for brand in named) + ", which is not among the trio's own brands (S5)")
    return issues


def _duplicates(results: list[TrioResult]) -> None:
    """A warning on the later trio of every pair that asks nearly the same question."""
    kept = [(result, set(tokenize(result.trio.question))) for result in results if result.trio is not None]
    for position, (later, words) in enumerate(kept):
        for earlier, other in kept[:position]:
            union = words | other
            if union and len(words & other) / len(union) >= DUPLICATE_SIMILARITY:
                later.issues.append(Issue("warn", "Duplicates", f"nearly the same question as {earlier.trio.id}"))
                break


def validate_loaded(loaded: list[LoadedFile], ctx: ValidationContext) -> list[TrioResult]:
    results: list[TrioResult] = []
    for item in loaded:
        result = TrioResult(item.path, item.trio)
        if item.trio is None:
            result.issues.append(Issue("fail", "Schema", item.error or "the file could not be read"))
        else:
            if item.path.stem != item.trio.id:
                result.issues.append(Issue("warn", "Schema", f"the file is called {item.path.name} but the id is {item.trio.id}"))
            result.issues.extend(check_trio(item.trio, ctx))
        results.append(result)
    _duplicates(results)
    return results


def validate_location(location: Path, ctx: ValidationContext) -> list[TrioResult]:
    """Read a folder (or one file) of trios and check every one."""
    return validate_loaded(load_trio_files(location), ctx)
