"""Who may see which trio (plan 10, S5, Q21): only approved trios, and only trios of the user's own brands, before anything is scored."""
import pytest

from src.golden.index import GoldenIndex
from tests.golden_support import BOTH, CEO, MAPLE, NOBODY, OHARA, make_trio

QUESTION = "How is the core range selling this season?"


def trios():
    return [
        make_trio(id="g-agnostic", question="How is the core range selling this season overall?"),
        make_trio(id="g-maple", question=QUESTION, brands=["Maple & Co"], interpretation_notes="Core range of Maple & Co."),
        make_trio(id="g-ohara", question=QUESTION, brands=["O'Hara Outfitters"], interpretation_notes="Core range of O'Hara Outfitters."),
        make_trio(id="g-both", question=QUESTION, brands=["Maple & Co", "O'Hara Outfitters"], interpretation_notes="Core range of both."),
        make_trio(id="g-nordic", question=QUESTION, brands=["Nordic Peak"], interpretation_notes="Core range of Nordic Peak."),
        make_trio(id="g-draft", question=QUESTION, status="draft"),
        make_trio(id="g-retired", question=QUESTION, status="retired"),
    ]


@pytest.fixture
def index():
    return GoldenIndex(trios(), top_k=10, min_similarity=0.0)


def ids(user, index, question=QUESTION):
    return {hit.trio.id for hit in index.search(question, user)}


def test_only_approved_trios_are_ever_returned(index):
    for user in (MAPLE, OHARA, BOTH, CEO):
        assert not {"g-draft", "g-retired"} & ids(user, index)


def test_a_brand_manager_never_gets_a_trio_tagged_only_with_another_brand(index):
    assert ids(MAPLE, index).isdisjoint({"g-ohara"})
    assert ids(OHARA, index).isdisjoint({"g-maple", "g-nordic"})


def test_a_user_gets_the_brand_agnostic_trios_and_those_of_their_own_brands(index):
    assert ids(MAPLE, index) == {"g-agnostic", "g-maple", "g-nordic"}
    assert ids(OHARA, index) == {"g-agnostic", "g-ohara"}


def test_a_trio_of_two_brands_needs_both_to_be_the_users(index):
    """It shows the data of both, so one of them is not enough."""
    assert "g-both" not in ids(MAPLE, index) and "g-both" not in ids(OHARA, index)
    assert "g-both" in ids(BOTH, index)


def test_the_ceo_gets_every_approved_trio(index):
    assert ids(CEO, index) == {"g-agnostic", "g-maple", "g-ohara", "g-both", "g-nordic"}


def test_a_user_with_no_brands_gets_nothing_not_even_the_agnostic_trios(index):
    assert index.eligible(NOBODY) == [] and index.search(QUESTION, NOBODY) == []


def test_brand_names_are_compared_exactly(index):
    """products.brand is free text, so `maple & co` is another brand (S1)."""
    lower = MAPLE.__class__("x", "X", "brand_manager", ("maple & co",), False, "hash-x")
    assert "g-maple" not in ids(lower, index)


def test_the_filter_runs_before_scoring_so_a_better_match_of_another_brand_changes_nothing(index):
    """Searching for the other brand's own words must still not return its trio, however high it would score."""
    exact = "Core range of O'Hara Outfitters"
    assert "g-ohara" in ids(CEO, index, exact) and "g-ohara" not in ids(MAPLE, index, exact)


def test_no_part_of_an_ineligible_trio_reaches_the_examples(index):
    flat = repr(index.examples(QUESTION, MAPLE))
    assert "O'Hara" not in flat and "g-ohara" not in flat and "g-both" not in flat


def test_eligibility_does_not_change_the_scores_of_the_trios_that_remain(index):
    """The word weights are computed once over all approved trios, so what another user may see does not move a score."""
    for hit in index.search(QUESTION, MAPLE):
        assert hit.score == next(h.score for h in index.search(QUESTION, CEO) if h.trio.id == hit.trio.id)
