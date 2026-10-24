"""Helpers the Golden Bucket tests share (plan 10): a valid trio to change one field of, trio files on disk, and the users of the filter tests."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import yaml

from src.access.users import UserContext
from src.golden.schema import Trio

REPO_ROOT = Path(__file__).resolve().parent.parent
SEED_DIR = REPO_ROOT / "golden" / "seed"

GOOD_SQL = (
    "SELECT p.category, SUM(oi.sale_price) AS revenue FROM order_items oi JOIN products p ON oi.product_id = p.id "
    "WHERE oi.status NOT IN ('Cancelled', 'Returned') GROUP BY p.category ORDER BY revenue DESC LIMIT 10"
)

# Users with the brands of the generated sample data (src/localdata/generate.py).
MAPLE = UserContext("alice", "Alice", "brand_manager", ("Maple & Co", "Nordic Peak"), False, "hash-alice")
OHARA = UserContext("bob", "Bob", "brand_manager", ("O'Hara Outfitters",), False, "hash-bob")
BOTH = UserContext("both", "Both", "brand_manager", ("Maple & Co", "O'Hara Outfitters"), False, "hash-both")
CEO = UserContext("ceo", "CEO", "ceo", (), True, "hash-ceo")
NOBODY = UserContext("nobrands", "No Brands", "brand_manager", (), False, "hash-nobody")


def trio_data(**overrides: Any) -> dict[str, Any]:
    """A valid brand-agnostic, approved trio, as the YAML would hold it. Change one field to break one check."""
    data: dict[str, Any] = {
        "id": "g-100", "version": 1, "status": "approved",
        "question": "Which product categories earn the most money overall?",
        "interpretation_notes": "Revenue is the sum of item sale prices without cancelled or returned items.",
        "sql": GOOD_SQL,
        "report": "A few categories bring in most of the revenue. Say which ones lead and whether the long tail matters.",
        "tables": ["order_items", "products"], "brands": ["*"], "reviewed_by": "tester", "created_at": date(2026, 10, 24),
    }
    data.update(overrides)
    return data


def make_trio(**overrides: Any) -> Trio:
    return Trio.model_validate(trio_data(**overrides))


def write_trio(directory: Path, name: str, **overrides: Any) -> Path:
    """Write a trio file. A field set to `None` is left out of the file, to test a missing field."""
    data = {key: value for key, value in trio_data(**overrides).items() if value is not None}
    if isinstance(data.get("created_at"), date):
        data["created_at"] = data["created_at"].isoformat()
    path = directory / f"{name}.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path
