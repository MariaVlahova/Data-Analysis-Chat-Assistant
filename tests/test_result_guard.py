"""The Result Guard: masking, fake customer IDs, small groups, the row cap and failing closed (plan 06, F14, F15)."""
import dataclasses
import datetime as dt
import itertools
import json
import types
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from src.config import GuardsConfig, Settings
from src.errors import AgentError, ErrorKind
from src.guards import result_guard
from src.guards.pseudonymizer import SessionPseudonymizer
from src.guards.query import ALL_BRANDS_LABEL
from src.guards.result_guard import (
    MASKED,
    MAX_CELL_CHARS,
    MAX_CELL_NODES,
    NOTICE_CUSTOMER_LABELS,
    NOTICE_CUSTOMER_SPEND_SCOPE,
    NOTICE_VALUES_MASKED,
    NOTICE_WITHHELD,
    GuardedResult,
    ResultGuard,
)
from src.observability import Tracer
from src.observability.tracing import Span
from tests.guard_support import ALICE, CEO, issue_query, make_allowlist, make_guard

K = "__k_users"  # the alias the SQL Guard gives the injected distinct-customer count
CANARY = "canary@example.test"
RANKED = dict(per_customer=True, id_columns=("user_id",))
DEMOGRAPHIC = dict(demographic=True, k_column=K)


def guard(*, k: int = 10, cap: int = 50, pseudonymizer: SessionPseudonymizer | None = None, **options) -> ResultGuard:
    settings = Settings(guards=GuardsConfig(k_anonymity=k, rows_to_model=cap))
    return ResultGuard(settings, make_allowlist(), pseudonymizer or SessionPseudonymizer(), **options)


def apply(df: pd.DataFrame, gq=None, total: int | None = None, **options) -> GuardedResult:
    return guard(**options).apply(df, gq or issue_query(), len(df) if total is None else total)


def failure(df, gq=None, total: int | None = None, **options) -> AgentError:
    with pytest.raises(AgentError) as err:
        apply(df, gq, total, **options)
    assert err.value.kind == ErrorKind.RESULT_GUARD_FAILED
    return err.value


def groups(sizes: list[int]) -> pd.DataFrame:
    """A result about groups of customers: one row per group, with the injected group-size column."""
    return pd.DataFrame({"state": [f"S{i}" for i in range(len(sizes))], "revenue": [100.0 * (i + 1) for i in range(len(sizes))], K: sizes})


@pytest.fixture
def tracer(tmp_path):
    return Tracer(tmp_path / "traces", tmp_path / "runs.jsonl")


def spans_of(tracer: Tracer, trace_id: str) -> list[dict]:
    return [json.loads(line) for line in tracer.trace_path(trace_id).read_text(encoding="utf-8").splitlines()]


# A: column masking ---------------------------------------------------------------------------------------------------

def test_a_column_aliased_email_is_masked_and_a_normal_column_is_not():
    result = apply(pd.DataFrame({"email": ["a@x.test", "b@x.test"], "brand": ["Nike", "Levi's"], "revenue": [10, 20]}))
    assert [row["email"] for row in result.rows] == [MASKED, MASKED]
    assert [row["brand"] for row in result.rows] == ["Nike", "Levi's"] and [row["revenue"] for row in result.rows] == [10, 20]
    assert result.masked_columns == ["email"] and result.columns == ["email", "brand", "revenue"]
    assert "1 column was hidden because it held personal data." in result.notices


@pytest.mark.parametrize("name", [
    "email", "Email", "customer_email", "street_address", "postal_code", "user_geom", "Latitude", "longitude", "lat", "lng",
    "phone_number", "first_name", "FirstName", "last_name", "ssn", "dob", "zip", "billing_address",
])
def test_a_sensitive_name_is_masked(name):
    assert apply(pd.DataFrame({name: ["x"]})).masked_columns == [name]


@pytest.mark.parametrize("name", [
    "First Name", "last-name", "E-mail", "e mail", "E.Mail", "Credit Card", "Social Security", "full name", "Postal Code", "Date of Birth",
    "userLat", "streetAddress", "customerEmail", "LAST NAME", "Phone Number", "ＥＭＡＩＬ",
    # names with no separator, or a person word next to `name`, that a list of fragments missed
    "postcode", "post_code", "Postcode", "customer_name", "user_name", "client name", "buyerName", "customername", "username",
    "street", "mobile", "Mobile Number", "cell phone", "forename", "surname", "useremail", "homeaddress", "email_address",
])
def test_a_sensitive_name_is_masked_whatever_separates_its_words(name):
    # Not only an underscore: spaces, hyphens, dots, camelCase and full-width letters spell the same column.
    assert apply(pd.DataFrame({name: ["Jane"]})).masked_columns == [name]


@pytest.mark.parametrize("name", [
    "brand", "product_name", "name", "platform", "latency_ms", "relation", "status", "gender", "total_orders", "First Order Date",
    "Last Order", "plat form", "Total Revenue",
    # a sensitive word inside another word is a metric or a product, not personal data
    "headphones_sold", "smartphone_revenue", "addressable_market", "microphone_units", "emailed_campaigns", "phones_sold_per_store",
    "mobile_orders", "cellular_share", "brand_name", "category_name", "store_name", "first_order_date", "last_login_days",
])
def test_an_ordinary_name_is_left_alone(name):
    assert apply(pd.DataFrame({name: ["x"]})).masked_columns == []


def test_a_column_name_that_holds_an_email_is_cleaned_like_a_cell():
    # A PIVOT names its columns after values, so a header can be data.
    df = pd.DataFrame({CANARY: [1], f"{CANARY} ": [3], "revenue": [2.0]})
    result = apply(df)
    assert result.columns == ["[EMAIL_MASKED]", "[EMAIL_MASKED] ", "revenue"] and result.masked_columns == []
    assert result.rows == [{"[EMAIL_MASKED]": 1, "[EMAIL_MASKED] ": 3, "revenue": 2.0}]
    assert CANARY not in json.dumps(dataclasses.asdict(result))
    assert NOTICE_VALUES_MASKED in result.notices


def test_two_headers_that_clean_to_the_same_name_keep_their_own_keys():
    result = apply(pd.DataFrame([[1, 2]], columns=[CANARY, "other@example.test"]))
    assert result.columns == ["[EMAIL_MASKED]", "[EMAIL_MASKED]_2"] and result.rows == [{"[EMAIL_MASKED]": 1, "[EMAIL_MASKED]_2": 2}]


def test_a_customer_id_column_is_found_whatever_separates_its_words():
    df = pd.DataFrame({"Customer ID": [4821377], "buyer-id": [4821377], "userId": [4821377]})
    (row,) = apply(df, issue_query(per_customer=True, id_columns=("Customer ID",))).rows
    assert all(value.startswith("Customer #") for value in row.values())


def test_a_masked_column_hides_its_nulls_too():
    result = apply(pd.DataFrame({"email": ["a@x.test", None]}))
    assert [row["email"] for row in result.rows] == [MASKED, MASKED]


# B: value scan ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, token", [
    (f"write to {CANARY} now", "[EMAIL_MASKED]"),
    ("call +1 800 555 0199", "[PHONE_MASKED]"),
    ("card 4111 1111 1111 1111", "[CARD_MASKED]"),
    ("ssn 123-45-6789", "[ID_MASKED]"),
    ("ship to 42 Wallaby Way, Sydney", "[ADDRESS_MASKED]"),
    ("at 34.0522, -118.2437", "[COORDS_MASKED]"),
    ("Austin, TX 78701", "[POSTAL_MASKED]"),
])
def test_a_pattern_inside_a_text_cell_becomes_its_token(text, token):
    result = apply(pd.DataFrame({"note": [text, "clean"]}))
    assert token in result.rows[0]["note"] and result.rows[1]["note"] == "clean"
    assert NOTICE_VALUES_MASKED in result.notices and result.masked_columns == []


def test_arrays_and_structs_are_scanned_as_json():
    df = pd.DataFrame({
        "tags": [np.array(["a", CANARY], dtype=object)],
        "owner": [{"brand": "X", "mail": CANARY}],
        "sizes": [[1, 2, 3]],
    })
    (row,) = apply(df).rows
    assert row == {"tags": '["a", "[EMAIL_MASKED]"]', "owner": '{"brand": "X", "mail": "[EMAIL_MASKED]"}', "sizes": "[1, 2, 3]"}


def test_cells_come_out_as_plain_values_that_json_can_write():
    df = pd.DataFrame({
        "n": [1, 2],
        "f": [1.5, float("nan")],
        "inf": [float("inf"), 2.0],
        "dec": [Decimal("1.50"), Decimal("2")],
        "when": [pd.Timestamp("2025-01-02 03:04:05"), pd.NaT],
        "day": [dt.date(2025, 1, 2), None],
        "flag": [True, False],
        "s": ["a", None],
    })
    first, second = apply(df).rows
    assert first == {"n": 1, "f": 1.5, "inf": None, "dec": 1.5, "when": "2025-01-02T03:04:05", "day": "2025-01-02", "flag": True, "s": "a"}
    assert second == {"n": 2, "f": None, "inf": 2.0, "dec": 2, "when": None, "day": None, "flag": False, "s": None}
    assert type(first["n"]) is int and type(first["f"]) is float and type(second["dec"]) is int
    json.dumps([first, second], allow_nan=False)


@pytest.mark.parametrize("cell", [b"bytes", dt.timedelta(days=1), object(), {1, 2}, complex(1, 2), np.timedelta64(5, "s")])
def test_a_cell_type_we_cannot_handle_fails_closed(cell):
    df = pd.DataFrame({"brand": ["Nike", "Levi's"], "odd": pd.Series([cell, cell], dtype=object)})
    assert failure(df).user_message == "I couldn't safely check the results, so I did not show them."


def test_an_array_that_is_nested_too_deeply_fails_closed():
    deep: list = []
    for _ in range(result_guard.MAX_NESTING + 2):
        deep = [deep]
    failure(pd.DataFrame({"x": pd.Series([deep], dtype=object)}))


def test_a_repeated_column_name_gets_its_own_key():
    df = pd.DataFrame([[1, 2]], columns=["id", "id"])
    result = apply(df)
    assert result.columns == ["id", "id_2"] and result.rows == [{"id": 1, "id_2": 2}]


def test_a_repeated_name_that_collides_with_another_column_fails_closed():
    failure(pd.DataFrame([[1, 2, 3]], columns=["id", "id", "id_2"]))


# C: fake customer IDs --------------------------------------------------------------------------------------------------

def test_customer_ids_become_fake_ids_and_the_same_customer_keeps_the_label():
    df = pd.DataFrame({"user_id": [4821377, 77, 4821377], "spend": [300.0, 200.0, 100.0]})
    result = apply(df, issue_query(**RANKED))
    labels = [row["user_id"] for row in result.rows]
    assert all(label.startswith("Customer #") for label in labels)
    assert labels[0] == labels[2] != labels[1]
    assert "4821377" not in json.dumps(result.rows) + repr(result)
    assert [row["spend"] for row in result.rows] == [300.0, 200.0, 100.0]


def test_the_same_customer_has_the_same_label_in_two_results_of_one_session():
    shared = SessionPseudonymizer()
    gq = issue_query(**RANKED)
    first = apply(pd.DataFrame({"user_id": [4821377, 77]}), gq, pseudonymizer=shared)
    second = apply(pd.DataFrame({"user_id": [77, 99]}), gq, pseudonymizer=shared)
    assert second.rows[0]["user_id"] == first.rows[1]["user_id"]
    other_session = apply(pd.DataFrame({"user_id": [4821377, 77]}), gq)
    assert other_session.rows != first.rows


def test_a_derived_string_that_holds_the_id_is_replaced_whole():
    df = pd.DataFrame({"customer_key": ["c4821377", "c77"], "spend": [2.0, 1.0]})
    result = apply(df, issue_query(per_customer=True, id_columns=("customer_key",)))
    assert all(row["customer_key"].startswith("Customer #") for row in result.rows)
    assert "4821377" not in json.dumps(result.rows)


def test_a_column_named_like_a_customer_id_is_replaced_even_when_the_query_did_not_declare_it():
    df = pd.DataFrame({"user_id": [4821377], "customer_id": [4821377], "buyer_key": [4821377], "product_id": [5551234]})
    (row,) = apply(df, issue_query(**RANKED)).rows
    assert row["user_id"] == row["customer_id"] == row["buyer_key"] and row["user_id"].startswith("Customer #")
    assert row["product_id"] == 5551234  # a product ID is not a customer


def test_the_declared_id_column_matches_without_regard_to_case_and_nulls_stay_null():
    df = pd.DataFrame({"USER_ID": [4821377, None], "spend": [2.0, 1.0]})
    first, second = apply(df, issue_query(**RANKED)).rows
    assert first["USER_ID"].startswith("Customer #") and second["USER_ID"] is None


def test_a_ranked_list_whose_id_column_is_missing_fails_closed():
    failure(pd.DataFrame({"spend": [2.0, 1.0]}), issue_query(**RANKED))
    failure(pd.DataFrame({"spend": [2.0]}), issue_query(per_customer=True, id_columns=()))


def test_an_id_cell_that_is_not_a_scalar_fails_closed():
    failure(pd.DataFrame({"user_id": pd.Series([[1, 2]], dtype=object)}), issue_query(**RANKED))


def test_a_ranked_list_says_the_labels_are_for_this_session_and_the_spend_is_the_managers_brands():
    result = apply(pd.DataFrame({"user_id": [1], "spend": [2.0]}), issue_query(**RANKED))
    assert NOTICE_CUSTOMER_LABELS in result.notices and NOTICE_CUSTOMER_SPEND_SCOPE in result.notices
    ceo = apply(pd.DataFrame({"user_id": [1], "spend": [2.0]}), issue_query(scope_label="all brands", **RANKED))
    assert ceo.notices == [NOTICE_CUSTOMER_LABELS]


# S: small groups ---------------------------------------------------------------------------------------------------------

def test_a_group_of_9_is_hidden_and_a_group_of_10_is_shown():
    result = apply(groups([9, 10, 11]), issue_query(**DEMOGRAPHIC))
    assert [row["state"] for row in result.rows] == ["S1", "S2"]
    assert result.suppressed_groups == 1 and result.total_rows == 2 and result.status == "ok"
    assert result.notices == ["1 group with fewer than 10 customers was hidden."]


def test_the_group_size_column_never_leaves_the_guard():
    result = apply(groups([50, 60]), issue_query(**DEMOGRAPHIC))
    assert result.columns == ["state", "revenue"] and all(set(row) == {"state", "revenue"} for row in result.rows)
    assert K not in json.dumps(dataclasses.asdict(result))
    assert result.summary["columns"] == ["state", "revenue"]


def test_a_mixed_result_hides_only_the_small_rows():
    result = apply(groups([3, 50, 8, 12, 1]), issue_query(**DEMOGRAPHIC))
    assert [row["state"] for row in result.rows] == ["S1", "S3"]
    assert result.suppressed_groups == 3 and "3 groups with fewer than 10 customers were hidden." in result.notices


def test_when_every_group_is_small_nothing_is_left_and_the_notice_says_why():
    result = apply(groups([1, 2, 3]), issue_query(**DEMOGRAPHIC))
    assert result.rows == [] and result.status == "empty" and result.suppressed_groups == 3
    assert result.notices == ["3 groups with fewer than 10 customers were hidden."]


def test_the_group_size_limit_is_the_configured_one():
    assert [r["state"] for r in apply(groups([2, 3, 4]), issue_query(**DEMOGRAPHIC), k=3).rows] == ["S1", "S2"]


@pytest.mark.parametrize("group_size", [pd.Series([None, 20], dtype="Int64"), ["many", 20], [float("nan"), 20], [True, 20]])
def test_a_group_size_that_is_not_a_whole_number_fails_closed(group_size):
    df = groups([1, 2]).assign(**{K: group_size})
    failure(df, issue_query(**DEMOGRAPHIC))


def test_a_whole_number_group_size_in_any_numeric_type_works():
    df = groups([1, 2, 3]).assign(**{K: pd.Series([5.0, 20.0, 30.0]).astype(object)})
    assert [r["state"] for r in apply(df, issue_query(**DEMOGRAPHIC)).rows] == ["S1", "S2"]
    df = groups([1, 2]).assign(**{K: [Decimal("5"), Decimal("20")]})
    assert [r["state"] for r in apply(df, issue_query(**DEMOGRAPHIC)).rows] == ["S1"]


def test_a_missing_group_size_column_fails_closed():
    failure(groups([50, 60]).drop(columns=K), issue_query(**DEMOGRAPHIC))


def test_when_the_group_sizes_cannot_be_checked_the_whole_result_is_withheld(tracer):
    with tracer.start_turn("hash", "conv-1", "t-1") as turn:
        result = apply(pd.DataFrame({"state": ["A", "B"], "revenue": [1.0, 2.0]}), issue_query(demographic=True, k_column=None))
    assert result.status == "withheld" and result.rows == [] and result.columns == [] and result.total_rows == 0
    assert result.notices == [NOTICE_WITHHELD] == ["Group sizes could not be checked, so the result was withheld."]
    assert result.summary["row_count"] == 0
    assert turn.row["guard_blocks"]["result"] == 1


def test_customer_rows_next_to_demographics_are_withheld_too():
    df = groups([50]).assign(user_id=[1])
    assert apply(df, issue_query(per_customer=True, id_columns=("user_id",), **DEMOGRAPHIC)).status == "withheld"


def test_a_ranked_list_is_not_suppressed():
    result = apply(pd.DataFrame({"user_id": [1, 2], "spend": [5.0, 3.0]}), issue_query(**RANKED))
    assert len(result.rows) == 2 and result.suppressed_groups == 0


def test_product_brand_and_time_groups_are_not_suppressed():
    result = apply(pd.DataFrame({"brand": ["Nike", "Levi's"], "orders": [1, 2]}), issue_query())
    assert len(result.rows) == 2 and result.suppressed_groups == 0 and result.notices == []


def test_an_empty_result_is_empty_not_withheld():
    for gq in (issue_query(), issue_query(demographic=True, k_column=None)):
        result = apply(pd.DataFrame({"state": []}), gq)
        assert result.status == "empty" and result.rows == [] and result.notices == []
        assert result.summary == {"row_count": 0, "columns": ["state"], "numeric": {}, "nulls": {"state": 0}}


# D: the cap --------------------------------------------------------------------------------------------------------------

def test_1240_rows_give_50_rows_a_truncated_flag_and_the_notice():
    df = pd.DataFrame({"brand": [f"B{i}" for i in range(1240)], "revenue": range(1240)})
    result = apply(df)
    assert len(result.rows) == 50 and result.truncated and result.total_rows == 1240
    assert result.rows[0]["brand"] == "B0" and result.rows[-1]["brand"] == "B49"
    assert result.notices == ["Showing top 50 of 1,240 rows."]
    assert result.summary["row_count"] == 1240 and result.summary["numeric"]["revenue"]["sum"] == sum(range(1240))


def test_the_suppression_happens_before_the_cap():
    sizes = [9] * 40 + [10] * 1200
    result = apply(groups(sizes), issue_query(**DEMOGRAPHIC))
    assert result.suppressed_groups == 40 and result.total_rows == 1200 and len(result.rows) == 50
    assert result.rows[0]["state"] == "S40"  # the 40 small groups did not use up the first rows
    assert result.notices == ["40 groups with fewer than 10 customers were hidden.", "Showing top 50 of 1,200 rows."]


def test_exactly_50_rows_are_not_truncated():
    result = apply(pd.DataFrame({"n": range(50)}))
    assert len(result.rows) == 50 and not result.truncated and result.notices == []


def test_the_cap_is_the_configured_one():
    result = apply(pd.DataFrame({"n": range(20)}), cap=5)
    assert len(result.rows) == 5 and result.notices == ["Showing top 5 of 20 rows."]


def test_rows_the_runner_cut_before_the_guard_are_still_counted():
    result = apply(pd.DataFrame({"n": range(1000)}), total=5000)
    assert result.total_rows == 5000 and result.notices == ["Showing top 50 of 5,000 rows."]
    short = apply(pd.DataFrame({"n": range(10)}), total=10)
    assert short.total_rows == 10 and not short.truncated


# the summary -------------------------------------------------------------------------------------------------------------

def test_the_summary_has_counts_and_totals_and_never_rows():
    df = pd.DataFrame({"brand": ["Nike", "Levi's", "Gap"], "orders": [1, 2, None], "revenue": [1.5, 2.5, 3.0], "email": ["a", "b", "c"]})
    summary = apply(df).summary
    assert summary == {
        "row_count": 3,
        "columns": ["brand", "orders", "revenue", "email"],
        "numeric": {"orders": {"min": 1.0, "max": 2.0, "sum": 3.0}, "revenue": {"min": 1.5, "max": 3.0, "sum": 7.0}},
        "nulls": {"brand": 0, "orders": 1, "revenue": 0, "email": 0},
    }
    assert "Nike" not in json.dumps(summary)


def test_a_whole_number_column_sums_exactly_and_a_flag_column_is_not_numeric():
    summary = apply(pd.DataFrame({"n": [2**62, 2**62], "flag": [True, False]})).summary
    assert summary["numeric"] == {"n": {"min": 2**62, "max": 2**62, "sum": 2**63}}


def test_the_summary_covers_the_rows_that_survived_the_suppression_not_the_cut_rows():
    result = apply(groups([1, 50, 60]), issue_query(**DEMOGRAPHIC))
    assert result.summary["row_count"] == 2 and result.summary["numeric"]["revenue"]["sum"] == 500.0


# the query the guard is given --------------------------------------------------------------------------------------------

def test_the_tags_the_sql_guard_issues_drive_the_result_guard():
    sql_guard = make_guard()
    gq = sql_guard.check("SELECT u.state, COUNT(*) AS customers FROM users u GROUP BY u.state", ALICE)
    assert gq.demographic and gq.k_column
    df = pd.DataFrame({"state": ["Ohio", "Utah"], "customers": [50, 5], gq.k_column: [50, 5]})
    result = apply(df, gq)
    assert [row["state"] for row in result.rows] == ["Ohio"] and result.suppressed_groups == 1

    ranked = sql_guard.check("SELECT user_id, SUM(sale_price) AS spend FROM order_items GROUP BY user_id ORDER BY spend DESC LIMIT 10", ALICE)
    assert ranked.per_customer and ranked.id_columns == ("user_id",)
    result = apply(pd.DataFrame({"user_id": [4821377], "spend": [9.5]}), ranked)
    assert result.rows[0]["user_id"].startswith("Customer #")


def test_a_query_that_was_changed_after_the_guard_sealed_it_fails_closed():
    # Only the seal stands between this forgery and the real IDs: the column name `account` tells nothing, and the forged
    # query says "no customer IDs here".
    df = pd.DataFrame({"account": [4821377], "spend": [2.0]})
    sealed = issue_query(per_customer=True, id_columns=("account",))
    assert apply(df, sealed).rows[0]["account"].startswith("Customer #")
    forged = dataclasses.replace(sealed, per_customer=False, id_columns=())
    assert not forged.authentic
    failure(df, forged)


@pytest.mark.parametrize("fake", [
    types.SimpleNamespace(id_columns=(), per_customer=False, demographic=False, k_column=None, scope_label="all brands"),
    "SELECT 1",
])
def test_a_look_alike_query_fails_closed(fake):
    failure(pd.DataFrame({"user_id": [4821377]}), fake)


def test_no_query_at_all_fails_closed():
    with pytest.raises(AgentError) as err:
        guard().apply(pd.DataFrame({"user_id": [4821377]}), None, 1)
    assert err.value.kind == ErrorKind.RESULT_GUARD_FAILED


def test_input_of_the_wrong_kind_fails_closed():
    gq = issue_query()
    failure(pd.DataFrame({"a": [1]}), gq, total=-1)
    failure(pd.DataFrame({"a": [1]}), gq, total=True)
    failure(pd.DataFrame({"a": [1]}), gq, total="1")
    with pytest.raises(AgentError) as err:
        guard().apply([{"a": 1}], gq, 1)
    assert err.value.kind == ErrorKind.RESULT_GUARD_FAILED


# E: fail closed ----------------------------------------------------------------------------------------------------------

def boom(*args, **kwargs):
    raise RuntimeError(f"forced failure holding {CANARY}")


SCENARIOS = {
    "ranked": (lambda: pd.DataFrame({"user_id": [4821377, 77], "spend": [2.0, 1.0], "note": ["x", "y"]}), issue_query(**RANKED)),
    "groups": (lambda: groups([50, 60]), issue_query(**DEMOGRAPHIC)),
}


@pytest.mark.parametrize("target, scenario", [
    ("column roles (A)", "ranked"),
    ("value scan (B)", "ranked"),
    ("pseudonymize (C)", "ranked"),
    ("group size (S)", "groups"),
    ("cap and notices (D)", "ranked"),
    ("summary", "ranked"),
])
def test_a_forced_exception_in_any_step_returns_no_rows_and_the_typed_error(monkeypatch, target, scenario):
    patch = {
        "column roles (A)": (ResultGuard, "_role"),
        "value scan (B)": (ResultGuard, "_scan"),
        "pseudonymize (C)": (SessionPseudonymizer, "fake_id"),
        "group size (S)": (result_guard, "_k_value"),
        "cap and notices (D)": (ResultGuard, "_build"),
        "summary": (result_guard, "_summary"),
    }[target]
    monkeypatch.setattr(*patch, boom)
    make_df, gq = SCENARIOS[scenario]
    with pytest.raises(AgentError) as err:
        guard().apply(make_df(), gq, 2)
    error = err.value
    assert error.kind == ErrorKind.RESULT_GUARD_FAILED and error.detail.startswith("RuntimeError at")
    assert CANARY not in error.detail and CANARY not in str(error)  # the exception's own text is never kept
    assert error.__cause__ is None and error.__suppress_context__


def test_a_guard_that_runs_out_of_time_fails_closed():
    ticks = itertools.count(0, 10)
    failure(pd.DataFrame({"n": range(5)}), clock=lambda: next(ticks), timeout_s=5.0)


def test_a_guard_within_its_time_limit_works():
    assert len(apply(pd.DataFrame({"n": range(5)}), clock=lambda: 0.0).rows) == 5


# the trace ---------------------------------------------------------------------------------------------------------------

def test_the_decision_is_pass_when_nothing_changed_and_rewrite_when_something_did(tracer):
    with tracer.start_turn("hash", "conv-1", "t-1") as clean:
        apply(pd.DataFrame({"brand": ["Nike"], "revenue": [1.0]}))
    with tracer.start_turn("hash", "conv-1", "t-2") as changed:
        apply(groups([3, 50]), issue_query(**DEMOGRAPHIC))

    def decision(turn):
        (span,) = [s for s in spans_of(tracer, turn.trace_id) if s["name"] == "guard.result"]
        (event,) = [e for e in span["events"] if e["name"] == "guard.decision"]
        return span, event["attrs"]

    span, attrs = decision(clean)
    assert span["kind"] == "guard" and attrs["guard"] == "result" and attrs["outcome"] == "pass" and attrs["rule"] is None
    span, attrs = decision(changed)
    assert attrs["outcome"] == "rewrite" and attrs["rule"] == "GROUPS_HIDDEN"
    assert span["attrs"]["groups_hidden"] == 1 and attrs["groups_hidden"] == 1
    assert changed.row["guard_blocks"]["result"] == 0 and clean.row["guard_blocks"]["result"] == 0


def test_a_failure_is_a_block_and_the_trace_holds_counts_never_values(tracer):
    df = pd.DataFrame({"user_id": [4821377], "note": [f"mail {CANARY} at 42 Wallaby Way"]})
    with tracer.start_turn("hash", "conv-1", "t-1") as turn:
        apply(df, issue_query(**RANKED))
    with pytest.raises(AgentError):
        with tracer.start_turn("hash", "conv-1", "t-2") as failed:
            apply(df, issue_query(**RANKED), total=-1)

    text = tracer.trace_path(turn.trace_id).read_text(encoding="utf-8") + tracer.trace_path(failed.trace_id).read_text(encoding="utf-8")
    for secret in (CANARY, "4821377", "Wallaby", "Customer #"):
        assert secret not in text
    (span,) = [s for s in spans_of(tracer, turn.trace_id) if s["name"] == "guard.result"]
    assert span["attrs"] == {
        "status": "ok", "rows": 1, "columns_masked": 0, "cells_masked": 1, "values_masked": 1, "ids_replaced": 1,
        "groups_hidden": 0, "rows_cut": 0, "unbranded": 0,
    }
    assert failed.row["guard_blocks"]["result"] == 1 and failed.row["error_kinds"] == ["RESULT_GUARD_FAILED"]
    assert failed.row["outcome"] == "failed"


# limits: one cell can be very large, and a regex cannot be interrupted ------------------------------------------------------

def test_a_cell_too_long_to_check_fails_closed_and_one_at_the_limit_works():
    failure(pd.DataFrame({"note": ["x" * (MAX_CELL_CHARS + 1)]}))
    assert apply(pd.DataFrame({"note": ["x" * MAX_CELL_CHARS]})).status == "ok"


def test_an_array_with_too_many_values_fails_closed_before_it_is_copied():
    failure(pd.DataFrame({"a": pd.Series([list(range(MAX_CELL_NODES + 1))], dtype=object)}))
    failure(pd.DataFrame({"a": pd.Series([np.arange(MAX_CELL_NODES + 1)], dtype=object)}))
    failure(pd.DataFrame({"a": pd.Series([{str(i): i for i in range(MAX_CELL_NODES + 1)}], dtype=object)}))
    assert apply(pd.DataFrame({"a": pd.Series([list(range(MAX_CELL_NODES))], dtype=object)})).status == "ok"


def test_a_json_text_that_ends_up_too_long_fails_closed():
    long_words = ["y" * 2_000] * 12  # few values, but 24,000 characters once written out
    failure(pd.DataFrame({"a": pd.Series([long_words], dtype=object)}))


def test_a_column_name_too_long_to_check_fails_closed():
    failure(pd.DataFrame({"n" * (MAX_CELL_CHARS + 1): [1]}))


def test_the_time_limit_is_also_checked_between_the_cells_of_one_row():
    # Without the check per cell this run reads the clock three times (start, row, after the build) and passes.
    ticks = itertools.count(0, 2)
    failure(pd.DataFrame({"a": [1], "b": [2]}), clock=lambda: next(ticks), timeout_s=5.0)


def test_the_time_limit_also_covers_the_summary_and_the_notices(monkeypatch):
    now = [0.0]
    build = ResultGuard._build

    def slow_build(self, *args, **kwargs):
        result = build(self, *args, **kwargs)
        now[0] = 100.0  # the summary took forever
        return result

    monkeypatch.setattr(ResultGuard, "_build", slow_build)
    failure(pd.DataFrame({"n": [1]}), clock=lambda: now[0], timeout_s=5.0)


# recording the decision is part of the guard ---------------------------------------------------------------------------------

def test_a_failure_while_recording_the_decision_is_still_the_typed_error_and_no_rows(monkeypatch):
    monkeypatch.setattr(result_guard, "guard_decision", boom)
    error = failure(pd.DataFrame({"n": [1]}))
    assert error.detail.startswith("RuntimeError at") and CANARY not in error.detail and error.__cause__ is None


def test_the_typed_error_survives_a_failure_to_record_the_failure_too(monkeypatch):
    monkeypatch.setattr(result_guard, "guard_decision", boom)
    error = failure(pd.DataFrame({"n": [1]}), total=-1)  # the guard itself refuses, and recording that refusal raises as well
    assert error.detail == "the row count is not a whole number"


def test_a_failure_while_setting_span_attributes_is_the_typed_error_too(monkeypatch):
    set_attributes = Span.set

    def broken(self, **attrs):
        if "ids_replaced" in attrs:  # only the guard's own summary of the run
            raise ValueError("cannot store")
        set_attributes(self, **attrs)

    monkeypatch.setattr(Span, "set", broken)
    assert failure(pd.DataFrame({"n": [1]})).detail.startswith("ValueError at")


# the scope label ---------------------------------------------------------------------------------------------------------------

def test_the_ceo_label_is_one_constant_that_both_guards_use():
    sql_guard = make_guard()
    sql = "SELECT user_id, SUM(sale_price) AS spend FROM order_items GROUP BY user_id ORDER BY spend DESC LIMIT 10"
    df = pd.DataFrame({"user_id": [4821377], "spend": [9.5]})
    ceo = sql_guard.check(sql, CEO)
    assert ceo.scope_label == ALL_BRANDS_LABEL
    assert apply(df, ceo).notices == [NOTICE_CUSTOMER_LABELS]  # the CEO sees every brand: no "only your brands" line
    manager = sql_guard.check(sql, ALICE)
    assert manager.scope_label != ALL_BRANDS_LABEL
    assert apply(df, manager).notices == [NOTICE_CUSTOMER_LABELS, NOTICE_CUSTOMER_SPEND_SCOPE]


# findings of the second review ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["buyers_number", "customers_num", "customer_no", "users_count", "client_total"])
def test_a_count_named_like_a_customer_is_a_number_not_a_customer(name):
    # `number`, `num` and `no` name a count as often as an ID. A count must not turn into `Customer #21257`.
    assert apply(pd.DataFrame({name: [1234]})).rows == [{name: 1234}]


@pytest.mark.parametrize("text", [
    "42 Wallaby\nWay",  # a line break inside the address
    "mail " + CANARY,
    "42 Wallaby\tWay",
])
def test_a_string_inside_an_array_or_struct_is_scanned_as_the_text_it_is(text):
    # JSON writes a line break inside a string as the two characters backslash and n, which no pattern reads across.
    df = pd.DataFrame({
        "tags": pd.Series([[text, "call me"]], dtype=object),
        "owner": pd.Series([{"note": text}], dtype=object),
        "deep": pd.Series([[{"inner": [text]}]], dtype=object),
    })
    (row,) = apply(df).rows
    for cell in row.values():
        assert "Wallaby" not in cell and CANARY not in cell and "[" in cell
    assert "[ADDRESS_MASKED]" in row["tags"] or "[EMAIL_MASKED]" in row["tags"]


def test_a_struct_key_that_holds_personal_data_is_masked_and_the_cell_is_counted():
    result = apply(pd.DataFrame({"owner": pd.Series([{CANARY: 1, "ok": 2}], dtype=object)}))
    assert result.rows == [{"owner": '{"[EMAIL_MASKED]": 1, "ok": 2}'}] and NOTICE_VALUES_MASKED in result.notices


def test_an_array_with_nothing_to_mask_is_not_counted_as_masked():
    result = apply(pd.DataFrame({"sizes": pd.Series([["S", "M", "L"]], dtype=object)}))
    assert result.rows == [{"sizes": '["S", "M", "L"]'}] and result.notices == []


def test_when_every_row_seen_is_hidden_the_rows_never_seen_are_still_mentioned():
    df = groups([3, 4])
    result = apply(df, issue_query(**DEMOGRAPHIC), total=5000)
    assert result.status == "empty" and result.rows == [] and result.total_rows == 4998
    assert result.notices == ["2 groups with fewer than 10 customers were hidden.", "4,998 more rows were not checked, so this result may be incomplete."]
    one = apply(groups([3]), issue_query(**DEMOGRAPHIC), total=2)
    assert one.notices == ["1 group with fewer than 10 customers was hidden.", "1 more row was not checked, so this result may be incomplete."]


def test_with_no_rows_left_out_there_is_no_not_checked_notice():
    assert apply(groups([3, 4]), issue_query(**DEMOGRAPHIC)).notices == ["2 groups with fewer than 10 customers were hidden."]
    assert apply(groups([30, 40]), issue_query(**DEMOGRAPHIC), total=2).notices == []


# E: order items with no brand (plan 12, C1, A23) -------------------------------------------------------------------------

def brand_rows(brands):
    return pd.DataFrame({"brand": brands, "revenue": [100.0 * (i + 1) for i in range(len(brands))]})


def test_an_empty_brand_is_labelled_for_the_user_who_sees_every_brand_and_the_answer_says_what_it_is():
    from src.guards.result_guard import NO_BRAND_LABEL, NOTICE_NO_BRAND

    result = apply(brand_rows([None, "", "   ", "Nike", float("nan")]), issue_query(scope_label=ALL_BRANDS_LABEL))
    assert [row["brand"] for row in result.rows] == [NO_BRAND_LABEL] * 3 + ["Nike", NO_BRAND_LABEL] and NO_BRAND_LABEL == "(no brand)"
    assert result.notices.count(NOTICE_NO_BRAND) == 1  # one notice, however many rows
    assert NOTICE_NO_BRAND == "Some order items have no brand. Brand managers do not see them; they are counted here."
    assert [row["revenue"] for row in result.rows] == [100.0, 200.0, 300.0, 400.0, 500.0]  # only the empty cells changed


def test_a_brand_manager_gets_no_such_label_or_notice():
    from src.guards.result_guard import NO_BRAND_LABEL, NOTICE_NO_BRAND

    result = apply(brand_rows([None, "Nike"]), issue_query())  # "your brands: Brand A, Brand B": the scope views leave unbranded items out
    assert [row["brand"] for row in result.rows] == [None, "Nike"] and NOTICE_NO_BRAND not in result.notices
    assert NO_BRAND_LABEL not in json.dumps(result.rows)


def test_a_result_with_every_brand_named_has_no_notice():
    from src.guards.result_guard import NOTICE_NO_BRAND

    assert NOTICE_NO_BRAND not in apply(brand_rows(["Nike", "Levi's"]), issue_query(scope_label=ALL_BRANDS_LABEL)).notices


def test_only_a_column_called_brand_is_labelled():
    df = pd.DataFrame({"category": [None, "Jeans"], "brand_count": [None, 3], "revenue": [1.0, 2.0]})
    result = apply(df, issue_query(scope_label=ALL_BRANDS_LABEL))
    assert [row["category"] for row in result.rows] == [None, "Jeans"] and result.rows[0]["brand_count"] is None


def test_a_small_group_that_is_hidden_is_not_counted_as_an_unbranded_item():
    from src.guards.result_guard import NO_BRAND_LABEL, NOTICE_NO_BRAND

    df = pd.DataFrame({"brand": [None, "Nike"], "revenue": [1.0, 2.0], K: [3, 50]})  # the unbranded group has 3 customers: hidden
    result = apply(df, issue_query(scope_label=ALL_BRANDS_LABEL, **DEMOGRAPHIC))
    assert [row["brand"] for row in result.rows] == ["Nike"] and NOTICE_NO_BRAND not in result.notices
    assert NO_BRAND_LABEL not in json.dumps(result.rows)


def test_the_label_is_a_rewrite_in_the_guard_decision_with_a_count_and_no_value(tmp_path):
    tracer = Tracer(tmp_path / "traces", tmp_path / "runs.jsonl")
    with tracer.start_turn("hash0001", "conv-1", "t-1") as turn:
        apply(brand_rows([None, None, "Nike"]), issue_query(scope_label=ALL_BRANDS_LABEL))
    spans = [json.loads(line) for line in tracer.trace_path(turn.trace_id).read_text(encoding="utf-8").splitlines()]
    (decision,) = [e for s in spans for e in s["events"] if e["name"] == "guard.decision"]
    assert decision["attrs"]["outcome"] == "rewrite" and decision["attrs"]["rule"] == "NO_BRAND_LABELLED" and decision["attrs"]["unbranded"] == 2
