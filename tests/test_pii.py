import re
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


# the output-only patterns (plan 06): street address, coordinates, postal code ------------------------------------------

EXTENDED_SAMPLES = [
    ("ship to 42 Wallaby Way, Sydney", "[ADDRESS_MASKED]"),
    ("350 5th Avenue", "[ADDRESS_MASKED]"),
    ("221B Baker Street", "[ADDRESS_MASKED]"),
    ("1600 Pennsylvania Ave", "[ADDRESS_MASKED]"),
    ("12 Oak Park Road", "[ADDRESS_MASKED]"),
    ("12-14 Oak Street", "[ADDRESS_MASKED]"),
    ("Unit 5/42 Wallaby Way", "Unit [ADDRESS_MASKED]"),
    ("Sydney,42 Wallaby Way", "Sydney,[ADDRESS_MASKED]"),
    ("12 Émile Zola Street", "[ADDRESS_MASKED]"),
    ("5 Martin Luther King Jr Boulevard", "[ADDRESS_MASKED]"),
    ("42 WALLABY WAY", "[ADDRESS_MASKED]"),
    ("10 State Street", "[ADDRESS_MASKED]"),
    ("34.0522, -118.2437", "[COORDS_MASKED]"),
    ("Last seen at 34.0522, -118.2437.", "Last seen at [COORDS_MASKED]."),
    ("-33.8688;151.2093", "[COORDS_MASKED]"),
    ("latitude: 34.0522", "[COORDS_MASKED]"),
    ("POINT(-118.2437 34.0522)", "[COORDS_MASKED]"),
    ("Austin, TX 78701", "[POSTAL_MASKED]"),
    ("Beverly Hills, CA 90210-1234", "[POSTAL_MASKED]"),
    ("London, SW1A 1AA", "[POSTAL_MASKED]"),
    ("Ottawa, ON K1A 0B1", "[POSTAL_MASKED]"),
    ("Ottawa ON K1A0B1", "[POSTAL_MASKED]"),
    ("postcode SW1A 1AA", "[POSTAL_MASKED]"),
    ("Postal code: K1A 0B1", "[POSTAL_MASKED]"),
    ("zip code 90210", "[POSTAL_MASKED]"),
    ("postal code: 10115", "[POSTAL_MASKED]"),
    # an address is still found when the street word is also an English word, as long as the phrase ends there
    ("42 Wallaby Way", "[ADDRESS_MASKED]"),
    ("ship to 42 Wallaby Way, Sydney", "ship to [ADDRESS_MASKED], Sydney"),
    ("(42 Wallaby Way)", "([ADDRESS_MASKED])"),
    ("lives at 42 Wallaby Way.", "lives at [ADDRESS_MASKED]."),
    ("42 Wallaby Way\nSydney", "[ADDRESS_MASKED]\nSydney"),
    ("12 Park Place Drive", "[ADDRESS_MASKED]"),
    # a valid address that sits next to words that make a wrong longer reading is still found (the shortest reading wins)
    ("ship to 42 Oak Street Brands Way", "ship to [ADDRESS_MASKED] Brands Way"),
    # coordinates: five decimals, or a value of ten or more
    ("51.5074, -0.1278", "[COORDS_MASKED]"),
    ("0.81234, 0.74561", "[COORDS_MASKED]"),
]

# Words and numbers that sit next to street words, coordinates and postal shapes in a real answer.
EXTENDED_FALSE_POSITIVES = [
    "Revenue was $1,234 Main Street Brands up 12%",
    "450 orders from Lane Bryant customers",
    "Top 10 Nike Shoes sold",
    "10 Levi's Jeans are top sellers",
    "3 Pack Court Tee and 6 Inch Drive Bit",
    # Ordinary analytic prose has the shape of an address when the street word is not capitalized, or when a heading
    # puts every word in capitals.
    "Your top 3 Brands drive 80% of revenue.",
    "The 5 Categories trail the average",
    "Top 4 Brands place in the top 10",
    "10 Products circle back",
    "Top 3 Best Selling Brands Drive Growth",
    "## 5 Fastest Growing Categories Lead The Market",
    "The 2 Regions Court new buyers",
    # Action items and headings: a number, capitalized words, and a verb that is also a street word. The product's core
    # output is a report with items like these, and one false positive blocks the whole report.
    "## 3 Recommended Actions To Drive Revenue",
    "Top 5 Priorities To Drive Growth",
    "Next 3 Steps To Drive Sales",
    "5 Strategies To Drive Repeat Purchases",
    "We recommend 3 Initiatives To Drive Volume.",
    "3 Key Actions Drive Growth",
    "2 Ways To Place Orders, 4 Ways To Win Court Cases.",
    "5 Ways To Reach Main Street Shoppers",  # an unambiguous street word: only the little words of the sentence tell
    "Customer #48213 Park Avenue",  # a label is not a house number
    "Customer #48213 Nike Air Max Court",
    # two capital letters after a word are not a state, and a letter-digit shape is not a postcode
    "Revenue UP 12000 vs last year", "Sales FY 25000", "Orders EU 15000", "Texas TX 12000",
    "Revenue, UP 12000 vs last year", "Sales, FY 25000", "Orders, EU 15000", "Markets, GB 25000", "Regions, ZZ 12345",
    "Austin, ON 78701",  # ON is a Canadian province, not a US state, and 78701 is not a Canadian code
    "SKU A1B2C3 sold well", "Size M2 3XL", "Model X9 4AB",
    # rates, ratios and correlations look like a latitude and a longitude
    "Correlation coefficients were 0.8123, 0.7456 and 0.6012.", "Return rates: 0.0543, 0.0612", "Conversion 0.1234; 0.5678",
    # known gaps, kept here so that a change of this behaviour is a decision and not an accident
    "123 main st",
    "ship to 42 Wallaby Way via courier",  # a street word that is also English counts only at the end of the phrase
    "SW1A 1AA", "K1A 0B1",  # a postcode on its own, without a place or a label
    "12.5, 40.25",
    "0.1234 and 0.5678 are rates",
    "the long 12.5 day wait",
    "March 2025 sales",
    "Q3 2025 revenue 1,234,567",
    "FY25 plan, H2 2024 actuals",
    "Customer #48213 spent $1,200",
    "a 10000 order backlog, 12345 items",
]


@pytest.mark.parametrize("text, token", EXTENDED_SAMPLES)
def test_an_output_only_pattern_is_found_when_asked_for(text, token):
    assert pii.mask(text, extended=True).count(token) == 1
    assert pii.scan(text) == [] and pii.mask(text) == text  # the tracer and the log filter keep the short list


@pytest.mark.parametrize("text", EXTENDED_FALSE_POSITIVES + NUMBER_FIXTURE)
def test_money_counts_dates_and_names_next_to_street_words_are_not_flagged(text):
    assert pii.scan(text, extended=True) == []


def test_the_extended_scan_still_finds_the_four_usual_types_and_masking_is_idempotent():
    text = "mail canary@example.test, 415-555-2671, 42 Wallaby Way, Austin, TX 78701"
    masked = pii.mask(text, extended=True)
    assert masked == "mail [EMAIL_MASKED], [PHONE_MASKED], [ADDRESS_MASKED], [POSTAL_MASKED]"
    assert pii.mask(masked, extended=True) == masked


def test_a_coordinate_pair_needs_values_a_map_can_hold():
    assert pii.scan("123.4567, 12.3456", extended=True) == [pii.Finding(pii.COORDS, 0, 17)]  # a longitude then a latitude
    assert pii.scan("191.4567, 191.3456", extended=True) == []  # no latitude or longitude is beyond 180
    assert pii.scan("95.1234, 95.5678", extended=True) == []  # and one of the two must be a latitude, which stops at 90


@pytest.mark.parametrize("gap", [" ", "\t", " \t", "\n", " "])
@pytest.mark.parametrize("opening", [
    "lat", "latitude", "lon", "lng", "longitude", "POINT", "POINT(", "POINT(1", "POINT(1 2", "zip", "zip code", "postal", "post code",
    "1", "1 Main", "42 Wallaby", "42 Wallaby Way", "5/42", "12-14", "34.0522,", "34.0522, 118", "Austin,", "Austin, TX",
    "London,", "Ottawa, ON", "SW1A", "K1A",
])
def test_a_long_run_of_whitespace_after_an_opening_does_not_stall_any_pattern(opening, gap):
    # `\s*[:=]?\s*` backtracked quadratically: 16,000 spaces after "lat" took almost 4 seconds. A regex cannot be interrupted,
    # so each pattern has to be linear on its own. Every opening that a pattern can start with is tried with every gap.
    started = time.perf_counter()
    pii.scan(opening + gap * 50_000 + "x", extended=True)
    pii.scan(opening + gap * 50_000, extended=True)
    assert time.perf_counter() - started < 1.0


def test_the_output_only_patterns_run_in_reasonable_time_on_hostile_input():
    started = time.perf_counter()
    hostile = (
        "1 " + "A" * 100_000, "1 A " * 50_000, "1" * 100_000, "1." + "1" * 100_000, "1.1111," * 20_000, "Aa " * 50_000,
        "Aa, TX " * 20_000, "A1 " * 50_000, "zip " * 50_000, "latitude " * 20_000, "POINT(" + "1 " * 50_000, "1 St " * 30_000,
        "1 Main " * 30_000,
    )
    for text in hostile:
        pii.mask(text, extended=True)
    assert time.perf_counter() - started < 5


# overlaps: one pass, not a comparison of every finding with every other ---------------------------------------------------------

def _naive_merge(accepted, fresh):
    """What the plain version did: keep a fresh finding unless it overlaps one already accepted."""
    kept = list(accepted)
    for finding in fresh:
        if not any(finding.start < other.end and other.start < finding.end for other in kept):
            kept.append(finding)
    return sorted(kept, key=lambda finding: finding.start)


def _non_overlapping(rng, count, span=400):
    """Random findings that do not overlap each other, sorted by position."""
    points = sorted(rng.sample(range(span), 2 * count))
    return [pii.Finding(pii.EMAIL, points[i], points[i + 1]) for i in range(0, 2 * count, 2)]


def test_the_merge_gives_what_the_comparison_of_every_pair_gave():
    import random

    rng = random.Random(7)
    for _ in range(500):
        accepted = _non_overlapping(rng, rng.randint(0, 30))
        fresh = _non_overlapping(rng, rng.randint(0, 30))
        assert pii._merge(accepted, fresh) == _naive_merge(accepted, fresh)


def test_the_earlier_type_wins_an_overlap_and_an_adjacent_finding_is_kept():
    mail, phone, address = pii.Finding(pii.EMAIL, 10, 20), pii.Finding(pii.PHONE, 15, 25), pii.Finding(pii.ADDRESS, 20, 30)
    assert pii._merge([mail], [phone]) == [mail]
    assert pii._merge([mail], [address]) == [mail, address]  # touching is not overlapping
    assert pii._merge([], [mail, address]) == [mail, address]  # nothing accepted yet: the fresh findings all stay
    assert pii._merge([mail, address], []) == [mail, address]
    assert pii._merge([address], [pii.Finding(pii.PHONE, 0, 12)]) == [pii.Finding(pii.PHONE, 0, 12), address]  # goes in before


@pytest.mark.parametrize("unit", ["1.1111,", "A1 1AA ", "Austin, TX 78701 ", "42 Wallaby Way, ", "34.0522, -118.2437; ", "x@y.com ", "415-555-2671 "])
def test_many_findings_cost_time_in_proportion_not_in_the_square(unit):
    # 20,000 findings. Comparing each with all the earlier ones took 2.7 to 10 seconds; the regexes alone take about 0.1.
    started = time.perf_counter()
    pii.scan(unit * 20_000, extended=True)
    assert time.perf_counter() - started < 2.0


def test_a_match_the_validator_refuses_does_not_hide_one_that_starts_inside_it():
    # The search goes on from the next character after a refusal, not from the end of the refused match.
    pattern = re.compile(r"\d(?: \d)*")
    refuse_ones = lambda text: "1" not in text  # noqa: E731 - "1 2 3" is refused, "2 3" is fine
    found = pii._matches("x 1 2 3 y", pii.PHONE, pattern, refuse_ones)
    assert [(f.start, f.end) for f in found] == [(4, 7)]
    assert pii._matches("x 1 y", pii.PHONE, pattern, refuse_ones) == []
