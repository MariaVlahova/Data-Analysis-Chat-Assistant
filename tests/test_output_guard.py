"""The Output Guard: the last scan before an answer is printed (plan 06, F16, NFR11, S9)."""
import json
import re
import types
from pathlib import Path

import pytest
from typer.testing import CliRunner

from src import pii
from src.cli import main as cli_main
from src.cli.main import app
from src.errors import AgentError, ErrorKind, friendly
from src.guards import output_guard
from src.guards.output_guard import BLOCKED_MESSAGE, OutputGuard
from src.observability import Tracer
from src.observability.tracing import Span

CANARY = "canary@example.test"
ADDRESS = "42 Wallaby Way"
runner = CliRunner(env={"COLUMNS": "200"})

# Strings the guard must let through. HAND-WRITTEN to look like answers: they are NOT real answers from the golden cases,
# which do not exist until plans 07 and 09. The false-positive count below says nothing about real answer text yet. When
# plan 09 has real answers, add them here: a false positive blocks a good answer.
MONEY_AND_COUNTS = [
    "1,234,567", "$12,345.67", "$1,234,567.89", "2,450", "10.5%", "-3.2%", "+12.5% growth", "12,345 units", "$0.99",
    "1.5M", "$2.4B", "536870912", "1099511627776", "2025-10-22", "2025-10-22T09:00:00+00:00", "Q3 2025", "FY25",
    "2,147,483,648", "10000000000", "1 234 567", "192.168.100.200", "120 130 140 150 160", "12013014015016",
]
ANSWERS = [
    "Revenue for your brands was $1,234,567.89 in the last 90 days, up 12.5% from the previous 90 days.",
    "You had 45,678 orders, 2,450 of them returned (5.4%). The average order was $27.03.",
    "Top categories: Jeans ($312,450), Tops & Tees ($287,100), Outerwear & Coats ($201,333).",
    "| Brand | Revenue | Orders |\n|---|---|---|\n| Brand A | $1,234.50 | 45 |\n| Brand B | $98,765.43 | 1,234 |",
    "Orders fell from 3,412 in March 2025 to 2,987 in April 2025 (-12.5%).",
    "The 501 Original Fit Jeans sold 4,310 units; the 3 Pack Court Tee sold 1,020.",
    "Customer #48213 spent $12,345.67 on your brands. Customer #90210 spent $9,870.10.",
    "Returns rose 3.2 points to 11.4%. Items shipped in 2-3 days on average; delivery took 5.5 days.",
    "Sales in the Sunset Boulevard Collection grew 8% while the Park Avenue line fell 2%.",
    "Margin was 41.2% (cost $18.40, price $31.30). That is 1.1 points above the 40.1% target.",
    "Of 1,240 rows, I am showing the top 50. 3 groups with fewer than 10 customers were hidden.",
    "Q1: $1.2M, Q2: $1.5M, Q3: $1.9M, Q4: $2.4M (total $7,000,000).",
    "Orders between 2025-01-01 and 2025-03-31 numbered 12,034, about 133.7 per day.",
    "Trace ID: 3fa9c21b",
    # Prose and headings with the shape of an address: a number, capitalized words, a street word.
    "Your top 3 Brands drive 80% of revenue. The 5 Categories trail the average, and 4 Brands place in the top 10.",
    "## Top 3 Best Selling Brands Drive Growth\n\n10 Products circle back each quarter; 2 Regions Court new buyers.",
    # A report with action items is the product's core output. These headings have the shape of an address.
    "## Recommended actions\n\n### 3 Recommended Actions To Drive Revenue\n1. Restock the 5 Top Sellers.\n2. Cut the 4 Slowest Lines.",
    "Top 5 Priorities To Drive Growth:\n- Next 3 Steps To Drive Sales\n- 5 Strategies To Drive Repeat Purchases\n- 3 Key Actions Drive Growth",
    "We recommend 3 Initiatives To Drive Volume. Our 2 Ways To Place Larger Orders, 4 Ways To Win Court Cases are on page 3.",
    "Customer #48213 Park Avenue Collection buyers spent $1,200; Customer #90210 Nike Air Max Court fans spent $900.",
]


def kinds(text: str) -> list[str]:
    return [finding.type for finding in OutputGuard().scan(text)]


@pytest.mark.parametrize("text, kind", [
    (f"Contact {CANARY} for details.", pii.EMAIL),
    ("Call +1 800 555 0199 today.", pii.PHONE),
    ("Call (415) 555-2671 today.", pii.PHONE),
    ("Their SSN is 123-45-6789.", pii.NATIONAL_ID),
    ("Card 4111 1111 1111 1111 was used.", pii.CARD),
    (f"They live at {ADDRESS}, Sydney.", pii.ADDRESS),
    ("Delivered to 350 5th Avenue.", pii.ADDRESS),
    ("Last seen at 34.0522, -118.2437.", pii.COORDS),
    ("The point was POINT(-118.2437 34.0522).", pii.COORDS),
    ("The customer is in Austin, TX 78701.", pii.POSTAL),
    ("Postcode SW1A 1AA.", pii.POSTAL),
])
def test_each_pattern_blocks_the_answer(text, kind):
    assert kinds(text) == [kind]
    with pytest.raises(AgentError) as err:
        OutputGuard().check(text)
    assert err.value.kind == ErrorKind.OUTPUT_BLOCKED and err.value.user_message == BLOCKED_MESSAGE


def test_the_whole_answer_is_blocked_not_just_the_match():
    answer = f"Revenue was $1,234. Our top buyer is reachable at {CANARY}. Orders were 2,450."
    with pytest.raises(AgentError):
        OutputGuard().check(answer)  # nothing is returned for the caller to print


def test_a_clean_answer_comes_back_unchanged():
    answer = ANSWERS[0]
    assert OutputGuard().check(answer) is answer


@pytest.mark.parametrize("text", MONEY_AND_COUNTS + ANSWERS)
def test_money_counts_percentages_and_answer_text_pass(text):
    assert OutputGuard().check(text) == text


def test_the_false_positive_count_on_the_fixture_is_zero():
    fixture = MONEY_AND_COUNTS + ANSWERS
    false_positives = [text for text in fixture if OutputGuard().scan(text)]
    # Recorded when written: 0 of 43 hand-written strings, none of them real answer text yet. The floor only stops the
    # fixture from being emptied; adding real answers (plan 09) must never need an edit of this line.
    assert false_positives == [] and len(fixture) >= 30


@pytest.mark.parametrize("text", [
    "Customer #48213", "Customer #90210 and Customer #10001", "customer Customer #123456 led", "**Customer #48213** spent $1,200",
])
def test_the_fake_id_format_is_never_flagged(text):
    assert OutputGuard().scan(text) == []


def test_a_finding_that_only_touches_a_fake_id_is_still_reported():
    # A card number that begins with the label is not "inside" it.
    assert kinds("Customer #4111 1111 1111 1111") == [pii.CARD]
    assert kinds("Customer #48213 mailed canary@example.test") == [pii.EMAIL]


@pytest.mark.parametrize("digits", ["4111111111111111", "378282246310005", "4222222222222"])
def test_a_card_number_with_the_label_prefix_is_not_a_fake_id(digits):
    # The exemption covers the digit counts the pseudonymizer issues (5 to 7) and nothing longer.
    assert kinds(f"Customer #{digits} paid") == [pii.CARD]
    with pytest.raises(AgentError):
        OutputGuard().check(f"Customer #{digits} paid")


@pytest.mark.parametrize("digits", ["48213", "482137", "4821377"])
def test_every_label_width_the_pseudonymizer_can_issue_passes(digits):
    assert OutputGuard().scan(f"Customer #{digits} spent $1,200") == []


def test_the_message_and_the_trace_id_are_what_the_plan_says():
    with pytest.raises(AgentError) as err:
        OutputGuard().check(CANARY)
    shown = friendly(err.value, "abcd1234")
    assert shown == (
        "I held back this answer because it may contain personal data. Please rephrase, or ask for totals instead.\n"
        "Trace ID: abcd1234"
    )
    assert CANARY not in shown and CANARY not in str(err.value) and CANARY not in err.value.detail


@pytest.mark.parametrize("not_text", [None, b"bytes", 42, ["list"]])
def test_an_answer_that_is_not_text_is_not_shown(not_text):
    with pytest.raises(AgentError) as err:
        OutputGuard().check(not_text)
    assert err.value.kind == ErrorKind.OUTPUT_BLOCKED


def test_a_guard_that_crashes_blocks_the_answer(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError(f"scan failed on {CANARY}")

    # Only the Output Guard's own view of the module: the tracer masks its attributes with the real one.
    monkeypatch.setattr(output_guard, "pii", types.SimpleNamespace(scan=boom, mask=pii.mask))
    with pytest.raises(AgentError) as err:
        OutputGuard().check("a perfectly clean answer")
    assert err.value.kind == ErrorKind.OUTPUT_BLOCKED and err.value.detail.startswith("RuntimeError at")
    assert CANARY not in err.value.detail


# the metrics row and the trace --------------------------------------------------------------------------------------------

@pytest.fixture
def tracer(tmp_path):
    return Tracer(tmp_path / "traces", tmp_path / "runs.jsonl")


# A name and a real ID that no pattern can find, next to the email and the address that the patterns can.
LEAKY_DRAFT = (
    f"Your leading purchaser Jane Q. Smith (Customer 4821377) is reachable at {CANARY} and lives at {ADDRESS}, Sydney. Revenue was $1,234."
)
DRAFT_WORDS = ("Jane", "Smith", "4821377", CANARY, "Wallaby", "reachable", "leading purchaser", "Revenue was")


def test_a_blocked_answer_sets_pii_at_output_and_the_trace_never_holds_the_answer(tracer):
    with pytest.raises(AgentError):
        with tracer.start_turn("hash", "conv-1", "t-1") as turn:
            OutputGuard().check(LEAKY_DRAFT)
    row = turn.row
    assert row["pii_at_output"] == 2 and row["guard_blocks"]["output"] == 1
    assert row["error_kinds"] == ["OUTPUT_BLOCKED"] and row["outcome"] == "failed" and row["success"] is False

    # Not even masked: a pattern cannot mask the name, and the user can read this trace back with /trace.
    text = tracer.trace_path(turn.trace_id).read_text(encoding="utf-8")
    for word in DRAFT_WORDS:
        assert word not in text, word
    spans = [json.loads(line) for line in text.splitlines()]
    (span,) = [s for s in spans if s["name"] == "guard.output"]
    assert "draft" not in span["attrs"]
    found = OutputGuard().scan(LEAKY_DRAFT)
    assert span["attrs"]["places"] == [f"{f.type}@{f.start}-{f.end}" for f in found] and len(found) == 2
    assert span["attrs"]["kinds"] == ["address", "email"] and span["attrs"]["findings"] == 2
    (event,) = [e for e in span["events"] if e["name"] == "guard.decision"]
    assert event["attrs"]["outcome"] == "block" and event["attrs"]["rule"] == "PII_AT_OUTPUT"


def test_a_clean_answer_leaves_the_counter_at_zero_and_stores_no_draft(tracer):
    with tracer.start_turn("hash", "conv-1", "t-1") as turn:
        OutputGuard().check(ANSWERS[0])
    assert turn.row["pii_at_output"] == 0 and turn.row["guard_blocks"]["output"] == 0
    (span,) = [json.loads(line) for line in tracer.trace_path(turn.trace_id).read_text(encoding="utf-8").splitlines()
               if '"guard.output"' in line]
    assert "draft" not in span["attrs"]
    assert [e["attrs"]["outcome"] for e in span["events"]] == ["pass"]


def test_a_crash_inside_the_guard_is_a_block_but_not_a_pii_finding(tracer, monkeypatch):
    monkeypatch.setattr(OutputGuard, "scan", lambda self, text: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(AgentError):
        with tracer.start_turn("hash", "conv-1", "t-1") as turn:
            OutputGuard().check("answer")
    assert turn.row["pii_at_output"] == 0 and turn.row["guard_blocks"]["output"] == 1


# the CLI: S9, a blocked answer is never printed -------------------------------------------------------------------------

def chat(config_path: Path, text: str):
    return runner.invoke(app, ["chat", "--user", "alice", "--config", str(config_path)], input=text)


def runs(project: Path) -> list[dict]:
    path = project / "data" / "runs.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def leaky_turn(session, text, *, trace_id, progress):
    """A Composer that got personal data through every earlier layer. Progress may stream; the answer may not."""
    progress("Looking up the numbers...")
    return OutputGuard().check(LEAKY_DRAFT)


def clean_turn(session, text, *, trace_id, progress):
    progress("Looking up the numbers...")
    return OutputGuard().check("Revenue was $12,345.67 across 2,450 orders. Customer #48213 spent the most.")


def test_a_blocked_answer_is_never_printed_and_cannot_be_read_back_with_trace(project, config_path, monkeypatch):
    monkeypatch.setattr(cli_main, "run_turn", leaky_turn)
    result = chat(config_path, "who is my top buyer?\n/trace\n/trace --raw\n/quit\n")
    assert result.exit_code == 0, result.output
    for word in DRAFT_WORDS:
        assert word not in result.output, word  # not on the screen, and not in what /trace and /trace --raw print
    assert "held back this answer" in result.output and re.search(r"Trace ID: [0-9a-f]{8}", result.output)
    assert "ALERT: personal data was found in a draft answer and blocked (2 finding(s))" in result.output
    assert "guard.output" in result.output  # the trace itself was shown: it just holds no answer text

    (row,) = runs(project)
    assert row["pii_at_output"] == 2 and row["guard_blocks"]["output"] == 1 and row["outcome"] == "failed"
    for path in (project / "data").rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            for word in DRAFT_WORDS:
                assert word.encode() not in data, f"{word} found in {path.name}"


def test_a_clean_answer_is_printed_once_after_the_scan(project, config_path, monkeypatch):
    monkeypatch.setattr(cli_main, "run_turn", clean_turn)
    result = chat(config_path, "how are sales?\n/quit\n")
    assert result.exit_code == 0, result.output
    assert result.output.count("Revenue was $12,345.67 across 2,450 orders.") == 1
    assert "ALERT" not in result.output and runs(project)[0]["pii_at_output"] == 0


# a label is not a house number; many labels cost time in proportion ----------------------------------------------------------

@pytest.mark.parametrize("text", ["Customer #48213 Park Avenue", "Customer #48213 Nike Air Max Court", "Customer #90210 Main Street"])
def test_the_digits_of_a_fake_id_are_never_a_house_number(text):
    assert OutputGuard().scan(text) == []


def test_a_real_address_next_to_a_label_is_still_found():
    assert kinds("Customer #48213 lives at 42 Oak Street, Sydney") == [pii.ADDRESS]


def test_many_labels_and_many_findings_cost_time_in_proportion_not_in_the_square():
    import time

    text = ("Customer #12345 spent $1,200; reach canary@example.test. " * 8_000)
    started = time.perf_counter()
    found = OutputGuard().scan(text)
    assert len(found) == 8_000 and time.perf_counter() - started < 2.0


# the block path is inside the same wrapper as the scan --------------------------------------------------------------------------

def boom(*args, **kwargs):
    raise ValueError("tracing fault")


@pytest.mark.parametrize("what", ["guard_decision", "turn_metrics", "span attributes"])
def test_a_failure_while_recording_a_finding_is_still_the_typed_error_with_the_usual_message(monkeypatch, what):
    if what == "guard_decision":
        monkeypatch.setattr(output_guard, "guard_decision", boom)
    elif what == "turn_metrics":
        monkeypatch.setattr(output_guard, "turn_metrics", boom)
    else:
        set_attributes = Span.set
        monkeypatch.setattr(Span, "set", lambda self, **attrs: boom() if "places" in attrs else set_attributes(self, **attrs))
    with pytest.raises(AgentError) as err:
        OutputGuard().check(f"reach {CANARY}")
    error = err.value
    assert error.kind == ErrorKind.OUTPUT_BLOCKED and error.user_message == BLOCKED_MESSAGE
    assert "ValueError at" in error.detail and CANARY not in error.detail and error.__cause__ is None


def test_a_clean_answer_whose_decision_cannot_be_recorded_is_not_shown_either(monkeypatch):
    monkeypatch.setattr(output_guard, "guard_decision", boom)
    with pytest.raises(AgentError) as err:
        OutputGuard().check("Revenue was $1,234.")
    assert err.value.kind == ErrorKind.OUTPUT_BLOCKED and err.value.detail.startswith("ValueError at")


def test_the_count_survives_a_failure_to_record_the_rest(tracer, monkeypatch):
    monkeypatch.setattr(output_guard, "guard_decision", boom)
    with pytest.raises(AgentError):
        with tracer.start_turn("hash", "conv-1", "t-1") as turn:
            OutputGuard().check(f"reach {CANARY}")
    assert turn.row["pii_at_output"] == 1  # the incident is still counted and still raises the alert
