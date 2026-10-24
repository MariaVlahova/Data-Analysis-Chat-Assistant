"""Open the Golden Bucket for a chat: read the seed folder, check every trio, build the index and the glossary (plan 10).

Called once at startup when `golden.enabled` is true. A trio that fails a check is left out and named in a warning; nothing in the
Golden Bucket can stop the chat from starting, because it only adds examples to a prompt and the answer never depends on it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from src.access.users import UserContext
from src.config import Settings
from src.golden.glossary import Glossary, load_glossary
from src.golden.index import GoldenIndex
from src.golden.validate import ValidationContext, validate_location
from src.guards.allowlist import Allowlist
from src.guards.sql_guard import SQLGuard

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GoldenBundle:
    index: GoldenIndex
    glossary: Glossary
    skipped: int  # trios that failed a check and were left out
    warnings: tuple[str, ...]

    def examples(self, question: str, user: UserContext) -> list[dict[str, object]]:
        return self.index.examples(question, user)

    def definitions(self, question: str) -> list[str]:
        return self.glossary.lookup(question)


def open_golden(settings: Settings, allowlist: Allowlist, brands: frozenset[str] | None) -> GoldenBundle:
    """The index and glossary named by `golden.*` in the settings. `brands` is the real brand list (the catalog's)."""
    config = settings.golden
    warnings: list[str] = []
    seed_dir = settings.resolve(config.seed_dir)
    if not seed_dir.is_dir():
        warnings.append(f"The Golden Bucket folder was not found: {config.seed_dir}")
    ctx = ValidationContext(sql_guard=SQLGuard(settings, allowlist), brands=brands)
    results = validate_location(seed_dir, ctx)
    usable = [result.trio for result in results if result.ok and result.trio is not None]
    skipped = 0
    for result in results:
        if result.ok:
            continue
        skipped += 1
        reason = result.fails()[0]
        log.warning("golden trio %s is not loaded: %s: %s", result.name, reason.check, reason.message)
    if skipped:
        warnings.append(f"{skipped} of {len(results)} Golden Bucket trio(s) were not loaded (python -m src.cli.main golden validate shows why).")
    glossary, problems = load_glossary(settings.resolve(config.glossary_file))
    for problem in problems:
        log.warning("golden glossary: %s", problem)
    if problems:
        warnings.append("The glossary has problems (python -m src.cli.main golden validate shows them).")
    index = GoldenIndex(usable, top_k=config.top_k, min_similarity=config.min_similarity)
    log.info("golden bucket: %d trio(s) searchable, %d glossary entries", len(index), len(glossary.entries))
    return GoldenBundle(index, glossary, skipped, tuple(warnings))
