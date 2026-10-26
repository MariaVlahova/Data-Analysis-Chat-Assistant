"""The numeric grounding check (plan 07, F6, S11): every number in the answer must come from a query result.

Code runs this after the Composer and before the Output Guard. A number in the Composer's text is grounded when it equals,
after rounding to the precision it is shown with, a value in a guarded result (a cell, a total, a count), the user's
question, or a date part from the plan or the results. `K`, `M`, `B`, `thousand`, `million`, `billion`, `%` and a currency
sign are expanded first: "$1.2M" is 1,200,000 to within 50,000, and "12.5%" is a 12.5 or a 0.125 in the data.

A number that is not grounded loses its sentence, or its table row (Q29 default): the answer is shorter and says so, never
quietly wrong. A number next to `%` or a currency sign is never exempt. The only exempt numbers are plain whole numbers that
count something visible: the rows and bullets of the answer itself, and the sub-questions of the plan.

A second question follows (plan 12, A3, R2a): does the answer state anything from the data at all? `states_result_value` is true when
the body holds a figure that came from a result, or a text value of a result cell (a brand, a category, a month). A draft that states
none is not an answer to a question about the data, whatever its words, and the Composer node replaces it with the guarded figures.

The check is deliberately literal. It does not accept a growth rate or a share the model worked out itself. If that rejects
too many correct figures in the golden runs, add a code-computed `derived_facts` table to the Composer's input (plan 07,
"Optional, measurement-driven") so the model quotes instead of calculating.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Mapping, Sequence

MIN_USEFUL_CHARS = 25  # fewer letters and digits than this left after the removals is filler, not an answer ("Here are the key insights:")
NUMERIC_NOTE = "Some figures could not be verified against the data and were left out."
RULE_NOT_FROM_DATA = "ANSWER_NOT_FROM_DATA"  # the guard decision when a draft states nothing from the results (A3)
MIN_TEXT_VALUE = 2  # a text cell of one character ("M") is not evidence that the body speaks of the data

_NUMBER = re.compile(
    r"""
    (?<![\w.,])                                  # not inside a word or a longer number
    (?:(?<=[\s(\[>])[-−])?                  # a minus only after a space or a bracket: 2025-2026 is a range
    (?P<currency>[$€£])?\s?
    (?P<digits>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)
    (?P<suffix>\s?%|\s?percent\b|\s?(?:pct|pts?|pp)\b|(?:bn|mn|[KMBkmb])\b|x\b|\s?(?:thousand|million|billion)\b)?
    (?P<trailing>\s?(?:usd|eur|gbp|dollars?|euros?|bucks)\b|\s?[$€£])?   # "12 USD", "12 dollars", "1,234 EUR"
    (?!\w)                                       # "3rd", "10pm" and "Q1" are not numbers
    """,
    re.VERBOSE | re.IGNORECASE,  # "2 Million", "2 PERCENT", "2X": a unit is a unit in a title or a heading too
)
_SCALES = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mn": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9, "billion": 1e9}
_PERCENT_SUFFIXES = frozenset({"%", "percent", "pct", "pt", "pts", "pp"})  # "12 pts" and "12 pp" are percentage points
# A text cell that is a date or a timestamp: its digits can ground a year, a month or a day, but never a percentage or an amount.
_DATE_LIKE = re.compile(r"^\d{4}-\d{2}(?:-\d{2})?(?:[T ][\d:.+\-Z ]*)?$")
_DIGIT_GROUPS = re.compile(r"\d+(?:\.\d+)?")
_LIST_MARKER = re.compile(r"^(\s*(?:[-*+]|\d+[.)])\s+|\s*#{1,6}\s+(?:\d+[.)]\s+)?|\s*>\s*)")
# A sentence ends at . ! or ? and any closing marks (bold, a bracket, a quote) that follow it, before a capital or a digit. The
# marks stay with the sentence they close, so `**Brand A earned $5.** Brand B earned $6.` is two sentences, not one.
_SENTENCE_END = re.compile(r"(?<=[.!?])([*_)\]\"']*)\s+(?=[A-Z0-9*_(\[\"'])")
_TABLE_ROW = re.compile(r"^\s*\|")
_HEADING = re.compile(r"^\s*#{1,6}\s")


def _is_separator(line: str) -> bool:
    """A table's `|---|:--:|` row: only bars, dashes, colons and spaces, with at least one dash. Not a regex: a long line of
    spaces after a bar must not make the match quadratic."""
    stripped = line.strip()
    return "-" in stripped and not stripped.strip(" \t:|-")


def _sentences(text: str) -> list[str]:
    parts = _SENTENCE_END.split(text)  # [sentence, closing marks, sentence, closing marks, ..., sentence]
    return [parts[i] + (parts[i + 1] if i + 1 < len(parts) else "") for i in range(0, len(parts), 2)]


@dataclass(frozen=True)
class Token:
    text: str  # as written, for the trace and the retry note
    value: float  # in the unit written: "1.2M" is 1.2
    decimals: int  # the precision shown
    scale: float  # 1e6 for "M"
    percent: bool  # a %, "percent", "pct" or percentage points
    currency: bool
    multiplier: bool = False  # "3x"

    @property
    def plain_integer(self) -> bool:
        """A whole number that may count something visible (bullets, rows): never an amount, a percentage or a multiple."""
        return (
            self.decimals == 0 and self.scale == 1.0 and not self.percent and not self.currency and not self.multiplier
            and math.isfinite(self.value)  # a 400-digit "number" is no count (and `int()` of infinity raises)
        )

    @property
    def strict(self) -> bool:
        """True for a number that states more than a plain count or date part: it can only be grounded by a real value."""
        return self.percent or self.currency or self.multiplier or self.decimals > 0 or self.scale != 1.0


@dataclass(frozen=True)
class NumberCheck:
    body: str  # the body with every unit that held an ungrounded number removed
    removed: int  # sentences or table rows removed
    ungrounded: tuple[str, ...]  # the numbers found ungrounded, as written (for the retry; never shown to the user)
    matched: tuple[str, ...]  # "12,345<-q1": a number kept and the sub-question it came from (the trace's provenance)
    empty: bool  # nothing useful is left

    @property
    def clean(self) -> bool:
        return not self.removed


def numbers_in(text: str) -> list[Token]:
    """Every number written in `text`, with the precision and the unit it was written with."""
    tokens = []
    for match in _NUMBER.finditer(text):
        digits = match.group("digits").replace(",", "")
        suffix = (match.group("suffix") or "").strip().lower()
        tokens.append(Token(
            text=match.group(0).strip(), value=float(digits), decimals=len(digits.split(".")[1]) if "." in digits else 0,
            scale=_SCALES.get(suffix, 1.0), percent=suffix in _PERCENT_SUFFIXES, currency=bool(match.group("currency") or match.group("trailing")),
            multiplier=suffix == "x",
        ))
    return tokens


# the values a number may come from ---------------------------------------------------------------------------------

Ground = list[tuple[float, str]]  # (value, where it came from; a source that is only dates ends in DATE_PART, a plain one in PLAIN_PART)
DATE_ONLY_TEXTS = frozenset({"plan", "context"})  # the names of the texts `composer._ground` builds from dates
PLAIN_ONLY_TEXTS = frozenset({"reading"})  # texts the model wrote: they ground a plain count ("the last 90 days"), never a figure


DATE_PART = ":date"  # a source that is only a date: it grounds a year, a month or a day, never a percentage or an amount
# A source that only counts or names things: the number of rows, the null counts, the digits in a column name or in the model's own
# reading of the question. It grounds a plain whole number ("12 months", "top 5"), never a %, an amount, a multiple or a scaled
# figure: twelve rows are no evidence for "grew 12%".
PLAIN_PART = ":plain"
_PLAIN_SOURCES = (DATE_PART, PLAIN_PART)


VERBATIM = ":text="  # a source that backs one figure only if it is written exactly like this in a text cell: "100%" in "Hanes 100% Cotton"


def _verbatim_key(token_text: str) -> str:
    return "".join(token_text.split()).lower()


def _walk(value: Any) -> Iterator[tuple[float, str]]:
    """The numbers inside a guarded cell or summary, each with the tag of its source: a number is itself (no tag). A text cell gives
    every digit group in it as a plain number (a label such as "Customer #48213", a product such as "Levi's 501"; a date gives its
    parts): the digits of a name are no evidence for an amount or a rate. Containers are searched."""
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (int, float)):
        yield float(value), ""
    elif isinstance(value, str):
        tag = DATE_PART if _DATE_LIKE.match(value.strip()) else PLAIN_PART
        for group in _DIGIT_GROUPS.findall(value):
            yield float(group), tag
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk(item)


def _verbatim_keys(value: Any) -> Iterator[str]:
    """The figures with a unit that a text cell writes out ("100%", "$12.50", "2.5 million"), so a body that quotes the name of a
    product with such a figure in it is still backed, word for word."""
    if isinstance(value, str):
        for token in numbers_in(value):
            if token.strict:
                yield _verbatim_key(token.text)
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _verbatim_keys(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _verbatim_keys(item)


def ground_values(
    results: Mapping[str, Mapping[str, Any]], *, texts: Mapping[str, str] | None = None,
    extra: Mapping[str, Iterable[float]] | None = None,
) -> Ground:
    """Every number the answer may use, each tagged with where it came from.

    `results` are the sub-question results of the state (guarded rows, summaries, counts). `texts` are other sources written
    as text, keyed by name: the question, the plan, the locked definitions, the dates of the context. `extra` is for a
    later `derived_facts` table.
    """
    ground: Ground = []
    for sid, result in results.items():
        guarded = result.get("guarded")
        if not guarded:
            continue
        summary = guarded.get("summary") or {}
        # The cells and the min, max and sum of each numeric column are results. The row counts, the null counts and the column
        # names are metadata: they ground a plain whole number only.
        values = ((number, sid + tag) for number, tag in (*_walk(guarded.get("rows", [])), *_walk(summary.get("numeric", {}))))
        metadata = ((number, sid + PLAIN_PART) for number, _ in (
            *_walk(guarded.get("total_rows")), *_walk(summary.get("row_count")), *_walk(summary.get("nulls", {})), *_walk(summary.get("columns", [])),
        ))
        written = ((math.nan, sid + VERBATIM + key) for key in _verbatim_keys(guarded.get("rows", [])))  # the value is not used
        seen: set[tuple[float, str]] = set()
        for entry in (*values, *metadata, *written):
            if entry not in seen:
                seen.add(entry)
                ground.append(entry)
    for name, text in (texts or {}).items():
        # The plan and the context hold dates only, the model's reading holds choices it made; the other texts (the question, the
        # definitions) hold real numbers.
        suffix = DATE_PART if name in DATE_ONLY_TEXTS else PLAIN_PART if name in PLAIN_ONLY_TEXTS else ""
        ground.extend((float(group), name + suffix) for group in _DIGIT_GROUPS.findall(text or ""))
    for name, values in (extra or {}).items():
        ground.extend((float(value), name) for value in values)
    return ground


def _grounded_by(token: Token, ground: Ground) -> str | None:
    """The source of the first value the number matches, or None. Compares magnitudes, in the unit the number was written in."""
    tolerance = 0.5 * 10 ** -token.decimals + 1e-9
    written = _verbatim_key(token.text)
    for value, source in ground:
        if VERBATIM in source:  # a figure written in a text cell backs only that figure, as written
            owner, _, wanted = source.partition(VERBATIM)
            if wanted == written:
                return owner + ":text"
            continue
        if token.strict and source.endswith(_PLAIN_SOURCES):
            continue  # "15%" is not backed by the 15th of a month, or by 15 rows
        for candidate in ((value, value * 100) if token.percent else (value,)):
            if abs(abs(candidate) / token.scale - token.value) <= tolerance:
                return source
    return None


# does the answer say anything from the data (plan 12, A3) ----------------------------------------------------------

def _result_owner(source: str) -> str | None:
    """The sub-question a source belongs to when it is a value of that result: not a year or a count (`:date`, `:plain`), and not the
    question, the plan, the context or the definitions, which have no sub-question ID. A verbatim match ends in `:text`."""
    if source.endswith(_PLAIN_SOURCES):
        return None
    return source[: -len(":text")] if source.endswith(":text") else source


def states_result_value(body: str, matched: Iterable[str], results: Mapping[str, Mapping[str, Any]]) -> bool:
    """True when `body` states a value of an `ok` result: a number the check matched to such a result (`matched` is `NumberCheck.matched`),
    or a text cell of one of its rows, written out. Only whole words count, in any case."""
    ok = {sid for sid, result in results.items() if result.get("status") == "ok"}
    if any(_result_owner(entry.rpartition("<-")[2]) in ok for entry in matched):
        return True
    lowered = " ".join(body.casefold().split())
    for sid in ok:
        for row in (results[sid].get("guarded") or {}).get("rows", []):
            for value in row.values():
                if isinstance(value, str):
                    text = " ".join(value.casefold().split())
                    if len(text) >= MIN_TEXT_VALUE and re.search(rf"(?<!\w){re.escape(text)}(?!\w)", lowered):
                        return True
    return False


# the check ---------------------------------------------------------------------------------------------------------

def _check_unit(text: str, ground: Ground, exempt: frozenset[int]) -> tuple[list[str], list[str]]:
    """(matched, ungrounded) for the numbers of one sentence or table row."""
    matched: list[str] = []
    ungrounded: list[str] = []
    for token in numbers_in(text):
        if token.plain_integer and int(token.value) in exempt:
            continue
        source = _grounded_by(token, ground)
        if source:
            matched.append(f"{token.text}<-{source}")
        else:
            ungrounded.append(token.text)
    return matched, ungrounded


def check_lines(lines: Sequence[str], ground: Ground) -> list[str]:
    """The lines that have no figure the data does not back. For the text the code shows next to the answer but the model wrote:
    how a question was read, what evidence is missing. A plain whole number passes ("the last 90 days" is a choice, not a result);
    a %, an amount, a decimal or a scaled figure must be grounded, so an invented rate does not reach the user as a "reading"."""
    kept = []
    for line in lines:
        bad = [token for token in numbers_in(line) if token.strict and not _grounded_by(token, ground)]
        if not bad:
            kept.append(line)
    return kept


def _count_visible(body: str) -> frozenset[int]:
    """The whole numbers that count something in the answer itself: its bullets and its table rows."""
    bullets = sum(1 for line in body.split("\n") if re.match(r"^\s*(?:[-*+]|\d+[.)])\s+", line))
    rows = 0
    for line in body.split("\n"):
        if _TABLE_ROW.match(line) and not _is_separator(line):
            rows += 1
    return frozenset(count for count in (bullets, rows - 1) if count > 0)  # the header is not a data row; zero is no count


def check_numbers(body: str, ground: Ground, *, counted: Iterable[int] = ()) -> NumberCheck:
    """Remove the sentences and table rows of `body` that hold a number the data does not back.

    `counted` are whole numbers that count something the answer may state without a source: the number of sub-questions, the
    row counts of the results. A line's list marker is not part of the text that is checked.
    """
    exempt = _count_visible(body) | frozenset(counted)
    kept_lines: list[str] = []
    matched: list[str] = []
    ungrounded: list[str] = []
    removed = 0

    lines = body.split("\n")
    index = 0
    while index < len(lines):
        line = lines[index]
        if _TABLE_ROW.match(line):
            end = index
            while end < len(lines) and _TABLE_ROW.match(lines[end]):
                end += 1
            block, dropped = _check_table(lines[index:end], ground, exempt, matched, ungrounded)
            removed += dropped
            kept_lines.extend(block)
            index = end
            continue
        marker = _LIST_MARKER.match(line)
        prefix, rest = (marker.group(0), line[marker.end():]) if marker else ("", line)
        sentences = _sentences(rest) if rest.strip() else [rest]
        kept: list[str] = []
        for sentence in sentences:
            got, bad = _check_unit(sentence, ground, exempt)
            if bad:
                removed += 1
                ungrounded.extend(bad)
            else:
                matched.extend(got)
                kept.append(sentence)
        if kept or not rest.strip():
            kept_lines.append(prefix + " ".join(kept) if kept else line)
        index += 1

    if removed:
        kept_lines = _drop_empty_headings(kept_lines)  # a heading whose items all went would stand over nothing
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(kept_lines)).strip()
    # A short answer that lost nothing is an answer, and so is one that still states a verified figure. Only what is left of
    # a body that lost sentences, with no verified figure in it and too little text, is filler.
    empty = not text or (removed > 0 and not matched and _is_empty(text))
    return NumberCheck(text, removed, tuple(dict.fromkeys(ungrounded)), tuple(dict.fromkeys(matched)), empty)


def _check_table(
    rows: Sequence[str], ground: Ground, exempt: frozenset[int], matched: list[str], ungrounded: list[str],
) -> tuple[list[str], int]:
    """A table keeps its header and the rows whose numbers are grounded. A header with an ungrounded number, or a table with
    no data row left, goes whole."""
    header_end = 2 if len(rows) > 1 and _is_separator(rows[1]) else 1
    head, data = list(rows[:header_end]), list(rows[header_end:])
    ranks = exempt | frozenset(range(1, len(data) + 1))  # a rank column counts the rows: 1, 2, 3 ...
    got, bad = _check_unit(" ".join(head), ground, exempt)
    if bad:
        ungrounded.extend(bad)
        return [], 1
    matched.extend(got)
    kept, dropped = [], 0
    for row in data:
        got, bad = _check_unit(row, ground, ranks)
        if bad:
            ungrounded.extend(bad)
            dropped += 1
        else:
            matched.extend(got)
            kept.append(row)
    if data and not kept:
        return [], dropped + 1
    return [*head, *kept], dropped


def _drop_empty_headings(lines: list[str]) -> list[str]:
    """The lines without a heading that has nothing under it: the next non-blank line is another heading, or there is none."""
    kept: list[str] = []
    for index, line in enumerate(lines):
        if _HEADING.match(line):
            following = next((later for later in lines[index + 1:] if later.strip()), None)
            if following is None or _HEADING.match(following):
                continue
        kept.append(line)
    return kept


def _is_empty(text: str) -> bool:
    """True when what is left holds too little to be an answer. Headings alone are not an answer."""
    body = "\n".join(line for line in text.split("\n") if not _HEADING.match(line))
    return len(re.sub(r"[\s#*_>|`\-]+", "", body)) < MIN_USEFUL_CHARS
