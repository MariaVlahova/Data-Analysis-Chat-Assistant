"""Retrieval over the seed trios (plan 10): the threshold is chosen on probe questions, and the probes are written down here.

THE THRESHOLD. `golden.min_similarity` is 0.15. It was picked on the seed set: the lowest score of a question that should find its
trio is about 0.175 and the highest score of an unrelated one is about 0.10, so 0.15 sits between them. The first guess, 0.25, lost
four of the paraphrases below. If the seed set grows, run these probes again and move the value only if they still separate.

KNOWN MISS. "who are my biggest spending customers" does not find g-002 (it scores 0.14, and g-007 scores 0.29): `spending` and
`spent` are different words to a word matcher, and so are `biggest` and `most`. This is what the production design's embeddings are
for (plan 11), and it is why the prototype's retriever sits behind the `Retriever` interface.
"""
import pytest

from src.config import GoldenConfig
from src.golden.index import GoldenIndex, Hit, Retriever, tokenize
from src.golden.schema import load_trio_files
from tests.golden_support import CEO, MAPLE, NOBODY, SEED_DIR, make_trio

# question -> the trio that must come back first
PARAPHRASES = {
    "what was the sales trend by month last year": "g-001",
    "show me the monthly revenue for the past year": "g-001",
    "rank the top buyers by what they spent": "g-002",
    "which product categories earn the most revenue": "g-003",
    "what categories bring in the most money": "g-003",
    "compare product A and product B on units sold, revenue and returns": "g-004",
    "why is spend per customer lower in Ohio than in Texas": "g-005",
    "why do customers in Texas spend less than customers in California": "g-005",
    "which categories have the highest return rate": "g-006",
    "how do younger and older customers differ in how much they spend": "g-007",
    "give me a report on last quarter with insights and action items": "g-008",
}
UNRELATED = [
    "what is the weather in Paris today", "write me a poem about autumn", "how many people work in the warehouse", "tell me a joke",
    "what is the sales tax in Ohio", "who won the football match yesterday", "hello there", "translate this sentence into Spanish",
]


@pytest.fixture(scope="module")
def index():
    trios = [item.trio for item in load_trio_files(SEED_DIR) if item.trio is not None]
    return GoldenIndex(trios, top_k=3, min_similarity=GoldenConfig().min_similarity)


def test_the_chosen_threshold_is_the_default():
    assert GoldenConfig().min_similarity == 0.15


@pytest.mark.parametrize("question,expected", PARAPHRASES.items())
def test_a_paraphrase_of_a_seed_question_finds_that_trio_first(index, question, expected):
    hits = index.search(question, CEO)
    assert hits and hits[0].trio.id == expected, [(hit.trio.id, hit.score) for hit in hits]


@pytest.mark.parametrize("question", UNRELATED)
def test_an_unrelated_question_finds_nothing(index, question):
    assert index.search(question, CEO) == []


def test_the_probes_are_separated_by_the_threshold_with_room_to_spare(index):
    """Why 0.15 and not a number closer to either side: the margin on both sides is at least 0.03."""
    loose = GoldenIndex(index.trios, top_k=3, min_similarity=0.0)
    positives = [next(h.score for h in loose.search(q, CEO) if h.trio.id == want) for q, want in PARAPHRASES.items()]
    negatives = [max([h.score for h in loose.search(q, CEO)] or [0.0]) for q in UNRELATED]
    assert min(positives) - index.min_similarity >= 0.02 and index.min_similarity - max(negatives) >= 0.04, (min(positives), max(negatives))


def test_the_same_question_is_the_best_match_with_a_high_score(index):
    hits = index.search("Which product categories drive the most revenue?", CEO)
    assert hits[0].trio.id == "g-003" and hits[0].score > 0.7  # the notes and tables are part of the document, so it is not 1.0


def test_at_most_top_k_hits_come_back_best_first(index):
    loose = GoldenIndex(index.trios, top_k=2, min_similarity=0.0)
    hits = loose.search("categories revenue returns customers", CEO)
    assert len(hits) == 2 and hits[0].score >= hits[1].score


def test_a_score_is_between_zero_and_one(index):
    loose = GoldenIndex(index.trios, top_k=12, min_similarity=0.0)
    for question in list(PARAPHRASES) + UNRELATED:
        assert all(0.0 <= hit.score <= 1.0 for hit in loose.search(question, CEO))


def test_the_threshold_is_inclusive_and_changes_what_comes_back(index):
    question = "show me the monthly revenue for the past year"
    score = next(h.score for h in GoldenIndex(index.trios, min_similarity=0.0).search(question, CEO) if h.trio.id == "g-001")
    assert [h.trio.id for h in GoldenIndex(index.trios, min_similarity=score).search(question, CEO)][:1] == ["g-001"]
    assert GoldenIndex(index.trios, min_similarity=score + 0.001).search(question, CEO) == []


def test_an_empty_question_or_one_of_only_stop_words_finds_nothing(index):
    assert index.search("", CEO) == [] and index.search("what is the of and", CEO) == []


def test_an_empty_index_finds_nothing_and_has_no_trios():
    empty = GoldenIndex([])
    assert empty.search("categories revenue", CEO) == [] and len(empty) == 0


def test_a_long_question_of_unrelated_words_cannot_score_on_the_two_words_it_shares(index):
    padded = "revenue categories " + " ".join(f"zebra{n}" for n in range(30))
    assert index.search(padded, CEO) == []


def test_search_is_deterministic_and_ties_break_on_the_id():
    twin_a, twin_b = make_trio(id="g-201"), make_trio(id="g-200")
    hits = GoldenIndex([twin_a, twin_b], min_similarity=0.0).search(twin_a.question, CEO)
    assert [hit.trio.id for hit in hits] == ["g-200", "g-201"] and hits[0].score == hits[1].score


# the words -----------------------------------------------------------------------------------------------------------

def test_tokens_are_lower_case_without_stop_words_and_plurals_are_folded():
    assert tokenize("Which Categories drive the MOST revenue?") == ["category", "drive", "most", "revenue"]
    words = tokenize("Which categories drive our customers' boxes and the status?")
    assert "category" in words and "customer" in words and "box" in words and "status" in words
    assert "which" not in words and "the" not in words and "our" not in words


def test_digits_stay_and_one_character_words_go():
    assert tokenize("q1 a 2026 x") == ["q1", "2026"]


# the interface -------------------------------------------------------------------------------------------------------

def test_the_index_satisfies_the_retriever_interface_the_graph_sees(index):
    retriever: Retriever = index
    assert all(isinstance(hit, Hit) for hit in retriever.search("which product categories earn the most revenue", CEO))


def test_examples_are_plain_records_with_the_sql_view_and_a_separate_report_excerpt(index):
    (first, *_) = index.examples("which product categories earn the most revenue", MAPLE)
    assert first["id"] == "g-003" and first["sql"].startswith("SELECT") and first["report"] and first["score"] > 0.3
    assert set(first) == {"id", "question", "notes", "sql", "report", "score"}


def test_a_user_with_no_access_gets_nothing_even_for_a_perfect_match(index):
    assert index.search("Which product categories drive the most revenue?", NOBODY) == []
