"""Shared PII patterns: find and mask emails, phone numbers, national or tax IDs and card numbers.

Used by the tracer and the log filter (plan 02), the SQL error masking (plan 03) and the Result Guard
and Output Guard (plan 06). Patterns and tokens follow the Result Guard note. Both functions are pure:
no I/O, no state.

There is one pattern list. Four patterns apply everywhere. The street address, coordinate and postal
code patterns are `output_only`: only `scan(text, extended=True)` uses them, which is what the Result Guard
(cell values) and the Output Guard (the answer) ask for. The tracer and the log filter keep the short
list, because SQL text and log lines are full of numbers next to words and a false positive there only
makes a trace harder to read.

Regex masking is best effort. It is a second line behind the SQL Guard and the Result Guard, not the
first. It is tuned for few false positives: money and counts such as `1,234,567` or `$12,345.67` must
pass through, so a bare run of digits is never a phone number or an ID, and a card needs a valid
Luhn check digit.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, NamedTuple

EMAIL = "email"
PHONE = "phone"
NATIONAL_ID = "id"
CARD = "card"
ADDRESS = "address"
COORDS = "coords"
POSTAL = "postal"

TOKENS: dict[str, str] = {
    EMAIL: "[EMAIL_MASKED]",
    PHONE: "[PHONE_MASKED]",
    NATIONAL_ID: "[ID_MASKED]",
    CARD: "[CARD_MASKED]",
    ADDRESS: "[ADDRESS_MASKED]",
    COORDS: "[COORDS_MASKED]",
    POSTAL: "[POSTAL_MASKED]",
}


@dataclass(frozen=True)
class Finding:
    """One match. Positions only: the matched text is never kept, so a finding is safe to log."""

    type: str
    start: int
    end: int


def _luhn_valid(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _is_card(match: str) -> bool:
    digits = re.sub(r"\D", "", match)
    return 13 <= len(digits) <= 19 and _luhn_valid(digits)


def _is_phone(match: str) -> bool:
    digits = re.sub(r"\D", "", match)
    return 8 <= len(digits) <= 15  # E.164 allows at most 15 digits


# Every character RFC 5322 allows in the local part. The lookbehind uses the same set, so a long run of
# these characters is tried from its first character only (linear time) and no prefix of an address is
# left outside the match (`o'brien@x.com`, `first!last@x.com`).
_ATEXT = r"[\w.!#$%&'*+/=?^`{|}~-]"
_EMAIL_RE = re.compile(rf"(?<!{_ATEXT}){_ATEXT}+@[\w-]+(?:\.[\w-]+)*\.[^\W\d_]{{2,}}")

# 13 to 19 digits: together, or in groups of 4-4-4-n or 4-6-4/5 (Amex style), joined by one kind of separator.
_CARD_RE = re.compile(
    r"(?<!\d)(?<!\d\.)(?:"
    r"\d{13,19}"
    r"|\d{4}(?P<s1>[ -])\d{4}(?P=s1)\d{4}(?P=s1)\d{1,7}"
    r"|\d{4}(?P<s2>[ -])\d{6}(?P=s2)\d{4,5}"
    r")(?!\d)(?!\.\d)"
)

# US social security number (ddd-dd-dddd) and employer tax ID (dd-ddddddd). Dashes only. Not inside a
# longer digit chain (`1-123-45-6789`, `123-45-6789-1`), but next to letters and commas it is still one:
# `SSN-123-45-6789`, `a,123-45-6789,b`.
_ID_RE = re.compile(r"(?<!\d)(?<!\d-)(?:\d{3}-\d{2}-\d{4}|\d{2}-\d{7})(?!\d)(?!-\d)")

# Needs a "+" or a "00" prefix, or the North American 3-3-4 shape with separators (and an optional leading
# 1), so a bare 10-digit count or `1,234,567` never matches. Only digits next to a number block it: letters,
# commas, dots and dashes around it are delimiters (`555-123-4567,555-123-4568`, `1-800-555-0199`, `...x12`).
_PHONE_RE = re.compile(
    r"(?<!\d)(?:"
    r"\+\d{1,3}[ .-]?(?:\(\d{1,4}\)[ .-]?)?\d{1,4}(?:[ .-]?\d{1,4}){1,4}"
    r"|00\d{1,3}[ .-](?:\(\d{1,4}\)[ .-]?)?\d{1,4}(?:[ .-]?\d{1,4}){1,4}"
    r"|(?:1[ .-])?(?:\(\d{3}\)[ .-]?|\d{3}[ .-])\d{3}[ .-]\d{4}"
    r")(?!\d)"
)

# The output-only patterns. They are tuned for PRECISION, not recall: the Output Guard blocks a whole answer on one hit, and
# a report full of action items ("3 Steps To Drive Growth") is the product's core output. An address that slips past
# these is still stopped upstream (the address columns are blocked; free text is assumed clean, A21).
#
# An address is a house number, one to four capitalized name words and a street word: "450 Oak Park Avenue", "12-14 Oak
# Street", "Unit 5/42 Wallaby Way", "12 Émile Zola Street". The regex finds the shape without regard to case (so a name in
# any alphabet is found) and `_is_address` then checks the capitals. A number glued to a thousands separator, a currency
# sign or a `#` (`$1,234 Main Street`, `Customer #48213 Park Avenue`) is money or a label, not a house number.
#
# Some street words are also ordinary English: "3 Key Actions Drive Growth", "5 Ways To Win Court Cases". Those count only
# at the end of the phrase (a comma, a closing bracket, a full stop or the end of the line follows), so "42 Wallaby Way,
# Sydney" is found and "3 Actions To Drive Revenue" is not. The cost is that "ship to 42 Wallaby Way via courier" is missed.
_STREET_WORDS = (
    "Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Terrace|Ter|Parkway|Pkwy|Highway|Hwy|Dr|Ln|Ct|Pl|Cir|Sq|Trl"
)
_STREET_WORDS_THAT_ARE_ENGLISH = "Drive|Lane|Court|Way|Place|Circle|Square|Trail|Alley"
_ORDINAL = r"\d{1,3}(?:st|nd|rd|th)"
_ADDRESS_END = r"[ \t]*(?:\Z|[\r\n,;:)\]}\"'’]|\.(?:\s|\Z))"
_ADDRESS_RE = re.compile(
    r"(?<![\d$%/#-])(?<!\d[,.])\d{1,6}(?:[-/]\d{1,6})?[A-Za-z]?\s+"  # 42, 221B, 12-14, 5/42
    rf"(?:(?:[^\W\d_][\w'’.-]*|{_ORDINAL})\s+){{1,4}}?"  # Martin Luther King Jr, 5th, Émile Zola. Lazy: the shortest reading
    rf"(?:(?:{_STREET_WORDS})(?!\w)|(?:{_STREET_WORDS_THAT_ARE_ENGLISH})(?!\w)(?={_ADDRESS_END}))",
    re.IGNORECASE,
)
# A word that makes the "address" a sentence about the business, not a street: "3 Pack Court Tee", "6 Inch Drive Bit",
# "Top 5 Best Selling Brands Drive Growth", "Next 3 Steps To Drive Sales". Real street names rarely use them.
_NOT_STREET_NAMES = frozenset(
    "pack packs pk piece pieces pc pcs pair pairs set sets count ct inch inches in button buttons pocket pockets layer layers ply "
    "brand brands category categories product products item items order orders customer customers store stores region regions "
    "segment segments channel channels department departments month months week weeks year years quarter quarters seller sellers "
    "buyer buyers user users unit units sale sales return returns reason reasons driver drivers factor factors metric metrics "
    "sku skus "
    # the little words of a sentence: no street is called "Actions To"
    "to for of and or on with by your our a an is are at from vs per via".split()
)


def _is_address(match: str) -> bool:
    tokens = match.split()
    suffix, names = tokens[-1], tokens[1:-1]
    if not suffix[0].isupper():  # "Drive" and "DRIVE", not "drive"
        return False
    for word in names:
        if word.strip(".'’").casefold() in _NOT_STREET_NAMES:
            return False
        if not (word[0].isupper() or re.fullmatch(_ORDINAL, word, re.IGNORECASE)):
            return False
    return True


# A latitude and longitude written as a pair, a value next to the word latitude or longitude, and the WKT form BigQuery
# prints for a GEOGRAPHY point. A pair needs four decimals each (`12.5, 40.25` in a table is not one) and `_is_coordinate_pair`
# tells it from the ratios, rates and coefficients that have the same shape (`0.8123, 0.7456`).
_COORD_PAIR_RE = re.compile(r"(?<![\d.])-?\d{1,3}\.\d{4,}\s*[,;/]\s*-?\d{1,3}\.\d{4,}(?!\d)(?!\.\d)")
_COORD_LABEL_RE = re.compile(r"(?i)\b(?:latitude|longitude|lat|lon|lng)\b[\s:=]{0,8}-?\d{1,3}\.\d+")  # one bounded class: two `\s*` backtrack quadratically
_COORD_POINT_RE = re.compile(r"(?i)\bPOINT\s*\(\s*-?\d{1,3}(?:\.\d+)?\s+-?\d{1,3}(?:\.\d+)?\s*\)")

# A postal code next to the place it belongs to, or after the words zip code or postal code. The letter-digit shape of a UK
# or Canadian code is NOT evidence by itself (`A1B2C3` is a SKU, `M2 3XL` a size), and two capital letters are not a state
# (`Revenue UP 12000`), so the place and a real state or province code are required.
#   Austin, TX 78701      London, SW1A 1AA      Ottawa, ON K1A 0B1      zip code 90210      postcode SW1A 1AA
_PLACE = r"[A-Z][a-z]+(?:[ -][A-Z][a-z]+){0,2}"
_US_STATES = (
    "AL|AK|AZ|AR|CA|CO|CT|DE|DC|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|"
    "SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|PR|GU|VI|AS|MP"
)
_CA_PROVINCES = "AB|BC|MB|NB|NL|NS|NT|NU|ON|PE|QC|SK|YT"
_UK_CODE = r"[A-Z]{1,2}\d[A-Z\d]?\s\d[A-Z]{2}"
_CA_CODE = r"[A-Z]\d[A-Z]\s?\d[A-Z]\d"
_POSTAL_US_RE = re.compile(rf"\b{_PLACE},\s+(?:{_US_STATES})\s+\d{{5}}(?:-\d{{4}})?(?!\d)")
_POSTAL_CA_RE = re.compile(rf"\b{_PLACE},?\s+(?:{_CA_PROVINCES})\s+{_CA_CODE}\b")
_POSTAL_UK_RE = re.compile(rf"\b{_PLACE},\s+{_UK_CODE}\b")
_POSTAL_LABEL_RE = re.compile(
    rf"(?i:\b(?:zip(?:\s*code)?|post(?:al)?\s*code)\b)[\s:#-]{{0,3}}(?:\d{{4,6}}(?:-\d{{4}})?(?!\d)|{_UK_CODE}\b|{_CA_CODE}\b)"
)


def _is_coordinate_pair(match: str) -> bool:
    parts = re.findall(r"-?(\d+)\.(\d+)", match)
    first, second = (abs(float(f"{whole}.{fraction}")) for whole, fraction in parts)
    if max(first, second) > 180 or min(first, second) > 90:  # latitude to 90, longitude to 180, either order
        return False
    # Real coordinates have either five decimals (about a metre) or a value of ten or more, as in `34.0522, -118.2437`.
    # Small values with four decimals are rates and correlations: `0.0543, 0.0612`.
    return min(len(fraction) for _, fraction in parts) >= 5 or max(first, second) >= 10


class _Pattern(NamedTuple):
    kind: str
    regex: re.Pattern[str]
    validate: Callable[[str], bool] | None = None
    output_only: bool = False


# Priority order: when two matches overlap, the earlier type wins.
_PATTERNS: list[_Pattern] = [
    _Pattern(EMAIL, _EMAIL_RE),
    _Pattern(CARD, _CARD_RE, _is_card),
    _Pattern(NATIONAL_ID, _ID_RE),
    _Pattern(PHONE, _PHONE_RE, _is_phone),
    _Pattern(ADDRESS, _ADDRESS_RE, _is_address, output_only=True),
    _Pattern(COORDS, _COORD_PAIR_RE, _is_coordinate_pair, output_only=True),
    _Pattern(COORDS, _COORD_LABEL_RE, output_only=True),
    _Pattern(COORDS, _COORD_POINT_RE, output_only=True),
    _Pattern(POSTAL, _POSTAL_US_RE, output_only=True),
    _Pattern(POSTAL, _POSTAL_CA_RE, output_only=True),
    _Pattern(POSTAL, _POSTAL_UK_RE, output_only=True),
    _Pattern(POSTAL, _POSTAL_LABEL_RE, output_only=True),
]


def scan(text: str, extended: bool = False) -> list[Finding]:
    """All PII findings in `text`, in order of position. Overlaps are resolved by type priority.

    `extended=True` adds the output-only patterns (street address, coordinates, postal code): the Result Guard
    and the Output Guard ask for them, the tracer and the log filter do not.
    """
    if not text:
        return []
    accepted: list[Finding] = []  # sorted by position and never overlapping
    for kind, pattern, validate, output_only in _PATTERNS:
        if output_only and not extended:
            continue
        accepted = _merge(accepted, _matches(text, kind, pattern, validate))
    return accepted


def _matches(text: str, kind: str, pattern: re.Pattern[str], validate: Callable[[str], bool] | None) -> list[Finding]:
    """The valid matches of one pattern, in order of position and not overlapping each other.

    A match the validator refuses is not skipped whole: the search goes on from the next character, so a real match that
    starts inside the refused one is still found."""
    found: list[Finding] = []
    position = 0
    while True:
        match = pattern.search(text, position)
        if match is None:
            return found
        start, end = match.span()
        if validate is not None and not validate(match.group()):
            position = start + 1
            continue
        if kind == EMAIL:
            while text[start] == "'":  # a leading quote is SQL or prose quoting, not part of the address
                start += 1
        found.append(Finding(kind, start, end))
        position = max(end, start + 1)


def _merge(accepted: list[Finding], fresh: list[Finding]) -> list[Finding]:
    """`accepted` and the `fresh` findings in one sorted list. A fresh finding that overlaps an accepted one is dropped, so
    the type that was tried first wins. One pass over both lists: the cost does not grow with the square of the findings."""
    merged: list[Finding] = []
    index = 0
    for finding in fresh:
        while index < len(accepted) and accepted[index].end <= finding.start:
            merged.append(accepted[index])
            index += 1
        # `accepted` is sorted and does not overlap, so only the first one that ends after this finding starts can overlap it
        if index < len(accepted) and accepted[index].start < finding.end:
            continue
        merged.append(finding)
    merged.extend(accepted[index:])
    return merged


def _encodable(text: str) -> str:
    """A lone surrogate (bad paste, bad decode) cannot be written as UTF-8 and would abort the write."""
    return text.encode("utf-8", "replace").decode("utf-8")


def mask(text: str, extended: bool = False) -> str:
    """Replace every finding with its token. Running it again changes nothing.

    The result is always safe to write as UTF-8: characters that cannot be encoded become "?".
    `extended` is the same switch as in `scan`.
    """
    findings = scan(text, extended)
    if not findings:
        return _encodable(text)
    parts: list[str] = []
    cursor = 0
    for finding in findings:
        parts.append(text[cursor:finding.start])
        parts.append(TOKENS[finding.type])
        cursor = finding.end
    parts.append(text[cursor:])
    return _encodable("".join(parts))
