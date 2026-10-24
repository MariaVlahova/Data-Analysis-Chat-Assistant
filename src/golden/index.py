"""In-memory retrieval of similar trios (plan 10, decision 3.3 prototype stand-in).

Two steps, in this order. First the **filter**: only `approved` trios, and only those the user may see (brand-agnostic, or every
brand of the trio is one of the user's, or the user sees all brands). It runs before scoring and applies to the whole trio (S5).
Then the **score**: TF-IDF vectors over `question + interpretation_notes + tables` (the question counts twice), cosine similarity
between 0 and 1, the best
`top_k` at or above `min_similarity`. Below the threshold nothing is added to the prompts (decision 3.3).

Why not embeddings here: they cost Gemini calls on a rate-limited free tier and the seed set is tiny. `Retriever` is the interface
the graph sees, so a vector or hybrid retriever (pgvector plus full text, the production design) replaces `GoldenIndex` without a
change in the graph.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Protocol, Sequence

from src.access.users import UserContext
from src.golden.schema import Trio

# Words that carry no topic. Question words and possessives are here too, so "show me my best sellers" scores on "best sellers".
STOP_WORDS = frozenset(
    "a an and are as at be been but by can could did do does for from had has have how i if in into is it its me my of on or our "
    "over show so than that the their them then there these they this to up us was we were what when where which who whom why will "
    "with would you your tell give list get find please".split()
)
_TOKEN = re.compile(r"[a-z0-9]+")


QUESTION_WEIGHT = 2  # the question counts twice as much as the notes and table names: it is the best signal of what a trio is about


def _singular(token: str) -> str:
    """Plural endings only (`customers`, `categories`, `boxes`), so a question in the plural finds a trio in the singular. This is
    all the stemming there is: `spending` and `spent` stay different words, which is a known limit of word matching."""
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 4 and token.endswith(("sses", "xes", "zes", "ches", "shes")):
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    return token


def tokenize(text: str) -> list[str]:
    """Lower-case letters and digits, plural endings folded, no stop words, no one-character words."""
    return [_singular(token) for token in _TOKEN.findall(text.lower()) if token not in STOP_WORDS and len(token) > 1]


@dataclass(frozen=True)
class Hit:
    trio: Trio
    score: float


class Retriever(Protocol):
    """What the graph needs from the Golden Bucket: the trios that are good examples for this user's question."""

    def search(self, question: str, user: UserContext) -> list[Hit]: ...


def _document(trio: Trio) -> list[str]:
    return tokenize(trio.question) * QUESTION_WEIGHT + tokenize(f"{trio.interpretation_notes} {' '.join(trio.tables)}")


def _weights(counts: Counter[str], idf: dict[str, float], unknown_idf: float) -> dict[str, float]:
    """Sublinear term frequency times idf, scaled to length 1. A word the index has never seen still counts in the length with
    the highest idf, so a long question of unrelated words cannot score high on the two words it shares."""
    raw = {term: (1 + math.log(count)) * idf.get(term, unknown_idf) for term, count in counts.items()}
    norm = math.sqrt(sum(value * value for value in raw.values()))
    return {term: value / norm for term, value in raw.items()} if norm else {}


class GoldenIndex:
    def __init__(self, trios: Sequence[Trio], *, top_k: int = 3, min_similarity: float = 0.15) -> None:
        self._trios = tuple(trios)
        self.top_k = top_k
        self.min_similarity = min_similarity
        searchable = [trio for trio in self._trios if trio.searchable]
        documents = {trio.id: Counter(_document(trio)) for trio in searchable}
        total = len(searchable)
        frequency = Counter(term for counts in documents.values() for term in counts)
        self._idf = {term: math.log((1 + total) / (1 + count)) + 1 for term, count in frequency.items()}
        self._unknown_idf = math.log((1 + total) / 1) + 1
        self._vectors = {trio_id: _weights(counts, self._idf, self._unknown_idf) for trio_id, counts in documents.items()}

    @property
    def trios(self) -> tuple[Trio, ...]:
        return self._trios

    def __len__(self) -> int:
        return sum(1 for trio in self._trios if trio.searchable)

    def eligible(self, user: UserContext) -> list[Trio]:
        """The searchable trios this user may see. A user with no access sees none."""
        mode = user.scope_mode
        if mode == "none":
            return []
        allowed = set(user.brands)
        return [
            trio for trio in self._trios
            if trio.searchable and (mode == "all" or trio.brand_agnostic or set(trio.brands) <= allowed)
        ]

    def search(self, question: str, user: UserContext) -> list[Hit]:
        eligible = self.eligible(user)  # before scoring: a trio of another brand is never even a candidate
        query = _weights(Counter(tokenize(question)), self._idf, self._unknown_idf)
        if not eligible or not query:
            return []
        hits = []
        for trio in eligible:
            vector = self._vectors[trio.id]
            score = sum(weight * vector.get(term, 0.0) for term, weight in query.items())
            if score >= self.min_similarity:
                hits.append(Hit(trio, round(score, 4)))
        hits.sort(key=lambda hit: (-hit.score, hit.trio.id))
        return hits[: self.top_k]

    def examples(self, question: str, user: UserContext) -> list[dict[str, object]]:
        """The hits as plain records for the graph: the id and score for the trace, and the views each step may see. The SQL view
        (question, notes, SQL) goes under the usual keys, and the Composer's report excerpt is kept apart under `report`."""
        return [
            {**hit.trio.view("sql"), "report": hit.trio.view("composer")["report"], "score": hit.score}
            for hit in self.search(question, user)
        ]
