"""Shared PII patterns: find and mask emails, phone numbers, national or tax IDs and card numbers.

Used by the tracer and the log filter (plan 02), the SQL error masking (plan 03) and the Result Guard
and Output Guard (plan 06, which adds output-only patterns). Patterns and tokens follow the Result
Guard note. Both functions are pure: no I/O, no state.

Regex masking is best effort. It is a second line behind the SQL Guard and the Result Guard, not the
first. It is tuned for few false positives: money and counts such as `1,234,567` or `$12,345.67` must
pass through, so a bare run of digits is never a phone number or an ID, and a card needs a valid
Luhn check digit.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

EMAIL = "email"
PHONE = "phone"
NATIONAL_ID = "id"
CARD = "card"

TOKENS: dict[str, str] = {
    EMAIL: "[EMAIL_MASKED]",
    PHONE: "[PHONE_MASKED]",
    NATIONAL_ID: "[ID_MASKED]",
    CARD: "[CARD_MASKED]",
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

# Priority order: when two matches overlap, the earlier type wins.
_PATTERNS: list[tuple[str, re.Pattern[str], Callable[[str], bool] | None]] = [
    (EMAIL, _EMAIL_RE, None),
    (CARD, _CARD_RE, _is_card),
    (NATIONAL_ID, _ID_RE, None),
    (PHONE, _PHONE_RE, _is_phone),
]


def scan(text: str) -> list[Finding]:
    """All PII findings in `text`, in order of position. Overlaps are resolved by type priority."""
    if not text:
        return []
    accepted: list[Finding] = []
    for kind, pattern, validate in _PATTERNS:
        for match in pattern.finditer(text):
            if validate is not None and not validate(match.group()):
                continue
            start, end = match.span()
            if kind == EMAIL:
                while text[start] == "'":  # a leading quote is SQL or prose quoting, not part of the address
                    start += 1
            if any(start < other.end and other.start < end for other in accepted):
                continue
            accepted.append(Finding(kind, start, end))
    return sorted(accepted, key=lambda finding: finding.start)


def _encodable(text: str) -> str:
    """A lone surrogate (bad paste, bad decode) cannot be written as UTF-8 and would abort the write."""
    return text.encode("utf-8", "replace").decode("utf-8")


def mask(text: str) -> str:
    """Replace every finding with its token. Running it again changes nothing.

    The result is always safe to write as UTF-8: characters that cannot be encoded become "?".
    """
    findings = scan(text)
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
