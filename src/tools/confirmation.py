"""Confirmation levels and the preview for a gated action (plan 07, F22 to F24; plan 08 reuses it).

Friction grows with risk (decision 3.6): one item takes `y` or `yes`, a short list takes `yes`, and "all" or a large batch
takes `DELETE <n>`. Everything else cancels. The comparison is done here, in code, against what the user typed. A model
never confirms anything.

The preview holds titles and dates only. The IDs behind them are never in it, and the confirmation is bound to a hash of
the exact ID set (`id_set_hash`), so a report added or removed after the preview makes the confirmation stale.
"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Sequence

from src.tools.base import Target

MAX_PREVIEW_ROWS = 10  # a longer list is cut with "... and N more"; the count is always the full one


@dataclass(frozen=True)
class Confirmation:
    level: int  # 1, 2 or 3
    expected: str  # what the user must type: "yes" (level 1 also accepts "y") or "DELETE 23"
    prompt: str


def id_set_hash(refs: Sequence[str], key: bytes) -> str:
    """The keyed hash of the exact set of IDs. Order does not matter; one more or one fewer ID changes it.

    It is checkpointed and its start is traced, and an ID can be short (8 hex characters is 2^32 guesses), so a plain hash could be
    inverted offline. The key is a secret of the installation and does not change between runs, so a confirmation still matches
    after a restart (plan 08).
    """
    body = "\n".join(sorted(set(refs)))
    return hmac.new(key, body.encode("utf-8"), hashlib.sha256).hexdigest()


def confirmation_for(count: int, selector_kind: str, threshold: int, noun: str, verb: str = "delete") -> Confirmation:
    plural = noun if count == 1 else f"{noun}s"
    if selector_kind == "all" or count >= threshold:
        return Confirmation(3, f"{verb.upper()} {count}", f"This will {verb} {count} {plural}. Type {verb.upper()} {count} to confirm.")
    if count == 1:
        return Confirmation(1, "yes", f"{verb.capitalize()} this {noun}? (y/N)")
    return Confirmation(2, "yes", f"{verb.capitalize()} these {count} {plural}? Type yes to confirm.")


def reply_confirms(level: int, expected: str, reply: object) -> bool:
    """True only for the exact reply of this level. `y` and `yes` ignore case and spacing; `DELETE n` must match exactly."""
    if not isinstance(reply, str):
        return False
    typed = reply.strip()
    if level == 1:
        return typed.lower() in ("y", "yes")
    if level == 2:
        return typed.lower() == "yes"
    return typed == expected


def preview_text(targets: Sequence[Target], noun: str) -> str:
    """A numbered list of titles and dates, built in code from what the finder returned."""
    plural = noun if len(targets) == 1 else f"{noun}s"
    lines = [f"I found {len(targets)} {plural}:"]
    for number, target in enumerate(targets[:MAX_PREVIEW_ROWS], 1):
        lines.append(f"{number}. {target.label or '(untitled)'}" + (f" ({target.when})" if target.when else ""))
    if len(targets) > MAX_PREVIEW_ROWS:
        lines.append(f"... and {len(targets) - MAX_PREVIEW_ROWS} more.")
    return "\n".join(lines)
