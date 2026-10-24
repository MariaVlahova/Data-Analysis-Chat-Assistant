"""A `Customer #NNNNN` label is a name for one session (plan 06, plan 07). Its digits are a random number in the range of real
customer IDs, so code, not a prompt, keeps them out of a query and out of a question about one customer."""
import re

import pandas as pd
import pytest

from src.errors import ErrorKind
from src.guards.pseudonymizer import LABEL_IN_TEXT, SessionPseudonymizer, labels_in
from src.llm.routing import Step
from tests.agent_support import REVENUE_SQL, composer_reply, guard_reply, make_rig, sql_reply

ROWS = pd.DataFrame({"brand": ["Brand A"], "revenue": [1234.5]})
BY_CUSTOMER = "SELECT SUM(oi.sale_price) AS revenue FROM order_items oi WHERE oi.user_id = {number}"
BY_PRICE = "SELECT SUM(oi.sale_price) AS revenue FROM order_items oi WHERE oi.sale_price < {number}"


@pytest.fixture
def rig(project, fake_bigquery):
    fake_bigquery.add("revenue", df=ROWS)
    return make_rig(project, fake_bigquery=fake_bigquery)


def issue_label(rig, real_id=7001) -> str:
    """Hand out a label the way the Result Guard does, and return its digits."""
    guard = rig.agent.deps.result_guard
    return re.search(r"\d+", guard._pseudonymizer.fake_id(real_id)).group()


# the pseudonymizer knows which numbers it handed out ---------------------------------------------------------------------------

def test_a_text_that_holds_a_handed_out_number_refers_to_a_label():
    pseudonymizer = SessionPseudonymizer(seed=1)
    digits = pseudonymizer.fake_id(7001).removeprefix("Customer #")
    assert pseudonymizer.refers_to(f"WHERE u.id = {digits}") and pseudonymizer.refers_to(f"Customer #{digits}")
    assert pseudonymizer.refers_to(f"x IN ({digits}, 5)")


def test_a_number_nobody_was_given_or_a_longer_number_is_not_a_label():
    pseudonymizer = SessionPseudonymizer(seed=1)
    digits = int(pseudonymizer.fake_id(7001).removeprefix("Customer #"))
    other = digits + 1 if digits < 99999 else digits - 1
    assert not pseudonymizer.refers_to(f"WHERE u.id = {other}")
    assert not pseudonymizer.refers_to(f"WHERE x = {digits}0 OR y = 1{digits}")  # a longer number is another number


def test_only_a_whole_number_in_a_query_is_taken_for_a_label_not_the_digits_of_a_name_or_of_another_number():
    pseudonymizer = SessionPseudonymizer(seed=1)
    n = pseudonymizer.fake_id(7001).removeprefix("Customer #")
    for refers in (f"WHERE u.id = {n}", f"IN ({n}, 5)", f"= '{n}'", f"id={n};", f"Customer #{n}", "x" + chr(10) + n + chr(10)):
        assert pseudonymizer.refers_to(refers), refers
    for other in (f"col{n}", f"t{n}.x", f"{n}abc", f"1.{n}", f"{n}.5", f"{n}0", f"9{n}"):
        assert not pseudonymizer.refers_to(f"WHERE {other} > 1"), other


@pytest.mark.parametrize("text,found", [
    ("tell me more about Customer #48213", True), ("customer#123456 please", True), ("CUSTOMER # 48213", True),
    ("Customer # 4821", False), ("customer 48213", False), ("Customer #12345678", False), ("our customers rank", False),
])
def test_the_pattern_finds_a_label_as_a_person_types_it(text, found):
    assert bool(LABEL_IN_TEXT.search(text)) is found


# a query that uses a label's digits is refused by code -----------------------------------------------------------------------------

def test_a_query_that_filters_on_the_digits_of_a_label_is_refused_and_never_run(rig):
    digits = issue_label(rig)
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply(standalone="Revenue by brand"))
    rig.gateway.queue(Step.SQL_WRITE, sql_reply(BY_CUSTOMER.format(number=digits)))
    rig.gateway.queue(Step.SQL_REPAIR, sql_reply(REVENUE_SQL))
    result = rig.ask("revenue by brand")
    assert result.outcome == "refused" and result.error.kind is ErrorKind.GUARD_VIOLATION and result.error.rule_id == "SESSION_LABEL"
    assert "names for this conversation only" in result.text
    assert not rig.queries and rig.calls(Step.SQL_REPAIR) == 0  # a guard violation is never retried


def test_a_number_that_is_no_label_runs_where_it_is_no_customer_id(rig):
    digits = int(issue_label(rig))
    other = digits + 1 if digits < 99999 else digits - 1
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply(standalone="Revenue by brand"))
    rig.gateway.queue(Step.SQL_WRITE, sql_reply(BY_PRICE.format(number=other)))
    rig.gateway.queue(Step.COMPOSER, composer_reply("Revenue was 1,234.5."))
    assert rig.ask("revenue by brand").outcome == "answered"


def test_a_customer_id_filter_with_a_number_that_is_no_label_is_still_refused_r6(rig):
    digits = int(issue_label(rig))
    other = digits + 1 if digits < 99999 else digits - 1
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply(standalone="Revenue by brand"))
    rig.gateway.queue(Step.SQL_WRITE, sql_reply(BY_CUSTOMER.format(number=other)))
    result = rig.ask("revenue by brand")
    assert result.outcome == "refused" and result.error.rule_id == "PER_CUSTOMER_ROWS"
    assert not rig.queries


# a question about one customer by label is refused before any query ------------------------------------------------------------------

def test_a_question_about_a_label_is_refused_without_a_query(rig):
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply(standalone="Tell me more about Customer #48213"))
    result = rig.ask("tell me more about Customer #48213")
    assert result.outcome == "refused" and result.error.rule_id == "SESSION_LABEL" and "names for this conversation only" in result.text
    assert rig.calls(Step.SQL_WRITE) == 0 and not rig.queries


def test_a_follow_up_that_the_rewrite_turns_into_a_question_about_a_label_is_refused_too(rig):
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply(standalone="Tell me more about Customer #48213"))  # the user only said "the first one"
    result = rig.ask("tell me more about the first one")
    assert result.outcome == "refused" and result.error.rule_id == "SESSION_LABEL"
    assert rig.calls(Step.SQL_WRITE) == 0
    assert rig.last_row["outcome"] == "refused"


def test_a_label_in_a_request_for_a_saved_report_is_not_a_data_question(rig):
    """A tool turn names reports, not customers: the text of a title may hold anything the user saved."""
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply("list_reports", standalone="List reports about Customer #48213"))
    assert rig.ask("list reports about Customer #48213").outcome == "action_done"


# labels an earlier process showed (the conversation is saved on disk, plan 08) ----------------------------------------------------------------------

def test_the_numbers_of_the_labels_in_a_text_are_found_once_each_in_order():
    assert labels_in("Customer #48213 spent 500, then customer#1234567 and CUSTOMER # 77001, and Customer #48213 again") == ["48213", "1234567", "77001"]


@pytest.mark.parametrize("text", ["customer 48213", "Customer #4821", "Customer #12345678", "our customers rank 48213", ""])
def test_text_that_is_not_a_label_has_no_number(text):
    assert labels_in(text) == []


def test_labels_that_were_shown_are_taken_as_handed_out():
    pseudonymizer = SessionPseudonymizer(seed=1)
    assert pseudonymizer.retire_labels(["48213", "1234567", "77001"]) == 3
    assert pseudonymizer.refers_to("WHERE u.id = 48213") and pseudonymizer.refers_to("IN (1234567, 5)") and pseudonymizer.refers_to("= 77001")
    assert pseudonymizer.retire_labels(["48213"]) == 0  # already known


def test_what_is_not_a_label_number_is_ignored_when_retiring():
    pseudonymizer = SessionPseudonymizer(seed=1)
    assert pseudonymizer.retire_labels(["4821", "12345678", "abc", "48 213", "", None, 48213]) == 0  # type: ignore[list-item]
    assert not pseudonymizer.refers_to("WHERE u.id = 48213")


def test_a_label_taken_from_the_checkpoint_is_never_issued_to_another_customer():
    digits = SessionPseudonymizer(seed=7).fake_id(1).removeprefix("Customer #")  # the first draw of this seed
    pseudonymizer = SessionPseudonymizer(seed=7)
    pseudonymizer.retire_labels([digits])
    assert pseudonymizer.fake_id(1) != f"Customer #{digits}"
