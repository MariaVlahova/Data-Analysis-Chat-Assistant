"""Domain words (plan 12, A1, R2a, NFR16): code checks that a question about data is about the store's data.

The Input Guard's classifier decides what counts as an analysis question, and a model can be fooled. This is the check behind it. A
message that the classifier took for a question about the data must hold at least one word of the store's domain, in what the user
typed (not in the model's rewrite). Pure functions: no model call, no BigQuery library.

The words are built at startup from what the app already has, never typed per question:
  - the table and column names of `schema_allowlist.yaml`, allowed and blocked, split on underscores (`email` is still a question about
    customers; the SQL Guard decides what may be read);
  - the glossary terms and their aliases;
  - the user's own brands and the product categories and departments of the data (added at the check, because they depend on the user);
  - a short fixed list: business words and words about the data itself.

A follow-up such as "and in 2024?" or "why?" has no domain word. It passes when the conversation already has an answered data turn
and every word of the message is a follow-up word: a stop word, a time word, a number, a comparison word or a pronoun.

The check is a floor, not a filter on meaning. It cannot tell a poem about orders from a question about orders: the SQL Guard (a
query must read a table), the numeric check (an answer must state a value of the data) and the Output Guard cover the rest.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Protocol, Sequence

MIN_WORD = 3  # a shorter piece of a column name ("id", "at", "of") is not a domain word
MIN_NAME_PART = 4  # a word taken from a brand, a category or a department is at least this long

# The pieces of column names that are not about the store ("first_name", "created_at", "num_of_item"): they stay out of the list.
SCHEMA_NOISE = frozenset({"first", "last", "code", "name", "source", "created", "geom", "num"})

BUSINESS_WORDS = frozenset("""
revenue sales sale spend spent spending order item brand return returned refund churn margin profit price cost discount units unit
inventory stock report insight performance trend growth decline top rank compare customer product category department store shop
retail sell sold selling buy bought buying purchase purchased buyer shopper cancel cancelled canceled ship shipped shipping delivered
delivery earn earned earning turnover basket
""".split())
DATA_WORDS = frozenset("data table column field available dataset database".split())

STOP_WORDS = frozenset("""
a an the and or but so then also too just only of in on at to for from by with without about as is are was were be been being it its
this that these those there here what which who how why when where whom whose do does did can could would should will shall may might
please thanks thank you your me my we our us they them their i he she him her his not no yes ok okay more less again instead rather
than same other another each every all both any some most least much many few
""".split())
TIME_WORDS = frozenset("""
today yesterday tomorrow now week month quarter year day ago since until before after during between over under next previous prior
current ytd mtd daily weekly monthly quarterly yearly annual annually january february march april may june july august september
october november december jan feb mar apr jun jul aug sep sept oct nov dec monday tuesday wednesday thursday friday saturday sunday
first last early late recent recently earlier later
""".split())
COMPARISON_WORDS = frozenset("compare compared comparison versus vs against difference different better worse higher lower bigger smaller larger similar".split())
PRONOUNS = frozenset("it they them those these that this one ones".split())
REQUEST_WORDS = frozenset("show tell give see list break split group sort filter exclude include redo repeat run try use check explain break down up".split())
FOLLOW_UP_WORDS = STOP_WORDS | TIME_WORDS | COMPARISON_WORDS | PRONOUNS | REQUEST_WORDS

_TOKEN = re.compile(r"[^\W_]+")
_NUMBER_LIKE = re.compile(r"^(?:\d+(?:st|nd|rd|th)?|q[1-4]|h[12]|fy\d{2,4}|\d{4}s)$")


def tokens(text: str) -> list[str]:
    """The words of a text: letters and digits, case-folded and compatibility-normalized. `Levi's` is `levi` and `s`."""
    return _TOKEN.findall(unicodedata.normalize("NFKC", text).casefold())


def stem(word: str) -> str:
    """A plural and its singular share a stem (`customers`, `customer`; `categories`, `category`). Both sides of a comparison are
    stemmed, so the stem need not be a word."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith(("ches", "shes", "sses", "xes")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def _stems(text: str) -> list[str]:
    return [stem(token) for token in tokens(text)]


_FOLLOW_UP_STEMS = frozenset(stem(word) for word in FOLLOW_UP_WORDS)


def is_follow_up(text: str) -> bool:
    """True when every word of the message is a follow-up word, a number or a year ("and in 2024?", "why?", "what about last year?").
    A message of no words is not a follow-up."""
    words = tokens(text)
    return bool(words) and all(_NUMBER_LIKE.match(word) or word in FOLLOW_UP_WORDS or stem(word) in _FOLLOW_UP_STEMS for word in words)


class AllowlistLike(Protocol):
    def table_names(self) -> Iterable[str]: ...

    def allowed(self, table: str) -> Sequence[str]: ...

    def blocked(self, table: str) -> Sequence[str]: ...


def _schema_words(allowlist: AllowlistLike) -> set[str]:
    names: list[str] = []
    for table in allowlist.table_names():
        names.append(table)
        names.extend(allowlist.allowed(table))
        names.extend(allowlist.blocked(table))
    return {part for name in names for part in name.casefold().split("_") if len(part) >= MIN_WORD and part not in SCHEMA_NOISE}


@dataclass(frozen=True)
class DomainWords:
    """The stems of the single words and the stem-sequences of the phrases that make a message a question about the store."""

    words: frozenset[str]
    phrases: frozenset[str] = frozenset()  # each is the stems of a multi-word term joined by a space

    def has_domain_word(self, text: str, extra: Iterable[str] = ()) -> bool:
        """True when the text holds a domain word. `extra` are terms that depend on the user or the data: brands, categories,
        departments. A multi-word term matches as a phrase, and its longer pieces (4 letters or more, not a common word) as words."""
        stems = _stems(text)
        if not stems:
            return False
        if not self.words.isdisjoint(stems):
            return True
        padded = " " + " ".join(stems) + " "
        if any(f" {phrase} " in padded for phrase in self.phrases):
            return True
        present = set(stems)
        for term in extra:
            term_stems = _stems(term)
            if not term_stems:
                continue
            if len(term_stems) > 1 and f" {' '.join(term_stems)} " in padded:
                return True
            if any(len(piece) >= MIN_NAME_PART and piece not in _FOLLOW_UP_STEMS and piece in present for piece in term_stems):
                return True
            if len(term_stems) == 1 and term_stems[0] in present:
                return True
        return False


def build_domain_words(allowlist: AllowlistLike, glossary_terms: Iterable[str] = ()) -> DomainWords:
    """The words that do not depend on the user. `glossary_terms` are every term and alias of the glossary: a single word counts as a
    word, several words as a phrase, and a term made only of follow-up words ("last month") counts for nothing."""
    words = {stem(word) for word in (*BUSINESS_WORDS, *DATA_WORDS)} | {stem(word) for word in _schema_words(allowlist)}
    phrases: set[str] = set()
    for term in glossary_terms:
        term_stems = _stems(term)
        if not term_stems or all(piece in _FOLLOW_UP_STEMS for piece in term_stems):
            continue
        if len(term_stems) == 1:
            words.add(term_stems[0])
        else:
            phrases.add(" ".join(term_stems))
    return DomainWords(frozenset(words), frozenset(phrases))
