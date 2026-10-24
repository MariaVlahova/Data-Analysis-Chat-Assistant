"""The numeric grounding check (plan 07, F6, S11): every number in the answer comes from a query result."""
import pandas as pd
import pytest

from src.agent.numeric_check import NUMERIC_NOTE, check_numbers, ground_values, numbers_in
from src.llm.routing import Step
from tests.agent_support import composer_reply, guard_reply, make_rig, sql_reply

GUARDED = {
    "rows": [
        {"month": "2026-09", "revenue": 1234567.891, "items": 4210, "share": 0.125, "label": "Customer #48213"},
        {"month": "2026-08", "revenue": 998877.5, "items": 3900, "share": 0.4, "label": "Customer #11875"},
    ],
    "summary": {"row_count": 2, "columns": ["month", "revenue", "items"], "numeric": {
        "revenue": {"min": 998877.5, "max": 1234567.891, "sum": 2233445.391}, "items": {"min": 3900, "max": 4210, "sum": 8110}}},
    "total_rows": 2, "notices": [],
}
GROUND = ground_values({"q1": {"guarded": GUARDED}}, texts={"question": "What was revenue in September 2026? Show the top 5."})


def check(body, **options):
    return check_numbers(body, GROUND, **options)


# what counts as grounded ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Revenue was 1,234,568.",  # rounded to the precision shown
    "Revenue was $1,234,567.89.",
    "Revenue was $1.23M.", "Revenue was $1.2 million.", "Items sold: 4,210.", "About 4.2K items.", "That is 12.5% of the total.",
    "A 40% share.", "Total of 8,110 items over 2 months.", "Revenue peaked at 1234567.9.",
    "In 2026-09 and 2026-08.", "In September 2026.", "The top 5 customers.", "Customer #48213 bought the most.",
])
def test_a_number_the_data_backs_is_kept(text):
    result = check(text)
    assert result.clean and result.body == text, result


@pytest.mark.parametrize("text", [
    "Revenue grew 18%.", "Revenue was 1,300,000.", "Revenue was $1.5M.", "A share of 30%.", "Items sold: 4,300.",
    "Revenue was 1,234,567.123 exactly.",  # more precision than the data has
    "Customer #99999 bought the most.", "In 2031.",
])
def test_a_number_the_data_does_not_back_removes_its_sentence(text):
    result = check(f"First, a sentence without numbers. {text} And a last one without numbers.")
    assert result.removed == 1 and text not in result.body
    assert "First, a sentence without numbers." in result.body and "And a last one without numbers." in result.body


def test_a_percent_or_currency_number_is_never_exempt_even_when_a_count_would_be():
    body = "- First\n- Second\n- Third\n- Fourth\n- Fifth\n- Sixth\n\nA 6% rise and $6 each."
    result = check(body)
    assert result.removed == 1 and "6%" not in result.body and "$6" not in result.body  # six bullets exempt "6", but not "6%" or "$6"
    assert check("- a\n- b\n- c\n- d\n- e\n- f\n\nThere are 6 points.").clean


def test_a_whole_number_that_counts_something_visible_is_allowed():
    assert check("- one\n- two\n- three\n\nThere are 3 insights.").clean  # the number of bullets
    table = "| Month | Revenue |\n|---|---|\n| 2026-09 | 1,234,568 |\n| 2026-08 | 998,878 |\n\nTwo months, 2 rows."
    assert check(table).clean  # the table's data rows
    assert check("I ran 3 queries.", counted=[3]).clean and not check("I ran 7 queries.", counted=[3]).clean  # the plan's sub-questions


def test_list_markers_headings_and_table_syntax_are_not_numbers():
    body = "## 1. Summary\n1. Revenue was 1,234,568.\n2) Items were 4,210.\n\n| Month | Revenue |\n|:---|---:|\n| 2026-09 | 1,234,568 |"
    assert check(body).clean


def test_words_that_contain_digits_are_not_numbers():
    assert [t.text for t in numbers_in("Q1 grew, the 3rd quarter, 10pm, COVID19 and 1,234.5 or $2.5 million, 12K, 12.5%, 2025-2026, -4%")] == [
        "1,234.5", "$2.5 million", "12K", "12.5%", "2025", "2026", "-4%"]


def test_a_table_row_with_an_ungrounded_number_goes_but_its_table_stays():
    body = "| Month | Revenue |\n|---|---|\n| 2026-09 | 1,234,568 |\n| 2026-08 | 777,000 |\n| 2026-09 | 998,878 |"
    result = check(body)
    assert result.removed == 1 and "777,000" not in result.body and "1,234,568" in result.body and "998,878" in result.body
    assert result.body.startswith("| Month | Revenue |\n|---|---|")


def test_a_table_with_no_data_row_left_goes_whole():
    result = check("Figures:\n\n| Month | Revenue |\n|---|---|\n| 2026-01 | 777,000 |\n| 2026-02 | 888,000 |\n\nThat is all.")
    assert "Month" not in result.body and result.removed == 3 and "That is all." in result.body


def test_a_list_item_loses_only_the_sentence_with_the_bad_number():
    result = check("- Revenue was 1,234,568. Growth was 18%. It was a good month.")
    assert result.body == "- Revenue was 1,234,568. It was a good month."


def test_nothing_useful_left_is_reported():
    assert check("Revenue grew 18%. Items grew 25%.").empty
    assert not check("Brand A earned 1,234,568.").empty and not check("Fine, thanks.").empty  # a short answer that lost nothing is an answer
    assert check("").empty
    assert check("Here are the key insights: revenue grew 18%.").empty  # the filler that is left states no verified figure
    assert not check("Revenue was 1,234,568. Revenue grew 18%.").empty  # a short remainder that still states a verified figure is useful


def test_the_numbers_found_ungrounded_are_listed_once_for_the_retry():
    assert check("Up 18%. Up 18% again. Down 7%.").ungrounded == ("18%", "7%")


def test_each_kept_number_names_the_sub_question_it_came_from():
    result = check("Revenue was 1,234,568 on 4,210 items in 2026.")
    assert "1,234,568<-q1" in result.matched and "4,210<-q1" in result.matched and "2026<-q1:date" in result.matched  # a date part is marked as one


def test_ground_values_take_numbers_from_cells_text_cells_summaries_and_texts_but_not_flags():
    ground = ground_values(
        {"q1": {"guarded": {"rows": [{"a": 7, "b": "Order 99 of 2026-09-01T00:00:00", "c": True, "d": None}], "summary": {}, "total_rows": 1}},
         "q2": {"status": "failed", "guarded": None}},
        texts={"question": "top 5", "empty": ""}, extra={"derived": [3.5]},
    )
    values = {(round(v, 2), src) for v, src in ground}
    # a number is itself; the digits of a text cell are plain numbers (they are no evidence for an amount or a rate)
    assert {(7.0, "q1"), (99.0, "q1:plain"), (2026.0, "q1:plain"), (9.0, "q1:plain"), (1.0, "q1:plain"), (5.0, "question"), (3.5, "derived")} <= values
    assert not any(v == 1.0 and src == "q1" and False for v, src in ground)  # True is a flag, not the number 1 (1 comes from the date)


# inside the graph -----------------------------------------------------------------------------------------------------

ROWS = pd.DataFrame({"brand": ["Brand A", "Brand B"], "revenue": [1234.5, 999.0]})


@pytest.fixture
def rig(project, fake_bigquery):
    fake_bigquery.add("revenue", df=ROWS)
    return make_rig(project, fake_bigquery=fake_bigquery)


def ask(rig, body, **options):
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply(standalone="Revenue by brand"))
    rig.gateway.queue(Step.SQL_WRITE, sql_reply())
    rig.gateway.queue(Step.COMPOSER, composer_reply(body), *options.get("more", ()))
    return rig.ask("revenue by brand")


def test_a_hallucinated_number_is_removed_and_the_answer_says_so(rig):
    result = ask(rig, "Brand A earned 1,234.5 and Brand B earned 999. Revenue grew 18% last quarter.")
    assert result.outcome == "partial"
    assert "Brand A earned 1,234.5" in result.text and "18%" not in result.text
    assert NUMERIC_NOTE in result.text and "Limited data" in result.text
    assert rig.last_row["guard_blocks"]["numeric"] == 1


def test_a_correct_rounded_number_passes_and_nothing_is_flagged(rig):
    result = ask(rig, "Brand A earned about $1.2K and Brand B 999.0, a total of 2,233.5.")
    assert result.outcome == "answered" and NUMERIC_NOTE not in result.text and rig.last_row["guard_blocks"]["numeric"] == 0


def test_the_trace_records_which_sub_question_each_kept_number_came_from(rig):
    ask(rig, "Brand A earned 1,234.5.")
    events = [e for s in rig.spans() for e in s["events"] if e["name"] == "numeric_check"]
    assert len(events) == 1 and events[0]["attrs"]["matched"] == ["1,234.5<-q1"] and events[0]["attrs"]["removed"] == 0


def test_when_the_whole_answer_was_made_of_ungrounded_figures_the_figures_come_from_the_data_instead(rig):
    result = ask(rig, "Revenue grew 18%. Orders grew 25%. Returns fell 7%.")
    assert result.outcome == "partial" and "here are the figures I found, straight from the data" in result.text
    assert "| Brand A | 1234.5 |" in result.text and "18%" not in result.text


def test_the_composer_is_asked_once_by_default(rig):
    ask(rig, "Revenue grew 18%.")
    assert rig.calls(Step.COMPOSER) == 1


def test_with_numeric_retry_the_composer_is_asked_again_with_the_rejected_numbers_listed(project, fake_bigquery):
    fake_bigquery.add("revenue", df=ROWS)
    rig = make_rig(project, fake_bigquery=fake_bigquery, config="composer:\n  numeric_retry: 1\n")
    result = ask(rig, "Brand A earned 1,234.5. Revenue grew 18%.", more=[composer_reply("Brand A earned 1,234.5 and Brand B 999.")])
    assert rig.calls(Step.COMPOSER) == 2 and "18%" in rig.gateway.calls_for(Step.COMPOSER)[1].contents
    assert result.outcome == "answered" and "Brand B 999" in result.text and NUMERIC_NOTE not in result.text


def test_a_retry_that_is_no_better_keeps_the_first_check(project, fake_bigquery):
    fake_bigquery.add("revenue", df=ROWS)
    rig = make_rig(project, fake_bigquery=fake_bigquery, config="composer:\n  numeric_retry: 1\n")
    result = ask(rig, "Brand A earned 1,234.5. Revenue grew 18%.", more=[composer_reply("Brand A earned 1,234.5. It grew 18%. Costs rose 9%.")])
    assert result.outcome == "partial" and "18%" not in result.text and "9%" not in result.text and NUMERIC_NOTE in result.text


def test_a_phone_number_the_model_writes_is_dropped_by_the_numeric_check_before_the_output_guard_sees_it(rig):
    result = ask(rig, "Brand A earned 1,234.5. Call 555-867-5309 for details.")
    assert result.outcome == "partial" and "5309" not in result.text and "Brand A earned 1,234.5" in result.text
    assert rig.last_row["pii_at_output"] == 0 and rig.last_row["guard_blocks"]["numeric"] == 1  # the first layer caught it; the second saw nothing


def saved_title(rig):
    return rig.agent.graph.get_state({"configurable": {"thread_id": rig.conversation_id}}).values["last_answer"]["title"]


def test_a_title_with_a_figure_the_data_does_not_back_is_dropped_and_the_question_names_the_report(rig):
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply(standalone="Revenue by brand"))
    rig.gateway.queue(Step.SQL_WRITE, sql_reply())
    rig.gateway.queue(Step.COMPOSER, composer_reply("Brand A earned 1,234.5.", "Revenue grew 18% last quarter"))
    rig.ask("revenue by brand")
    assert saved_title(rig) == "Revenue by brand"  # the first 60 characters of the question, not the invented figure


def test_a_title_with_a_verified_figure_is_kept(rig):
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply(standalone="Revenue by brand"))
    rig.gateway.queue(Step.SQL_WRITE, sql_reply())
    rig.gateway.queue(Step.COMPOSER, composer_reply("Brand A earned 1,234.5.", "Revenue of 1,234.5 for Brand A"))
    rig.ask("revenue by brand")
    assert saved_title(rig) == "Revenue of 1,234.5 for Brand A"


def test_a_title_that_holds_personal_data_is_never_stored_even_when_it_is_not_shown(rig):
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply(standalone="Revenue by brand"))
    rig.gateway.queue(Step.SQL_WRITE, sql_reply())
    rig.gateway.queue(Step.COMPOSER, composer_reply("Brand A earned 1,234.5.", "Report for jane.roe@example.test"))
    result = rig.ask("revenue by brand")
    assert result.outcome == "answered" and "jane.roe" not in result.text  # a simple turn does not show the title
    assert saved_title(rig) == "Revenue by brand"
    checkpoints = [s.values for s in rig.agent.graph.get_state_history({"configurable": {"thread_id": rig.conversation_id}})]
    assert not any("jane.roe" in str(values) for values in checkpoints)


def test_a_period_the_answer_itself_states_as_its_reading_may_be_restated_in_the_body(rig):
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply(standalone="How is revenue doing recently?"))
    rig.gateway.queue(Step.SQL_WRITE, sql_reply(interpretation="Recently means the last 30 days."))
    rig.gateway.queue(Step.COMPOSER, composer_reply("In the last 30 days Brand A earned 1,234.5 and Brand B 999."))
    result = rig.ask("how is revenue doing recently?")
    assert result.outcome == "answered" and "In the last 30 days Brand A earned 1,234.5" in result.text
    assert "Recently means the last 30 days." in result.text  # the same words are in the envelope
    assert rig.last_row["guard_blocks"]["numeric"] == 0


def test_a_period_nobody_stated_is_still_removed(rig):
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply(standalone="How is revenue doing recently?"))
    rig.gateway.queue(Step.SQL_WRITE, sql_reply(interpretation="Revenue is the sum of item sale prices."))
    rig.gateway.queue(Step.COMPOSER, composer_reply("Brand A earned 1,234.5. Over the last 45 days Brand B grew faster."))
    result = rig.ask("how is revenue doing recently?")
    assert "45 days" not in result.text and "Brand A earned 1,234.5" in result.text


# abbreviated units, date parts, structure and robustness (review findings) -----------------------------------------------------

@pytest.mark.parametrize("text", [
    "Revenue reached $15m this quarter.", "Revenue grew 3x versus last year.", "Share moved 17pts.", "It rose 20pct.", "About 5mn items.",
    "It was .5% of sales.", "Revenue hit $1.2bn.", "A rise of 17.3 % overall.", "Up 17 percent.", "Margin of 7 pp.",
])
def test_an_invented_figure_with_an_abbreviated_unit_is_found_and_removed(text):
    result = check(f"Fine sentence one. {text} Fine sentence two.")
    assert result.removed == 1 and text not in result.body, text


@pytest.mark.parametrize("text,written", [
    ("A total of $1.2M.", "$1.2M"), ("A total of $1.2m.", "$1.2m"), ("A total of $1.23mn.", "$1.23mn"), ("Up 12.5 %.", "12.5 %"),
])
def test_the_same_units_still_pass_when_the_data_backs_them(text, written):
    assert [t.text for t in numbers_in(text)] == [written] and check(text).clean


def test_a_text_that_only_looks_like_a_unit_is_not_a_number():
    assert [t.text for t in numbers_in("10pm 3rd 2fast")] == []


def test_a_date_part_grounds_a_year_or_a_day_but_never_a_percentage_or_an_amount():
    dated = ground_values({"q1": {"guarded": {"rows": [{"day": "2026-03-14"}], "summary": {}, "total_rows": 1}}})
    assert check_numbers("In 2026 and on the 14th of March, 14 days.", dated).clean  # a year and a day are what a date holds
    for text in ("Returns rose to 15%.", "It cost $14.", "That is 14.5 of them.", "A rise of 9x.", "About 14K."):
        assert check_numbers(text, dated).removed == 1, text


def test_a_heading_whose_items_were_all_removed_goes_too():
    body = "## Summary\nBrand A earned 1,234,568.\n\n## Action items\n- Grow it by 10%.\n- Do it within 30 days.\n\n## Next steps\n- Review 1,234,568."
    result = check(body)
    assert "Action items" not in result.body and "## Summary" in result.body and "## Next steps" in result.body and result.removed == 2


def test_a_sentence_that_ends_in_closing_marks_is_split_after_them():
    body = "**Brand A earned $1,234,568 (55.3% of the total).** Brand B earned $998,878."
    result = check(body)
    assert result.removed == 1 and result.body == "Brand B earned $998,878." and "Brand A" not in result.body
    kept = check('He said "revenue was 1,234,568." Then he left.')
    assert kept.clean and kept.body == 'He said "revenue was 1,234,568." Then he left.'


def test_a_rank_column_counts_the_rows_and_does_not_cost_them_their_place():
    table = "| Rank | Brand | Revenue |\n|---|---|---|\n| 1 | A | 1,234,568 |\n| 2 | B | 998,878 |\n| 3 | C | 1,234,568 |\n| 4 | D | 998,878 |"
    assert check(table).clean
    assert check(table.replace("| 3 |", "| 7 |")).removed == 1  # a number that is no rank and no value is still removed


def test_an_absurdly_long_number_does_not_crash_the_check():
    result = check("Revenue was " + "9" * 400 + " in total. Another fine sentence follows here.")
    assert result.removed == 1 and "Another fine sentence" in result.body


def test_a_long_run_of_spaces_after_a_table_bar_is_checked_in_linear_time():
    import time

    started = time.perf_counter()
    check("| " + " " * 20000 + "x")
    check("|" + " " * 20000)
    assert time.perf_counter() - started < 1.0


# counts are no evidence for a rate, an amount or a scaled figure (review findings) ------------------------------------------------

TWELVE_ROWS = {
    "rows": [{"month": f"2025-{month:02d}", "revenue": 500.0 + 11 * month} for month in range(1, 13)],
    "summary": {
        "row_count": 12, "columns": ["month", "revenue"], "nulls": {"month": 0, "revenue": 0},
        "numeric": {"revenue": {"min": 511.0, "max": 632.0, "sum": 6858.0}},
    },
    "total_rows": 12, "notices": [],
}
COUNTS_ONLY = ground_values({"q1": {"guarded": TWELVE_ROWS}})


@pytest.mark.parametrize("claim", [
    "Revenue grew 12% year over year.", "Churn was 12%.", "Revenue rose to $12 per order.", "Revenue was up 12x.", "Revenue grew 0%.",
    "Revenue reached 12 million.", "Revenue was 12.0 on average.",
])
def test_a_row_count_or_a_null_count_never_backs_a_percentage_an_amount_or_a_scaled_figure(claim):
    result = check_numbers(f"A sentence without numbers. {claim} Another one without numbers.", COUNTS_ONLY)
    assert result.removed == 1 and claim not in result.body, claim


def test_a_count_still_backs_a_plain_whole_number_and_a_real_value_still_backs_a_figure():
    assert check_numbers("The result has 12 rows, one for each of 12 months, and 0 gaps.", COUNTS_ONLY).clean
    assert check_numbers("Revenue ranged from $511 to $632.", COUNTS_ONLY).clean  # min and max of a column are results
    assert check_numbers("Revenue totalled $6,858 over the year.", COUNTS_ONLY).clean


# a unit in any case, and a currency written after the figure (review findings) -----------------------------------------------------

ROW_COUNT_TWO = ground_values({"q1": {"guarded": {"rows": [], "summary": {"row_count": 2}, "total_rows": 2}}})


@pytest.mark.parametrize("text", [
    "Revenue was 2 Million.", "Revenue was $2 Million.", "Revenue was $2.0 Million.", "It was 2 Thousand.", "It was 2 Billion.",
    "Growth was 2 Percent.", "Growth was 2 PERCENT.", "It was 2X.", "Revenue Hit $2 Million", "REVENUE WAS 2 MILLION",
])
def test_a_unit_in_any_case_is_still_a_unit(text):
    assert check_numbers(text, ROW_COUNT_TWO).removed == 1, text


@pytest.mark.parametrize("text", [
    "Revenue was 12 USD.", "Revenue was 12 dollars.", "Revenue was 1,234.5USD.", "It cost 12 EUR.", "It cost 12 euros.",
    "It cost 12 Dollars.", "It cost 12€.",
])
def test_a_currency_written_after_the_figure_makes_it_an_amount(text):
    dates_only = ground_values({}, texts={"context": "2026-12-12"})  # a month and a day of 12, and no amount
    assert check_numbers(text, dates_only).removed == 1, text


def test_the_token_of_a_figure_with_a_trailing_currency_is_the_whole_figure():
    (token,) = numbers_in("Revenue was 1,234.5USD.")
    assert (token.text, token.value, token.currency) == ("1,234.5USD", 1234.5, True)
    assert numbers_in("12 dollars")[0].currency and not numbers_in("12 dolls")[0].currency


def test_a_trailing_currency_is_not_read_into_words_that_start_like_one():
    assert [(t.text, t.currency) for t in numbers_in("5 usdx 6 eurostar 7 dollarstore")] == [("5", False), ("6", False), ("7", False)]


# the model's reading is not evidence, and is checked before it is shown (review findings) --------------------------------------------

def test_a_number_in_the_reading_grounds_a_plain_count_but_never_a_rate():
    ground = ground_values({}, texts={"reading": "Assumes revenue grew 45% as the baseline. Recent means the last 90 days."})
    assert check_numbers("Over the last 90 days.", ground).clean
    assert check_numbers("A baseline of 45% held.", ground).removed == 1


def test_a_reading_line_with_a_figure_nothing_backs_is_left_out_and_a_plain_choice_stays():
    from src.agent.numeric_check import check_lines

    lines = ["Recent means the last 90 days.", "Assumes revenue grew 45% as the baseline.", "Orders above $50 only.", "Period: 2026-09-01 to 2026-09-30."]
    ground = ground_values({}, texts={"question": "revenue of the last 90 days", "sql": "WHERE oi.sale_price > 50"})
    assert check_lines(lines, ground) == [lines[0], lines[2], lines[3]]  # $50 is the filter the query used


def test_an_invented_rate_in_the_planners_assumptions_reaches_neither_the_reading_nor_the_body(rig):
    from tests.agent_support import plan_reply

    rig.gateway.queue(Step.INPUT_GUARD, guard_reply("why_question", "Why did revenue change?"))
    rig.gateway.queue(Step.PLANNER, plan_reply("Revenue by brand", assumptions=["Assumes revenue grew 45% as the baseline.", "Recent means the last 90 days."]))
    rig.gateway.queue(Step.SQL_WRITE, sql_reply(interpretation="Revenue is the sum of item sale prices."))
    rig.gateway.queue(Step.EVIDENCE_CHECK, {"enough": True, "missing": []})
    rig.gateway.queue(Step.COMPOSER, composer_reply("Brand A earned 1,234.5. Revenue grew 45% over the baseline."))
    result = rig.ask("why did revenue change?")
    assert "45%" not in result.text, result.text  # not in the reading, and the body sentence that repeated it is gone
    assert "Recent means the last 90 days." in result.text and "Brand A earned 1,234.5" in result.text


def test_an_invented_figure_in_the_missing_evidence_is_not_shown(rig):
    from tests.agent_support import plan_reply

    rig.gateway.queue(Step.INPUT_GUARD, guard_reply("why_question", "Why did revenue change?"))
    rig.gateway.queue(Step.PLANNER, plan_reply("Revenue by brand"))
    rig.gateway.queue(Step.SQL_WRITE, sql_reply())
    rig.gateway.queue(Step.EVIDENCE_CHECK, {"enough": False, "missing": ["return rate above 33%", "orders per customer by month"]})
    rig.gateway.queue(Step.COMPOSER, composer_reply("Brand A earned 1,234.5."))
    result = rig.ask("why did revenue change?")
    assert "33%" not in result.text and "orders per customer by month" in result.text


# the digits of a name are no evidence for an amount or a rate (review finding) ---------------------------------------------------------

NAMES = {"rows": [{"product": "Levi's 501 Original Jeans", "who": "Customer #48213", "tee": "Hanes 100% Cotton Tee", "pack": "Socks $5 Pack"}],
         "summary": {}, "total_rows": 1}
NAMED = ground_values({"q1": {"guarded": NAMES}})


@pytest.mark.parametrize("claim", ["Revenue reached $501.", "Revenue grew 501%.", "An invented $48,213 in sales.", "Sales of 501K.", "A 48213x jump.",
                                   "Margin of 5%.", "Revenue was $100.", "Revenue was 100 million."])
def test_the_digits_of_a_name_in_a_text_cell_never_back_an_amount_a_rate_or_a_scaled_figure(claim):
    result = check_numbers(f"A sentence without numbers. {claim} Another one without numbers.", NAMED)
    assert result.removed == 1 and claim not in result.body, claim


@pytest.mark.parametrize("text", [
    "Levi's 501 Original Jeans sold best.", "Customer #48213 bought the most.", "Hanes 100% Cotton Tee led the list.", "The Socks $5 Pack came second.",
    "The 501 and the 100% Cotton Tee.", "The Hanes 100 % Cotton Tee led.",
])
def test_a_name_quoted_word_for_word_is_still_backed(text):
    assert check_numbers(text, NAMED).clean, text


def test_a_figure_written_in_one_cell_does_not_back_the_same_digits_with_another_unit():
    result = check_numbers("The tee earned $100 from 100% of the orders.", NAMED)  # "100%" is written in a cell; "$100" is not
    assert result.removed == 1
