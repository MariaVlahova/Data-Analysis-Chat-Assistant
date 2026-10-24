"""Small text helpers shared by more than one layer."""
from __future__ import annotations

from typing import Any


def squash(text: Any, limit: int | None = None) -> str:
    """The text on one line: runs of white space (new lines and tabs too) become one space and the ends are trimmed. Then cut to
    `limit` characters when one is given. None is empty and anything else is read as text.

    This does not mask. Where the text can echo something the user typed, mask first and cut after (`pii.mask(text)[:n]`): a cut
    can leave half an e-mail that no pattern matches.
    """
    squashed = " ".join(str(text if text is not None else "").split())
    return squashed if limit is None else squashed[:limit]
