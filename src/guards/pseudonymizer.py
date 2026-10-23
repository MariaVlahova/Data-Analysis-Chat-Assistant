"""Fake customer IDs for one chat session (plan 06, F14, owner decision 2026-10-22, A3).

Each real `users.id` becomes a label such as `Customer #48213`. The same customer keeps the same label for the whole
session, and two customers never share one. The label is drawn at random, so it cannot be turned back into the ID or
matched across sessions. The model, the traces and the user only ever see labels.

This object is the one place that knows which real customers appeared. So it follows three rules:
- it lives in memory for the process and is handed to the node that needs it through a closure. It must not be put in
  graph state, in LangGraph `config` or in a trace: `copy` and `pickle` refuse it, so a checkpoint write fails loudly
  instead of saving real IDs;
- it never keeps a real ID: the lookup key is a keyed hash with a salt that exists only in this object;
- a restart gives new labels, so "tell me more about Customer #48213" cannot be resolved afterwards, and the model
  never had the real ID to write that SQL with. The prototype says so honestly.
"""
from __future__ import annotations

import hashlib
import math
import random
import secrets
import threading
from decimal import Decimal
from typing import Any

from src.scalars import plain

LABEL_PREFIX = "Customer #"
FIRST_DIGITS = 5  # `Customer #48213`
# The Output Guard lets a label of this many digits through and nothing longer, so a long number after "Customer #" is
# still scanned. Seven digits hold about 10 million customers, far more than a session sees: past that we stop, not widen.
MAX_DIGITS = 7
_FULL_AT = 0.5  # a space that is half used gets one more digit, so a draw never has to search long


def _identity(real_id: Any) -> str:
    """The text two spellings of the same customer share: `7`, `7.0`, `Decimal("7")` and numpy 7 are one customer.

    Raises TypeError for a value that cannot be a customer ID (None, a flag, a collection, bytes, NaN): the Result Guard
    turns the error into a withheld result.
    """
    value = plain(real_id)
    if isinstance(value, bool) or value is None:
        raise TypeError("not a customer ID")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError("not a customer ID")
        return str(int(value)) if value.is_integer() else repr(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise TypeError("not a customer ID")
        return str(int(value)) if value == value.to_integral_value() else str(value)
    if isinstance(value, (int, str)):
        return str(value)
    raise TypeError("not a customer ID")


class SessionPseudonymizer:
    """One per CLI session. `fake_id(real)` gives `Customer #48213`, the same for the same customer all session."""

    def __init__(self, seed: int | None = None) -> None:
        # `seed` is for a test that needs to replay the draws. A real session leaves it out and gets a random one.
        self._rng = random.Random(secrets.randbits(64) if seed is None else seed)
        self._salt = secrets.token_bytes(16)
        self._labels: dict[str, str] = {}  # keyed hash of the real ID -> label
        self._taken: set[str] = set()
        self._digits = FIRST_DIGITS
        self._lock = threading.Lock()

    def fake_id(self, real_id: Any) -> str:
        key = hashlib.blake2b(_identity(real_id).encode("utf-8"), key=self._salt, digest_size=16).hexdigest()
        with self._lock:
            label = self._labels.get(key)
            if label is None:
                label = self._draw()
                self._labels[key] = label
            return label

    @property
    def size(self) -> int:
        """How many customers have a label so far."""
        return len(self._labels)

    def _draw(self) -> str:
        # Uniqueness is guaranteed by the lookup, not by chance: a 5-digit space plus a birthday collision is why.
        # Labels with different digit counts can never be equal, so widening keeps every earlier label valid.
        if len(self._taken) >= _FULL_AT * 9 * 10 ** (self._digits - 1):
            if self._digits >= MAX_DIGITS:
                raise RuntimeError("the customer label space is used up")  # fail closed: the Result Guard withholds the result
            self._digits += 1
        while True:
            label = f"{LABEL_PREFIX}{self._rng.randrange(10 ** (self._digits - 1), 10 ** self._digits)}"
            if label not in self._taken:
                self._taken.add(label)
                return label

    def __repr__(self) -> str:
        return f"<SessionPseudonymizer: {self.size} customers labelled>"

    def __reduce__(self):
        raise TypeError(
            "a SessionPseudonymizer stands for real customers and cannot be copied, pickled or stored: "
            "hand it to the node that needs it through a closure"
        )
