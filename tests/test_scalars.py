"""The one helper for numpy scalars that the tracer and the pseudonymizer share."""
import numpy as np
import pandas as pd

from src import scalars
from src.guards import pseudonymizer
from src.observability import tracing


def test_a_numpy_scalar_becomes_the_python_value():
    for value, expected in ((np.int64(7), 7), (np.float64(1.5), 1.5), (np.bool_(True), True), (np.array(3), 3), (np.str_("a"), "a")):
        out = scalars.plain(value)
        assert out == expected and type(out) in (int, float, bool, str)


def test_anything_else_is_left_alone():
    series, array, obj = pd.Series([1]), np.array([1, 2]), object()
    assert scalars.plain(series) is series and scalars.plain(array) is array and scalars.plain(obj) is obj
    assert scalars.plain(None) is None and scalars.plain("x") == "x" and scalars.plain(5) == 5


def test_there_is_one_copy_of_it():
    assert tracing.plain is scalars.plain and pseudonymizer.plain is scalars.plain
