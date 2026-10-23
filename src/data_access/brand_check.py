"""Brand checks (plan 03, S1, A23).

`products.brand` is free text. A typo in `users.yaml` would give a brand manager an empty scope that looks like
"no sales", so startup compares every configured brand with the real list and stops on a miss. A brand is never
dropped silently.
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass

from src.access.users import PLACEHOLDER_BRAND, UserDirectory
from src.data_access.catalog import SchemaCatalog
from src.data_access.safe_runner import SafeQueryRunner, SystemQuery
from src.errors import ConfigError

MAX_SUGGESTIONS = 3
FIX = (
    "Use the exact brand names from the data (case and spacing count). "
    "`python -m src.cli.main doctor` lists the top brands by item count."
)
# The chat prints the check to the person who is starting it. Close matches would name brands that may belong to
# other managers, so only the operator's `doctor` shows them.
FIX_WITHOUT_NAMES = (
    "Correct the brand in users.yaml (case and spacing count). "
    "An administrator can run `python -m src.cli.main doctor` to see the real names."
)


class BrandMappingError(ConfigError):
    """`users.yaml` has brands that do not match the data. `problems` holds one entry per brand, as data."""

    def __init__(self, problems: list[str], fix: str) -> None:
        self.problems = list(problems)
        super().__init__("users.yaml has brands that do not match the data:\n" + "\n".join(self.problems), fix)

    def __reduce__(self):
        return (BrandMappingError, (self.problems, self.fix))


def suggest(brand: str, known: frozenset[str]) -> list[str]:
    """Up to 3 real brands that look like `brand`, ignoring case ("nike" finds "Nike")."""
    by_folded: dict[str, list[str]] = {}
    for name in sorted(known):
        by_folded.setdefault(name.casefold(), []).append(name)
    close = difflib.get_close_matches(brand.casefold(), list(by_folded), n=MAX_SUGGESTIONS, cutoff=0.6)
    return [name for key in close for name in by_folded[key]][:MAX_SUGGESTIONS]


def check_brand_mapping(
    users: UserDirectory, catalog: SchemaCatalog, *, only_user: str | None = None, suggest_names: bool = True
) -> None:
    """Every brand of every brand-scoped user must match a real brand exactly. Raises `BrandMappingError`.

    `only_user` limits the check to one login name: the chat checks the user who is starting, so one manager's
    typo does not take the chat offline for everyone. `doctor` checks all users.
    `suggest_names=False` leaves out the "did you mean" names, for output a brand manager reads.
    """
    problems = []
    for name in users.names:
        if only_user is not None and name != only_user:
            continue
        user = users.get(name)
        if user.scope_mode != "brands":
            continue  # the CEO sees all brands, and an empty list is refused elsewhere (S14)
        for brand in user.brands:
            if brand in catalog.brands:
                continue
            if PLACEHOLDER_BRAND.match(brand):
                problems.append(f'{name}: "{brand}" is still a placeholder.')
                continue
            close = suggest(brand, catalog.brands) if suggest_names else []
            hint = " Did you mean " + " or ".join(f'"{c}"' for c in close) + "?" if close else ""
            problems.append(f'{name}: "{brand}" is not a brand in the data.{hint}')
    if problems:
        raise BrandMappingError(problems, FIX if suggest_names else FIX_WITHOUT_NAMES)


# live facts for `doctor` -----------------------------------------------------------------------

def top_brands(safe: SafeQueryRunner, limit: int = 10) -> list[tuple[str, int]]:
    """The brands with the most order items, to pick real names for `users.yaml`."""
    outcome = safe.run(SystemQuery(
        f"SELECT p.brand AS brand, COUNT(*) AS items "
        f"FROM {safe.table_ref('order_items')} oi JOIN {safe.table_ref('products')} p ON oi.product_id = p.id "
        f"WHERE p.brand IS NOT NULL GROUP BY p.brand ORDER BY items DESC LIMIT {int(limit)}"
    ))
    return [(str(row.brand), int(row.items)) for row in outcome.df.itertuples(index=False)]


@dataclass(frozen=True)
class BrandCoverage:
    items: int
    without_product: int  # the item's product is missing from `products`
    without_brand: int  # the product has no brand (null or blank)

    @property
    def invisible_to_managers(self) -> int:
        """Items no brand-scoped user can see. Only the CEO sees them (A23)."""
        return self.without_product + self.without_brand


def brand_coverage(safe: SafeQueryRunner) -> BrandCoverage:
    outcome = safe.run(SystemQuery(
        f"SELECT COUNT(*) AS items, COUNTIF(p.id IS NULL) AS without_product, "
        f"COUNTIF(p.id IS NOT NULL AND (p.brand IS NULL OR TRIM(p.brand) = '')) AS without_brand "
        f"FROM {safe.table_ref('order_items')} oi LEFT JOIN {safe.table_ref('products')} p ON oi.product_id = p.id"
    ))
    row = outcome.df.iloc[0]
    return BrandCoverage(int(row["items"]), int(row["without_product"]), int(row["without_brand"]))
