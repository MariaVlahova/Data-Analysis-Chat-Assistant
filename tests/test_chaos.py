"""Chaos (plan 04, F37): seeded and scripted fault injection."""
import httpx
import pytest
from google.api_core import exceptions as api_exc
from google.genai import errors as genai_errors

from src.data_access.errors_map import classify
from src.errors import ErrorKind
from src.llm.chaos import MISSING_COLUMN, PROFILES, BqFault, Chaos, LlmFault, MalformedReply, SqlFault, corrupt_sql
from src.llm.routing import Step

SQL = "SELECT brand, SUM(sale_price) AS revenue FROM t GROUP BY brand LIMIT 10"


def llm_sequence(chaos: Chaos, calls: int = 200) -> list[str]:
    return [("ok" if (fault := chaos.maybe_fail_llm(Step.SQL_WRITE)) is None else type(fault).__name__) for _ in range(calls)]


def test_the_same_seed_gives_the_same_faults_twice():
    first, second = Chaos.from_level("harsh", 42), Chaos.from_level("harsh", 42)
    assert llm_sequence(first) == llm_sequence(second)
    assert first.injected == second.injected and first.injected  # something was injected


def test_another_seed_gives_another_sequence():
    assert llm_sequence(Chaos.from_level("harsh", 1)) != llm_sequence(Chaos.from_level("harsh", 2))


def test_a_run_without_a_seed_draws_one_and_keeps_it_so_the_run_can_be_replayed():
    chaos = Chaos.from_level("mild")
    assert isinstance(chaos.seed, int)
    assert llm_sequence(chaos) == llm_sequence(Chaos.from_level("mild", chaos.seed))


def test_mild_hits_about_15_percent_and_harsh_about_40():
    for level, expected in (("mild", 0.15), ("harsh", 0.40)):
        chaos = Chaos.from_level(level, 5)
        faults = sum(1 for _ in range(4000) if chaos.maybe_fail_llm(Step.COMPOSER) is not None)
        # the consecutive-fault cap pulls the share a little under the nominal rate
        assert expected * 0.7 < faults / 4000 <= expected * 1.1, (level, faults / 4000)


def test_a_run_of_faults_is_cut_short_so_a_turn_can_finish():
    for level, profile in PROFILES.items():
        chaos = Chaos.from_level(level, 3)
        run = longest = 0
        for _ in range(3000):
            run = run + 1 if chaos.maybe_fail_llm(Step.COMPOSER) is not None else 0
            longest = max(longest, run)
        assert longest <= profile.max_consecutive


def test_begin_turn_forgets_a_run_of_faults():
    chaos = Chaos.from_level("harsh", 7)
    while chaos._run_llm < PROFILES["harsh"].max_consecutive:
        chaos.maybe_fail_llm(Step.COMPOSER)
    assert chaos.maybe_fail_llm(Step.COMPOSER) is None  # the cap forces one clean call
    chaos.begin_turn()
    assert chaos._run_llm == 0


def test_faults_are_the_exceptions_the_real_libraries_raise():
    chaos = Chaos.scripted()
    chaos.fail_llm(LlmFault.RATE_LIMIT).fail_llm(LlmFault.UNAVAILABLE).fail_llm(LlmFault.TIMEOUT).fail_llm(LlmFault.BAD_JSON)
    rate, unavailable, timeout, garbage = (chaos.maybe_fail_llm(Step.PLANNER) for _ in range(4))
    assert isinstance(rate, genai_errors.ClientError) and rate.code == 429 and "1s" in str(rate.details)
    assert isinstance(unavailable, genai_errors.ServerError) and unavailable.code == 503
    assert isinstance(timeout, httpx.TimeoutException)
    assert isinstance(garbage, MalformedReply)
    assert [e.chaos_fault for e in (rate, unavailable, timeout, garbage)] == ["rate_limit", "unavailable", "timeout", "bad_json"]
    assert chaos.maybe_fail_llm(Step.PLANNER) is None  # the script is used up


def test_scripted_faults_can_be_aimed_at_one_step():
    chaos = Chaos.scripted().fail_llm(LlmFault.UNAVAILABLE, times=2, step=Step.COMPOSER)
    assert chaos.maybe_fail_llm(Step.SQL_WRITE) is None
    assert chaos.maybe_fail_llm(Step.COMPOSER) is not None
    assert chaos.maybe_fail_llm(Step.COMPOSER) is not None
    assert chaos.maybe_fail_llm(Step.COMPOSER) is None


def test_a_scripted_chaos_injects_nothing_by_itself():
    chaos = Chaos.scripted()
    assert llm_sequence(chaos, 500) == ["ok"] * 500
    assert chaos.maybe_corrupt_sql(SQL) == SQL and chaos.maybe_fail_bq() is None


def test_bigquery_faults_are_classified_like_real_ones():
    chaos = Chaos.scripted().fail_bq(BqFault.UNAVAILABLE).fail_bq(BqFault.TIMEOUT)
    outage, timeout = chaos.maybe_fail_bq(), chaos.maybe_fail_bq()
    assert isinstance(outage, api_exc.ServiceUnavailable) and isinstance(timeout, TimeoutError)
    assert classify(outage, "query").kind is ErrorKind.BQ_UNAVAILABLE and classify(outage, "query").retryable
    assert classify(timeout, "query").kind is ErrorKind.QUERY_TIMEOUT


# bad SQL ----------------------------------------------------------------------------------------

def test_a_syntax_fault_leaves_sql_both_parsers_reject():
    broken = corrupt_sql(SQL, SqlFault.SYNTAX)
    assert broken.rstrip().endswith("ORDER BY") and broken.startswith(SQL)
    import sqlglot

    with pytest.raises(sqlglot.errors.ParseError):
        sqlglot.parse_one(broken, read="bigquery")


def test_an_unknown_column_fault_adds_a_column_that_does_not_exist_and_still_parses():
    broken = corrupt_sql(SQL, SqlFault.UNKNOWN_COLUMN)
    assert broken.startswith(f"SELECT {MISSING_COLUMN}, brand")
    import sqlglot

    sqlglot.parse_one(broken, read="bigquery")  # the SQL Guard sees valid SQL and the column check or BigQuery catches it


def test_an_unknown_column_goes_after_distinct_and_in_the_first_select():
    assert corrupt_sql("SELECT DISTINCT brand FROM t", SqlFault.UNKNOWN_COLUMN) == f"SELECT DISTINCT {MISSING_COLUMN}, brand FROM t"
    assert corrupt_sql("WITH a AS (SELECT x FROM t) SELECT x FROM a", SqlFault.UNKNOWN_COLUMN).startswith(f"WITH a AS (SELECT {MISSING_COLUMN}, x")


def test_sql_without_a_select_still_gets_a_syntax_error_and_a_trailing_semicolon_is_handled():
    assert corrupt_sql("not sql", SqlFault.UNKNOWN_COLUMN).endswith("ORDER BY")
    assert corrupt_sql("SELECT 1;", SqlFault.SYNTAX) == "SELECT 1\nORDER BY"


def test_a_scripted_bad_sql_is_replaced_once_and_the_repair_is_left_alone():
    chaos = Chaos.scripted().corrupt_next_sql(SqlFault.UNKNOWN_COLUMN)
    assert MISSING_COLUMN in chaos.maybe_corrupt_sql(SQL)
    assert chaos.maybe_corrupt_sql(SQL) == SQL  # the repaired query passes


def test_bad_sql_always_corrupts_every_query():
    chaos = Chaos.scripted().corrupt_next_sql(SqlFault.SYNTAX, times=99)
    assert all(chaos.maybe_corrupt_sql(SQL) != SQL for _ in range(10))


def test_random_mode_corrupts_at_most_one_query_a_turn():
    chaos = Chaos.from_level("harsh", 11)
    for turn in range(30):
        chaos.begin_turn()
        corrupted = sum(chaos.maybe_corrupt_sql(SQL) != SQL for _ in range(5))
        assert corrupted <= 1, turn
    assert any(item.startswith("sql:") for item in chaos.injected)


def test_an_unknown_level_is_refused():
    with pytest.raises(ValueError, match="unknown chaos level"):
        Chaos.from_level("apocalyptic")


def test_bigquery_faults_are_cut_short_below_the_runners_three_attempts():
    """Review finding: with a cap of 3 a run of faults could beat the 3 attempts, and BigQuery has no sibling to fall back to."""
    for level in PROFILES:
        chaos = Chaos.from_level(level, 9)
        run = longest = faults = 0
        for _ in range(4000):
            if chaos.maybe_fail_bq() is not None:
                run, faults = run + 1, faults + 1
            else:
                run = 0
            longest = max(longest, run)
        assert faults > 0 and longest <= 2, level
