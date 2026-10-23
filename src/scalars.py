"""One helper for values that come out of pandas and numpy: the tracer (plan 02) and the pseudonymizer (plan 06) share it."""
from __future__ import annotations

from typing import Any


def plain(value: Any) -> Any:
    """A numpy scalar (np.int64, np.bool_, a 0-d array) as the Python value. A Series or array is not one."""
    if type(value).__module__ == "numpy" and getattr(value, "shape", None) == () and hasattr(value, "item"):
        return value.item()
    return value
