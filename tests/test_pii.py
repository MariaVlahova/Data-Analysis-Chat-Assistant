import time

import pytest

from src import pii

# Typical numbers in an answer: revenue, counts, rates, dates, byte counts. None of them is personal data.
NUMBER_FIXTURE = [
    "1,234,567",
    "$12,345.67",
    "$1,234,567.89",
    "Revenue was $2,450,000 across 45,678 orders.",
    "up 12.5% from 3.14159",
    "+12.5% growth",
    "+12,345 units",
    "2024-01-15",
    "2025-10-22T09:00:00+00:00",
    "Q3 2025",
    "536870912",  # 512 MiB bytes
    "1099511627776",  # 1 TiB bytes, 13 digits, not Luhn-valid
    "2,147,483,648",
    "1,000,000,000",
    "10000000000",
    "1 234 567",
    "192.168.100.200",
    "revenue 120 130 140 150 160",
    "12013014015016",
]


@pytest.mark.parametrize("text, token", [
    ("write to canary@example.test now", "[EMAIL_MASKED]"),
    ("first.last+tag@sub.example.co.uk", "[EMAIL_MASKED]"),
    ("call +1 800 555 0199 today", "[PHONE_MASKED]"),
    ("+44 20 7946 0958", "[PHONE_MASKED]"),
    ("+359881234567", "[PHONE_MASKED]"),
    ("(415) 555-2671", "[PHONE_MASKED]"),
    ("415-555-2671", "[PHONE_MASKED]"),
    ("415.555.2671", "[PHONE_MASKED]"),
    ("415 555 2671", "[PHONE_MASKED]"),
    ("ssn 123-45-6789", "[ID_MASKED]"),
    ("ein 12-3456789", "[ID_MASKED]"),
    ("card 4111111111111111", "[CARD_MASKED]"),
    ("card 4111 1111 1111 1111", "[CARD_MASKED]"),
    ("card 4111-1111-1111-1111", "[CARD_MASKED]"),
    ("amex 3782 822463 10005", "[CARD_MASKED]"),
])
def test_each_pattern_masks_a_sample(text, token):
    masked = pii.mask(text)
    assert token in masked
    assert masked != text


def test_the_masked_text_keeps_what_surrounds_the_match():
    assert pii.mask("mail canary@example.test, thanks") == "mail [EMAIL_MASKED], thanks"
    assert pii.mask("a 415-555-2671 b 123-45-6789 c") == "a [PHONE_MASKED] b [ID_MASKED] c"


# Numbers joined to other text by a delimiter are still numbers (CSV rows, error messages, "ext", "SSN-").
@pytest.mark.parametrize("text, masked", [
    ("1-800-555-0199", "[PHONE_MASKED]"),
    ("call 1-555-123-4567 today", "call [PHONE_MASKED] today"),
    ("1.555.123.4567", "[PHONE_MASKED]"),
    ("+1-555-123-4567x12", "[PHONE_MASKED]x12"),
    ("555-123-4567ext12", "[PHONE_MASKED]ext12"),
    ("Alice,555-123-4567,NY", "Alice,[PHONE_MASKED],NY"),
    ("555-123-4567,555-123-4568", "[PHONE_MASKED],[PHONE_MASKED]"),
    ("Phone.555-123-4567", "Phone.[PHONE_MASKED]"),
    ("0049 30 123456", "[PHONE_MASKED]"),
    ("+49 30 123456", "[PHONE_MASKED]"),
    ("SSN-123-45-6789", "SSN-[ID_MASKED]"),
    ("a,123-45-6789,b", "a,[ID_MASKED],b"),
    ("ein:12-3456789;", "ein:[ID_MASKED];"),
])
def test_a_number_next_to_a_delimiter_or_a_suffix_is_still_masked(text, masked):
    assert pii.mask(text) == masked


@pytest.mark.parametrize("text", ["123-45-6789-1", "1-123-45-6789", "order 00123456", "0012345678901"])
def test_a_longer_digit_chain_is_not_cut_into_an_id_or_phone(text):
    assert pii.mask(text) == text


@pytest.mark.parametrize("address", [
    "o'brien@example.co.uk", "a&b@x.com", "first!last@x.com", "a+b=c@x.com", "q?x#y@x.com", "{a}|b~c@x.com",
])
def test_an_email_is_masked_from_its_first_character(address):
    # RFC 5322 allows these in the local part: none of them may stay behind as an unmasked prefix.
    assert pii.mask(address) == "[EMAIL_MASKED]"
    assert pii.mask(f"to {address}, thanks") == "to [EMAIL_MASKED], thanks"


def test_a_quote_around_an_address_is_left_alone_so_sql_stays_readable():
    assert pii.mask("WHERE email = 'canary@example.test'") == "WHERE email = '[EMAIL_MASKED]'"


def test_text_that_cannot_be_written_as_utf8_is_made_safe():
    masked = pii.mask("hi \ud800 there canary@example.test")
    assert masked == "hi ? there [EMAIL_MASKED]"
    masked.encode("utf-8")  # does not raise
    assert pii.mask("lone \udc80 surrogate").encode("utf-8")


@pytest.mark.parametrize("text", NUMBER_FIXTURE)
def test_revenue_and_count_numbers_are_not_pii(text):
    assert pii.scan(text) == []
    assert pii.mask(text) == text


def test_a_card_number_needs_a_valid_check_digit():
    assert pii.mask("4111 1111 1111 1112") == "4111 1111 1111 1112"  # Luhn fails: just a number
    assert pii.mask("4111111111111112") == "4111111111111112"
    assert pii.mask("4111111111111111") == "[CARD_MASKED]"


def test_a_run_of_20_digits_is_not_a_card():
    assert pii.mask("41111111111111111111") == "41111111111111111111"


@pytest.mark.parametrize("text", [
    "canary@example.test",
    "415-555-2671 and 123-45-6789 and 4111 1111 1111 1111",
    "no personal data here: 1,234,567",
    "",
])
def test_masking_is_idempotent(text):
    once = pii.mask(text)
    assert pii.mask(once) == once


def test_scan_returns_types_and_positions_never_the_matched_text():
    text = "mail canary@example.test or 415-555-2671"
    findings = pii.scan(text)
    assert [(f.type, text[f.start:f.end]) for f in findings] == [
        (pii.EMAIL, "canary@example.test"),
        (pii.PHONE, "415-555-2671"),
    ]
    assert not any(hasattr(f, "text") for f in findings)
    assert "canary" not in repr(findings)


def test_overlapping_matches_resolve_by_priority_and_never_double_mask():
    # A card number also looks like digit groups: it must come out as one card, not card plus phone.
    assert pii.mask("4111 1111 1111 1111") == "[CARD_MASKED]"
    # The email wins over anything inside it.
    assert pii.mask("415-555-2671@example.test") == "[EMAIL_MASKED]"


def test_empty_and_plain_text_pass_through():
    assert pii.scan("") == [] and pii.mask("") == ""
    assert pii.mask("Brand A sold more in March") == "Brand A sold more in March"


def test_a_very_long_input_is_masked_in_reasonable_time():
    started = time.perf_counter()
    hostile = (
        "a" * 200_000, "a@" + "b" * 100_000, "1 " * 50_000, "1-" * 50_000, "+1" * 50_000, "+" + "9" * 60_000,
        "a'" * 100_000, "00 " * 50_000, "(1)" * 50_000,
    )
    for text in hostile:
        pii.mask(text)
    assert time.perf_counter() - started < 5
