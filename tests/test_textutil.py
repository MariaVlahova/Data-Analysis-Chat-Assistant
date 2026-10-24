"""`squash`: the one place that puts text on a line and cuts it."""
import pytest

from src.textutil import squash


@pytest.mark.parametrize("text,limit,expected", [
    ("  a \n b\t\tc  ", None, "a b c"),
    ("abcdef", 3, "abc"),
    ("a   b   c", 3, "a b"),  # the cut comes after the white space is squashed
    (None, 5, ""),
    (12.5, None, "12.5"),
    ("", None, ""),
])
def test_squash(text, limit, expected):
    assert squash(text, limit) == expected
