"""SafeQueryRunner (plan 03): dry run, cost gate, timeout, labels, row cap, retries, masking, spans, accounting."""
import json
import re
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
import pytest
from google.api_core import exceptions as api_exc
from google.auth import exceptions as auth_exc
from google.cloud import bigquery

from src.config import BigQueryConfig, Settings
from src.data_access import safe_runner
from src.data_access.safe_runner import SafeQueryRunner, SystemQuery, job_labels, label_value
from src.errors import AgentError, ErrorKind
from src.logging_setup import setup_logging, shutdown_logging
from src.observability import Tracer, turn_metrics
from src.observability.metrics import read_runs
from tests.fake_bigquery import FakeClient, FakeRunner, Rule

REAL_ACCEPTED_QUERY_TYPES = safe_runner.ACCEPTED_QUERY_TYPES  # read before any fixture swaps in the stand-in

CANARY = "canary@example.test"
SELECT_ONE = "SELECT brand, SUM(sale_price) AS revenue FROM t GROUP BY brand"
PARAMS = [bigquery.ArrayQueryParameter("brands", "STRING", ["Brand A", "Brand B"])]


@dataclass
class Query:
    """A stand-in for plan 05's GuardedQuery with BigQuery-form params, so these tests need not build a full one."""

    sql: str = SELECT_ONE
    params: list = field(default_factory=lambda: list(PARAMS))


class Budget:
    def __init__(self, bytes_remaining: int = 10**12, time_error: AgentError | None = None, seconds: float = 3600.0) -> None:
        self.bytes_remaining = bytes_remaining
        self.time_error = time_error
        self.seconds = seconds
        self.charged: list[int] = []

    def remaining_seconds(self) -> float:
        return self.seconds

    def charge_bytes(self, n: int) -> None:
        self.charged.append(n)
        self.bytes_remaining -= n

    def check_time(self) -> None:
        if self.time_error is not None:
            raise self.time_error


def make_settings(tmp_path, **bigquery_options) -> Settings:
    return Settings(bigquery=BigQueryConfig(**bigquery_options), base_dir=tmp_path)


@pytest.fixture
def client(fake_bigquery) -> FakeClient:
    fake_bigquery.add("GROUP BY brand", df=pd.DataFrame({"brand": ["Brand A"], "revenue": [12.5]}), bytes_processed=2_000_000)
    return fake_bigquery


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture(autouse=True)
def the_test_query_counts_as_guarded(monkeypatch):
    """`Query` stands in for plan 05's GuardedQuery. Any other type is refused (see the tests at the end of this file,
    which use the real accepted types)."""
    monkeypatch.setattr(safe_runner, "ACCEPTED_QUERY_TYPES", (SystemQuery, Query))


@pytest.fixture
def runner(client, tmp_path, sleeps) -> SafeQueryRunner:
    return SafeQueryRunner(make_settings(tmp_path), FakeRunner(client), sleep=sleeps.append, jitter=lambda: 1.0)


def runner_with(client, tmp_path, sleeps, **bigquery_options) -> SafeQueryRunner:
    return SafeQueryRunner(make_settings(tmp_path, **bigquery_options), FakeRunner(client), sleep=sleeps.append, jitter=lambda: 1.0)


def spans_of(tracer: Tracer, trace_id: str) -> list[dict]:
    return [json.loads(line) for line in tracer.trace_path(trace_id).read_text(encoding="utf-8").splitlines()]


def span_named(spans: list[dict], name: str) -> dict:
    return next(s for s in spans if s["name"] == name)


# the happy path ----------------------------------------------------------------------------------

def test_a_query_runs_after_a_dry_run_and_reports_its_stats(runner, client):
    outcome = runner.run(Query())
    assert outcome.df.to_dict("records") == [{"brand": "Brand A", "revenue": 12.5}]
    assert (outcome.total_rows, outcome.bytes_processed, outcome.bytes_billed, outcome.cache_hit) == (1, 2_000_000, 2_000_000, False)
    assert outcome.elapsed_ms >= 0
    assert [job.dry_run for job in client.jobs] == [True, False]  # the dry run comes first


def test_the_dry_run_and_the_real_run_both_carry_the_query_parameters(runner, client):
    runner.run(Query())
    dry, real = client.jobs
    assert dry.config.query_parameters == PARAMS and real.config.query_parameters == PARAMS


def test_the_dry_run_skips_the_cache_and_the_real_run_has_the_cap_labels_and_a_job_timeout(runner, client):
    runner.run(Query())
    dry, real = client.jobs
    assert dry.config.dry_run and dry.config.use_query_cache is False
    assert real.config.maximum_bytes_billed == 536_870_912
    assert real.config.use_query_cache is True
    assert real.config.labels["app"] == "opsfleet-agent"
    assert int(real.config.job_timeout_ms) == 35_000  # 30 s client timeout plus a 5 s grace (the library hands it back as a string)


def test_the_librarys_own_retries_are_off_so_there_is_one_retry_loop(runner, client):
    runner.run(Query())
    for job in client.jobs:
        assert job.call_kwargs["retry"] is None and job.call_kwargs["job_retry"] is None
    assert client.real_jobs[0].result_kwargs["retry"] is None and client.real_jobs[0].result_kwargs["job_retry"] is None


def test_the_result_is_fetched_with_the_row_cap_the_timeout_and_no_storage_api_client(runner, client):
    runner.run(Query())
    job = client.real_jobs[0]
    assert job.result_kwargs["max_results"] == 1000 and job.result_kwargs["timeout"] == 30
    assert job.iterator.to_dataframe_kwargs == {"create_bqstorage_client": False}


def test_a_dry_run_alone_returns_the_estimate_and_starts_no_job(runner, client):
    assert runner.dry_run(Query()).bytes_processed == 2_000_000
    assert client.real_jobs == []


def test_a_client_that_cannot_be_created_for_lack_of_a_project_is_a_typed_error_not_a_crash(tmp_path, monkeypatch):
    def no_project(settings):
        raise OSError("Project was not passed and could not be determined from the environment.")

    monkeypatch.setattr(safe_runner, "make_runner", no_project)
    with pytest.raises(AgentError) as err:
        SafeQueryRunner(make_settings(tmp_path)).run(Query())
    assert err.value.kind == ErrorKind.BQ_UNAVAILABLE and not err.value.retryable
    assert "GOOGLE_CLOUD_PROJECT" in err.value.detail and "OSError" in err.value.detail
    assert "Project was not passed" not in err.value.detail  # only the class: the text can carry data


def test_the_runner_needs_no_credentials_until_it_runs_something(tmp_path, monkeypatch):
    def boom(settings):
        raise AssertionError("created a client too early")

    monkeypatch.setattr(safe_runner, "make_runner", boom)
    SafeQueryRunner(make_settings(tmp_path))  # must not raise


def test_a_missing_credential_is_a_typed_error_with_the_fix(tmp_path, monkeypatch):
    def no_credentials(settings):
        raise auth_exc.DefaultCredentialsError("none")

    monkeypatch.setattr(safe_runner, "make_runner", no_credentials)
    with pytest.raises(AgentError) as err:
        SafeQueryRunner(make_settings(tmp_path)).run(Query())
    assert err.value.kind == ErrorKind.BQ_UNAVAILABLE and "gcloud auth application-default login" in err.value.detail


# cost gate (F33, NFR6) ---------------------------------------------------------------------------

def test_a_dry_run_over_the_cap_is_too_expensive_and_the_query_job_never_starts(client, tmp_path, sleeps):
    client.add("GROUP BY brand", bytes_processed=600_000_000)
    with pytest.raises(AgentError) as err:
        runner_with(client, tmp_path, sleeps).run(Query())
    assert err.value.kind == ErrorKind.QUERY_TOO_EXPENSIVE and not err.value.retryable and not err.value.repairable
    assert client.real_jobs == []


def test_a_query_exactly_at_the_cap_still_runs(client, tmp_path, sleeps):
    client.add("GROUP BY brand", bytes_processed=536_870_912, df=pd.DataFrame({"brand": ["x"]}))
    assert runner_with(client, tmp_path, sleeps).run(Query()).total_rows == 1


def test_a_query_that_would_push_the_turn_over_its_byte_budget_is_too_expensive(runner, client):
    with pytest.raises(AgentError) as err:
        runner.run(Query(), Budget(bytes_remaining=1_000_000))  # the query needs 2,000,000
    assert err.value.kind == ErrorKind.QUERY_TOO_EXPENSIVE and "left for this turn" in err.value.detail
    assert client.real_jobs == []


def test_billed_bytes_are_charged_to_the_turn_budget(runner):
    budget = Budget()
    runner.run(Query(), budget)
    assert budget.charged == [2_000_000]


def test_a_cache_hit_bills_nothing_even_if_the_job_reports_bytes(client, tmp_path, sleeps):
    client.add("GROUP BY brand", cache_hit=True, bytes_billed=2_000_000, df=pd.DataFrame({"brand": ["x"]}))
    budget = Budget()
    outcome = runner_with(client, tmp_path, sleeps).run(Query(), budget)
    assert outcome.cache_hit and outcome.bytes_billed == 0 and budget.charged == [0]


def test_a_turn_that_is_out_of_time_starts_nothing(runner, client):
    budget = Budget(time_error=AgentError(ErrorKind.TURN_TIMEOUT))
    with pytest.raises(AgentError) as err:
        runner.run(Query(), budget)
    assert err.value.kind == ErrorKind.TURN_TIMEOUT and client.jobs == []


def test_only_plain_selects_run(client, tmp_path, sleeps):
    client.add("GROUP BY brand", statement_type="SCRIPT")
    with pytest.raises(AgentError) as err:
        runner_with(client, tmp_path, sleeps).run(Query())
    assert err.value.kind == ErrorKind.GUARD_VIOLATION and err.value.rule_id == "NOT_SELECT"
    assert client.real_jobs == []


def test_a_dry_run_that_reports_no_statement_type_is_refused_too(client, tmp_path, sleeps):
    client.add("GROUP BY brand", statement_type=None)
    with pytest.raises(AgentError) as err:
        runner_with(client, tmp_path, sleeps).run(Query())
    assert err.value.kind == ErrorKind.GUARD_VIOLATION and err.value.rule_id == "NOT_SELECT" and "None" in err.value.detail
    assert client.real_jobs == []  # fail closed: a change in what BigQuery reports is loud, not silent


# timeout and cancel ------------------------------------------------------------------------------

def test_a_timeout_cancels_the_job_and_is_not_retried(client, tmp_path, sleeps):
    client.add("GROUP BY brand", result_errors=[TimeoutError()])
    with pytest.raises(AgentError) as err:
        runner_with(client, tmp_path, sleeps).run(Query())
    assert err.value.kind == ErrorKind.QUERY_TIMEOUT
    assert len(client.real_jobs) == 1 and client.real_jobs[0].cancelled
    assert sleeps == []


def test_a_failed_cancel_does_not_hide_the_timeout(client, tmp_path, sleeps):
    client.add("GROUP BY brand", result_errors=[TimeoutError()], cancel_error=ConnectionError("down"))
    with pytest.raises(AgentError) as err:
        runner_with(client, tmp_path, sleeps).run(Query())
    assert err.value.kind == ErrorKind.QUERY_TIMEOUT


def test_ctrl_c_cancels_the_running_job_and_propagates(client, tmp_path, sleeps):
    client.add("GROUP BY brand", result_errors=[KeyboardInterrupt()])
    with pytest.raises(KeyboardInterrupt):
        runner_with(client, tmp_path, sleeps).run(Query())
    assert client.real_jobs[0].cancelled


def test_a_transient_failure_while_the_job_runs_cancels_it_before_the_retry_starts_another(client, tmp_path, sleeps):
    client.add("GROUP BY brand", result_errors=[api_exc.ServiceUnavailable("503")], df=pd.DataFrame({"brand": ["x"]}))
    outcome = runner_with(client, tmp_path, sleeps).run(Query())
    first, second = client.real_jobs
    assert outcome.total_rows == 1 and first.cancelled and not second.cancelled  # no second job left billing in the background


def test_a_broken_connection_while_the_rows_download_is_retried(client, tmp_path, sleeps):
    import requests

    client.add("GROUP BY brand", result_errors=[requests.exceptions.ChunkedEncodingError("connection broken")],
               df=pd.DataFrame({"brand": ["x"]}))
    outcome = runner_with(client, tmp_path, sleeps).run(Query())
    assert outcome.total_rows == 1 and len(client.real_jobs) == 2 and client.real_jobs[0].cancelled


def test_a_job_that_fails_for_another_reason_is_not_cancelled(client, tmp_path, sleeps):
    client.add("GROUP BY brand", result_errors=[api_exc.BadRequest("Unrecognized name: x at [1:8]")])
    with pytest.raises(AgentError) as err:
        runner_with(client, tmp_path, sleeps).run(Query())
    assert err.value.kind == ErrorKind.SQL_UNKNOWN_COLUMN and not client.real_jobs[0].cancelled


# row cap -----------------------------------------------------------------------------------------

def test_rows_above_the_cap_are_not_fetched_but_the_total_is_reported(client, tmp_path, sleeps):
    big = pd.DataFrame({"n": range(1500)})
    client.add("GROUP BY brand", df=big, total_rows=250_000)
    outcome = runner_with(client, tmp_path, sleeps, sql_row_limit=100).run(Query())
    assert len(outcome.df) == 100 and outcome.total_rows == 250_000
    assert client.real_jobs[0].result_kwargs["max_results"] == 100


def test_a_system_query_can_ask_for_a_bigger_row_cap(client, tmp_path, sleeps):
    client.add("GROUP BY brand", df=pd.DataFrame({"n": range(1500)}))
    outcome = runner_with(client, tmp_path, sleeps).run(SystemQuery(SELECT_ONE, max_rows=5000))
    assert len(outcome.df) == 1500 and client.real_jobs[0].result_kwargs["max_results"] == 5000


def test_a_query_from_the_sql_guard_cannot_lift_the_row_cap(client, tmp_path, sleeps):
    @dataclass
    class Sneaky(Query):
        max_rows: int = 10**9  # not a SystemQuery, so it is ignored

    client.add("GROUP BY brand", df=pd.DataFrame({"n": range(1500)}))
    outcome = runner_with(client, tmp_path, sleeps).run(Sneaky())
    assert len(outcome.df) == 1000 and client.real_jobs[0].result_kwargs["max_results"] == 1000


def test_a_row_cap_of_zero_or_less_is_refused_not_ignored():
    for bad in (0, -1):
        with pytest.raises(ValueError):
            SystemQuery("SELECT 1", max_rows=bad)


# labels ------------------------------------------------------------------------------------------

LABEL_KEY = re.compile(r"[a-z][a-z0-9_-]{0,62}")
LABEL_VALUE = re.compile(r"[a-z0-9_-]{0,63}")


def test_label_values_are_valid_bigquery_labels():
    assert label_value("Alice Smith!") == "alice_smith_"
    assert label_value("x" * 100) == "x" * 63
    assert label_value(None) == "unknown" and label_value("") == "unknown"
    assert label_value("日本") == "__"


def test_labels_use_the_user_hash_never_the_login_name(tmp_path):
    tracer = Tracer(tmp_path / "traces", tmp_path / "runs.jsonl")
    with tracer.start_turn("abc123def456", "conv-1", "t-1", trace_id="0a1b2c3d") as turn:
        turn.metrics.set_intent("sales", "why")
        labels = job_labels()
    assert labels == {"app": "opsfleet-agent", "env": "prototype", "user": "abc123def456", "trace": "0a1b2c3d", "kind": "why"}
    for key, value in labels.items():
        assert LABEL_KEY.fullmatch(key) and LABEL_VALUE.fullmatch(value)


def test_labels_outside_a_turn_say_system():
    labels = job_labels()
    assert labels["user"] == "system" and labels["trace"] == "none" and labels["kind"] == "system"


def test_the_real_job_gets_the_turn_labels(client, tmp_path, sleeps):
    tracer = Tracer(tmp_path / "traces", tmp_path / "runs.jsonl")
    with tracer.start_turn("abc123def456", "conv-1", "t-1", trace_id="0a1b2c3d"):
        runner_with(client, tmp_path, sleeps).run(Query())
    assert client.real_jobs[0].config.labels["user"] == "abc123def456"


# transient failures (brief R5) -------------------------------------------------------------------

def test_a_transient_failure_is_retried_with_growing_waits(client, tmp_path, sleeps):
    client.add("GROUP BY brand", query_errors=[api_exc.ServiceUnavailable("503"), api_exc.ServiceUnavailable("503")],
               df=pd.DataFrame({"brand": ["x"]}))
    outcome = runner_with(client, tmp_path, sleeps).run(Query())
    assert outcome.total_rows == 1 and len(client.real_jobs) == 3
    assert sleeps == [1.0, 2.0]  # base 1 s, doubled; jitter fixed at 1.0 here


def test_the_waits_have_jitter_and_a_cap(client, tmp_path):
    waits: list[float] = []
    runner = SafeQueryRunner(make_settings(tmp_path, transient_retries=5), FakeRunner(client), sleep=waits.append, jitter=lambda: 0.0)
    client.add("GROUP BY brand", dry_errors=[api_exc.ServiceUnavailable("x")] * 5, df=pd.DataFrame({"brand": ["x"]}))
    runner.run(Query())
    assert waits == [0.5, 1.0, 2.0, 4.0, 4.0]  # half of 1, 2, 4, 8, 8 (capped)


def test_retries_stop_at_the_configured_number_and_raise_unavailable(client, tmp_path, sleeps):
    client.add("GROUP BY brand", dry_errors=[api_exc.ServiceUnavailable("503")] * 5)
    with pytest.raises(AgentError) as err:
        runner_with(client, tmp_path, sleeps, transient_retries=1).run(Query())
    assert err.value.kind == ErrorKind.BQ_UNAVAILABLE
    assert len(client.dry_jobs) == 2 and len(sleeps) == 1


def test_no_retries_when_configured_to_zero(client, tmp_path, sleeps):
    client.add("GROUP BY brand", dry_errors=[api_exc.ServiceUnavailable("503")])
    with pytest.raises(AgentError):
        runner_with(client, tmp_path, sleeps, transient_retries=0).run(Query())
    assert len(client.dry_jobs) == 1 and sleeps == []


@pytest.mark.parametrize("exc", [
    api_exc.Forbidden("Access Denied"),
    api_exc.NotFound("Not found: Dataset"),
    api_exc.BadRequest("Syntax error: Unexpected identifier at [1:8]"),
])
def test_permanent_failures_are_not_retried(client, tmp_path, sleeps, exc):
    client.add("GROUP BY brand", dry_errors=[exc])
    with pytest.raises(AgentError):
        runner_with(client, tmp_path, sleeps).run(Query())
    assert len(client.dry_jobs) == 1 and sleeps == []


def test_a_retry_never_starts_after_the_turn_deadline(client, tmp_path, sleeps):
    client.add("GROUP BY brand", dry_errors=[api_exc.ServiceUnavailable("503")])

    class DeadlineAfterOneCall(Budget):
        calls = 0

        def check_time(self):
            self.calls += 1
            if self.calls > 1:  # the first check is before the dry run; the next one is before the retry
                raise AgentError(ErrorKind.TURN_TIMEOUT)

    with pytest.raises(AgentError) as err:
        runner_with(client, tmp_path, sleeps).run(Query(), DeadlineAfterOneCall())
    assert err.value.kind == ErrorKind.TURN_TIMEOUT and len(client.dry_jobs) == 1


def test_a_retry_wait_is_cut_to_the_time_the_turn_has_left(client, tmp_path, sleeps):
    client.add("GROUP BY brand", dry_errors=[api_exc.ServiceUnavailable("503")], df=pd.DataFrame({"brand": ["x"]}))
    runner_with(client, tmp_path, sleeps).run(Query(), Budget(seconds=0.3))  # the normal first wait is 1 s
    assert sleeps == [0.3]


def test_a_turn_that_is_already_out_of_time_does_not_sleep_before_giving_up(client, tmp_path, sleeps):
    client.add("GROUP BY brand", dry_errors=[api_exc.ServiceUnavailable("503")])

    class OutOfTimeAfterTheFirstCall(Budget):
        calls = 0

        def check_time(self):
            self.calls += 1
            if self.calls > 1:
                raise AgentError(ErrorKind.TURN_TIMEOUT)

    with pytest.raises(AgentError) as err:
        runner_with(client, tmp_path, sleeps).run(Query(), OutOfTimeAfterTheFirstCall())
    assert err.value.kind == ErrorKind.TURN_TIMEOUT and sleeps == []


def test_a_bug_in_our_own_code_is_not_disguised_as_a_bigquery_error(client, tmp_path, sleeps):
    client.add("GROUP BY brand", result_errors=[ValueError("a bug")])
    with pytest.raises(ValueError):
        runner_with(client, tmp_path, sleeps).run(Query())


# repairable errors come from the dry run, before any money is spent ------------------------------

def test_a_syntax_error_is_found_by_the_dry_run_and_is_repairable(client, tmp_path, sleeps):
    client.add("GROUP BY brand", dry_errors=[api_exc.BadRequest('Syntax error: Unexpected identifier "x" at [1:8]')])
    with pytest.raises(AgentError) as err:
        runner_with(client, tmp_path, sleeps).run(Query())
    assert err.value.kind == ErrorKind.SQL_SYNTAX and err.value.repairable
    assert client.real_jobs == []


# S6: nothing sensitive reaches the error, the trace or the log ----------------------------------

def test_a_canary_in_a_bigquery_error_is_masked_in_the_error_the_span_and_the_log(client, tmp_path, sleeps):
    from src.config import load_settings

    (tmp_path / "config.yaml").write_text("app: {}\n", encoding="utf-8")
    settings = load_settings(tmp_path / "config.yaml", load_env_file=False)
    setup_logging(settings)
    tracer = Tracer(tmp_path / "traces", tmp_path / "runs.jsonl")
    client.add("GROUP BY brand", dry_errors=[api_exc.BadRequest(f"Syntax error: Unexpected keyword {CANARY} at [1:30]")])
    runner = SafeQueryRunner(settings, FakeRunner(client), sleep=sleeps.append)
    try:
        with pytest.raises(AgentError) as err:
            with tracer.start_turn("hash0001", "conv-1", "t-1", trace_id="0a1b2c3d"):
                runner.run(Query(sql=f"SELECT 1 FROM t WHERE x = '{CANARY}' GROUP BY brand"))
    finally:
        shutdown_logging()
    assert CANARY not in str(err.value) and CANARY not in err.value.detail and "[EMAIL_MASKED]" in err.value.detail
    trace_text = tracer.trace_path("0a1b2c3d").read_text(encoding="utf-8")
    assert CANARY not in trace_text
    dry = span_named(spans_of(tracer, "0a1b2c3d"), "bq.dry_run")
    assert dry["status"] == "error" and dry["error_kind"] == "SQL_SYNTAX" and "[EMAIL_MASKED]" in dry["attrs"]["sql"]
    log_file = settings.log_path
    assert not log_file.exists() or CANARY not in log_file.read_text(encoding="utf-8")
    assert CANARY not in (tmp_path / "runs.jsonl").read_text(encoding="utf-8")


def test_outcomes_compare_by_identity_so_equality_never_touches_the_rows(runner):
    first, second = runner.run(Query()), runner.run(Query())
    assert first == first and first != second and first not in [second]  # a DataFrame comparison here would raise


def test_raw_rows_never_reach_the_span_or_the_repr(client, tmp_path, sleeps):
    client.add("GROUP BY brand", df=pd.DataFrame({"brand": ["SECRET-CELL"], "revenue": [1.0]}))
    tracer = Tracer(tmp_path / "traces", tmp_path / "runs.jsonl")
    with tracer.start_turn("hash0001", "conv-1", "t-1", trace_id="0a1b2c3d"):
        outcome = runner_with(client, tmp_path, sleeps).run(Query())
    assert "SECRET-CELL" not in tracer.trace_path("0a1b2c3d").read_text(encoding="utf-8")
    assert "SECRET-CELL" not in repr(outcome) and "SECRET-CELL" not in str(outcome)


# spans and metrics (plan 02) ---------------------------------------------------------------------

def test_a_query_is_a_span_and_a_metrics_call(client, tmp_path, sleeps):
    tracer = Tracer(tmp_path / "traces", tmp_path / "runs.jsonl")
    with tracer.start_turn("hash0001", "conv-1", "t-1", trace_id="0a1b2c3d"):
        runner_with(client, tmp_path, sleeps).run(Query())
    spans = spans_of(tracer, "0a1b2c3d")
    dry, query = span_named(spans, "bq.dry_run"), span_named(spans, "bq.query")
    assert dry["kind"] == query["kind"] == "query"
    assert dry["attrs"]["bytes_processed"] == 2_000_000 and dry["attrs"]["param_names"] == ["brands"]
    assert query["attrs"]["bytes_billed"] == 2_000_000 and query["attrs"]["rows"] == 1 and query["attrs"]["cache_hit"] is False
    assert query["attrs"]["total_rows"] == 1 and query["attrs"]["truncated"] is False
    assert query["parent_id"] == dry["parent_id"]  # siblings under the turn, not nested
    row = read_runs(tracer.metrics_file)[0][0]
    assert row["bq_queries"] == 1 and row["bytes_billed"] == 2_000_000


def test_a_dry_run_is_a_span_only_and_bills_nothing(client, tmp_path, sleeps):
    tracer = Tracer(tmp_path / "traces", tmp_path / "runs.jsonl")
    with tracer.start_turn("hash0001", "conv-1", "t-1", trace_id="0a1b2c3d"):
        runner_with(client, tmp_path, sleeps).dry_run(Query())
        queries = turn_metrics().bq_queries
    assert queries == 0
    assert [s["name"] for s in spans_of(tracer, "0a1b2c3d")] == ["turn", "bq.dry_run"]


def test_a_failed_query_counts_its_error_kind_in_the_metrics_row(client, tmp_path, sleeps):
    client.add("GROUP BY brand", bytes_processed=600_000_000)
    tracer = Tracer(tmp_path / "traces", tmp_path / "runs.jsonl")
    with pytest.raises(AgentError):
        with tracer.start_turn("hash0001", "conv-1", "t-1", trace_id="0a1b2c3d"):
            runner_with(client, tmp_path, sleeps).run(Query())
    assert read_runs(tracer.metrics_file)[0][0]["error_kinds"] == ["QUERY_TOO_EXPENSIVE"]


def test_a_retry_is_an_event_on_the_span_not_an_error(client, tmp_path, sleeps):
    client.add("GROUP BY brand", dry_errors=[api_exc.ServiceUnavailable("503")], df=pd.DataFrame({"brand": ["x"]}))
    tracer = Tracer(tmp_path / "traces", tmp_path / "runs.jsonl")
    with tracer.start_turn("hash0001", "conv-1", "t-1", trace_id="0a1b2c3d"):
        runner_with(client, tmp_path, sleeps).run(Query())
    dry = span_named(spans_of(tracer, "0a1b2c3d"), "bq.dry_run")
    assert dry["status"] == "ok" and [e["name"] for e in dry["events"]] == ["retry"]
    assert dry["events"][0]["attrs"]["error_kind"] == "BQ_UNAVAILABLE"
    assert read_runs(tracer.metrics_file)[0][0]["error_kinds"] == []


# system queries and table metadata ---------------------------------------------------------------

def test_a_system_query_goes_through_the_same_dry_run_and_cap(client, tmp_path, sleeps):
    client.add("FROM `bigquery-public-data.thelook_ecommerce.products`", df=pd.DataFrame({"n": [1]}))
    runner = runner_with(client, tmp_path, sleeps)
    outcome = runner.run(SystemQuery(f"SELECT COUNT(*) AS n FROM {runner.table_ref('products')}"))
    assert outcome.total_rows == 1 and [j.dry_run for j in client.jobs] == [True, False]


def test_table_ref_refuses_odd_characters(tmp_path):
    runner = SafeQueryRunner(make_settings(tmp_path, dataset="p.d"), FakeRunner(FakeClient()))
    assert runner.table_ref("orders") == "`p.d.orders`"
    with pytest.raises(AgentError):
        runner.table_ref("orders`; DROP TABLE x; --")
    with pytest.raises(AgentError):
        SafeQueryRunner(make_settings(tmp_path, dataset="p.d`x"), FakeRunner(FakeClient())).table_ref("orders")


def test_table_metadata_calls_are_retried_and_classified(client, tmp_path, sleeps):
    client.table_errors.append(api_exc.ServiceUnavailable("503"))
    table = runner_with(client, tmp_path, sleeps).get_table("orders")
    assert table.num_bytes == 9_000_000 and len(client.table_calls) == 2
    with pytest.raises(AgentError) as err:
        runner_with(client, tmp_path, sleeps).get_table("nope")
    assert err.value.kind == ErrorKind.BQ_UNAVAILABLE and "bigquery.dataset" in err.value.detail


# time: a call never outlasts the turn --------------------------------------------------------------------------

def test_each_call_is_given_only_the_time_the_turn_has_left(runner, client):
    runner.run(Query(), Budget(seconds=5))
    dry, real = client.jobs
    assert dry.call_kwargs["timeout"] == 5 and real.call_kwargs["timeout"] == 5 and real.result_kwargs["timeout"] == 5
    assert int(real.config.job_timeout_ms) == 10_000  # the same 5 s plus the 5 s grace


def test_with_plenty_of_time_left_the_configured_timeout_applies(runner, client):
    runner.run(Query(), Budget(seconds=3600))
    assert client.jobs[0].call_kwargs["timeout"] == 30 and client.real_jobs[0].result_kwargs["timeout"] == 30


def test_a_turn_that_is_almost_out_of_time_still_gives_the_call_a_positive_timeout(runner, client):
    runner.run(Query(), Budget(seconds=0.0))  # check_time has not raised yet, but nothing is left
    assert client.jobs[0].call_kwargs["timeout"] == pytest.approx(0.1)


def test_a_timeout_caused_by_the_turns_deadline_is_a_turn_timeout_and_the_job_is_cancelled(client, tmp_path, sleeps):
    client.add("GROUP BY brand", result_errors=[TimeoutError()])

    class ExpiresWhileTheJobRuns(Budget):
        def check_time(self):
            if client.real_jobs and client.real_jobs[0].result_kwargs is not None:
                raise AgentError(ErrorKind.TURN_TIMEOUT)

    with pytest.raises(AgentError) as err:
        runner_with(client, tmp_path, sleeps).run(Query(), ExpiresWhileTheJobRuns())
    assert err.value.kind == ErrorKind.TURN_TIMEOUT and client.real_jobs[0].cancelled


# a stalled submission is a connection problem, not a slow question ----------------------------------------------

def test_a_stalled_job_submission_is_retried_and_not_reported_as_a_slow_question(client, tmp_path, sleeps):
    import requests

    client.add("GROUP BY brand", query_errors=[requests.exceptions.ReadTimeout("jobs.insert stalled")], df=pd.DataFrame({"brand": ["x"]}))
    outcome = runner_with(client, tmp_path, sleeps).run(Query())
    assert outcome.total_rows == 1 and len(client.real_jobs) == 2 and sleeps == [1.0]


def test_a_submission_that_keeps_stalling_ends_as_unavailable_not_as_query_timeout(client, tmp_path, sleeps):
    import requests

    client.add("GROUP BY brand", query_errors=[requests.exceptions.ReadTimeout("stalled")] * 5)
    with pytest.raises(AgentError) as err:
        runner_with(client, tmp_path, sleeps, transient_retries=1).run(Query())
    assert err.value.kind == ErrorKind.BQ_UNAVAILABLE and err.value.retryable


def test_a_job_that_runs_too_long_is_still_a_query_timeout(client, tmp_path, sleeps):
    client.add("GROUP BY brand", result_errors=[TimeoutError()])
    with pytest.raises(AgentError) as err:
        runner_with(client, tmp_path, sleeps).run(Query())
    assert err.value.kind == ErrorKind.QUERY_TIMEOUT


# the read-only backstop is visible in the metrics ----------------------------------------------------------------

def test_a_refused_statement_counts_as_an_sql_guard_block_and_is_on_the_span(client, tmp_path, sleeps):
    client.add("GROUP BY brand", statement_type="SCRIPT")
    tracer = Tracer(tmp_path / "traces", tmp_path / "runs.jsonl")
    with pytest.raises(AgentError):
        with tracer.start_turn("hash0001", "conv-1", "t-1", trace_id="0a1b2c3d"):
            runner_with(client, tmp_path, sleeps).run(Query())
    dry = span_named(spans_of(tracer, "0a1b2c3d"), "bq.dry_run")
    assert dry["attrs"]["statement_type"] == "SCRIPT" and dry["error_kind"] == "GUARD_VIOLATION"
    decision = next(e for e in dry["events"] if e["name"] == "guard.decision")
    assert decision["attrs"]["guard"] == "sql" and decision["attrs"]["outcome"] == "block" and decision["attrs"]["rule"] == "NOT_SELECT"
    row = read_runs(tracer.metrics_file)[0][0]
    assert row["guard_blocks"]["sql"] == 1 and row["error_kinds"] == ["GUARD_VIOLATION"]


# a dry run without an estimate is not "free" ----------------------------------------------------------------------

def test_a_dry_run_that_reports_no_byte_estimate_is_refused_not_read_as_zero(client, tmp_path, sleeps):
    client.add("GROUP BY brand", bytes_processed=None)
    with pytest.raises(AgentError) as err:
        runner_with(client, tmp_path, sleeps).run(Query(), Budget(bytes_remaining=1))
    assert err.value.kind == ErrorKind.INTERNAL and "did not report" in err.value.detail
    assert client.real_jobs == []


def test_a_scan_of_zero_bytes_is_a_real_answer(client, tmp_path, sleeps):
    client.add("GROUP BY brand", bytes_processed=0, df=pd.DataFrame({"brand": ["x"]}))
    assert runner_with(client, tmp_path, sleeps).run(Query(), Budget(bytes_remaining=1)).total_rows == 1


# jobs that were cancelled are paid for, at their estimate ----------------------------------------------------------

def test_a_timed_out_job_is_charged_at_its_estimate_to_the_metrics_and_the_budget(client, tmp_path, sleeps):
    client.add("GROUP BY brand", result_errors=[TimeoutError()], bytes_processed=2_000_000)
    tracer = Tracer(tmp_path / "traces", tmp_path / "runs.jsonl")
    budget = Budget()
    with pytest.raises(AgentError) as err:
        with tracer.start_turn("hash0001", "conv-1", "t-1", trace_id="0a1b2c3d"):
            runner_with(client, tmp_path, sleeps).run(Query(), budget)
    assert err.value.kind == ErrorKind.QUERY_TIMEOUT
    row = read_runs(tracer.metrics_file)[0][0]
    assert row["bq_queries"] == 1 and row["bytes_billed"] == 2_000_000  # a loop of timeouts would show here
    assert budget.charged == [2_000_000]
    query = span_named(spans_of(tracer, "0a1b2c3d"), "bq.query")
    assert query["attrs"]["abandoned_jobs"] == 1 and query["attrs"]["abandoned_bytes_estimated"] == 2_000_000


def test_a_retry_after_a_cancelled_job_counts_both_jobs(client, tmp_path, sleeps):
    client.add("GROUP BY brand", result_errors=[api_exc.ServiceUnavailable("503")], bytes_processed=2_000_000,
               df=pd.DataFrame({"brand": ["x"]}))
    tracer = Tracer(tmp_path / "traces", tmp_path / "runs.jsonl")
    budget = Budget()
    with tracer.start_turn("hash0001", "conv-1", "t-1", trace_id="0a1b2c3d"):
        runner_with(client, tmp_path, sleeps).run(Query(), budget)
    row = read_runs(tracer.metrics_file)[0][0]
    assert row["bq_queries"] == 2 and row["bytes_billed"] == 4_000_000  # the cancelled job's estimate plus the real bill
    assert budget.charged == [2_000_000, 2_000_000]


def test_a_budget_that_refuses_the_charge_does_not_replace_the_error_in_flight(client, tmp_path, sleeps):
    client.add("GROUP BY brand", result_errors=[TimeoutError()])

    class RefusesCharges(Budget):
        def charge_bytes(self, n):
            raise AgentError(ErrorKind.BUDGET_EXHAUSTED)

    with pytest.raises(AgentError) as err:
        runner_with(client, tmp_path, sleeps).run(Query(), RefusesCharges())
    assert err.value.kind == ErrorKind.QUERY_TIMEOUT


def test_a_job_that_finishes_without_trouble_has_no_abandoned_charge(runner):
    budget = Budget()
    runner.run(Query(), budget)
    assert budget.charged == [2_000_000]


def test_a_400_job_timeout_ends_as_a_query_timeout_and_the_job_is_cancelled_not_repaired(client, tmp_path, sleeps):
    client.add("GROUP BY brand", result_errors=[api_exc.BadRequest("Job timed out after 35 sec", errors=[{"reason": "timeout"}])])
    with pytest.raises(AgentError) as err:
        runner_with(client, tmp_path, sleeps).run(Query())
    assert err.value.kind == ErrorKind.QUERY_TIMEOUT and not err.value.repairable
    assert client.real_jobs[0].cancelled and len(client.real_jobs) == 1


def test_a_wrong_dataset_named_by_the_model_is_left_to_the_repair_step(client, tmp_path, sleeps):
    client.add("GROUP BY brand", dry_errors=[api_exc.NotFound("Not found: Dataset my-project:thelook_ecommerce was not found")])
    with pytest.raises(AgentError) as err:
        runner_with(client, tmp_path, sleeps).run(Query())
    assert err.value.kind == ErrorKind.SQL_UNKNOWN_COLUMN and err.value.repairable


# only queries that came out of the SQL Guard run ----------------------------------------------------------------------

def test_a_query_that_did_not_come_from_the_sql_guard_never_runs(client, tmp_path, sleeps):
    @dataclass
    class RawModelSql:  # has .sql and .params, so a structural check would let it through
        sql: str = SELECT_ONE
        params: list = field(default_factory=list)

    tracer = Tracer(tmp_path / "traces", tmp_path / "runs.jsonl")
    for raw in (RawModelSql(), SELECT_ONE):
        with pytest.raises(AgentError) as err:
            with tracer.start_turn("hash0001", "conv-1", "t-1"):
                runner_with(client, tmp_path, sleeps).run(raw)
        assert err.value.kind == ErrorKind.GUARD_VIOLATION and err.value.rule_id == "UNGUARDED_QUERY"
    assert client.jobs == []  # not even a dry run
    rows = read_runs(tracer.metrics_file)[0]
    assert [row["guard_blocks"]["sql"] for row in rows] == [1, 1]


def use_the_real_accepted_types(monkeypatch):
    """Put back the real tuple. `monkeypatch.undo()` would also undo the fake BigQuery client and the cleaned environment
    that every test relies on, and a test that built its own client would then reach the real service."""
    monkeypatch.setattr(safe_runner, "ACCEPTED_QUERY_TYPES", REAL_ACCEPTED_QUERY_TYPES)


def test_the_accepted_types_are_the_system_query_and_the_sql_guards_guarded_query():
    from src.guards.query import GuardedQuery

    assert REAL_ACCEPTED_QUERY_TYPES == (SystemQuery, GuardedQuery)


def guarded(sql: str = SELECT_ONE, params=()):
    from src.guards.query import GuardedQuery

    return GuardedQuery.issue(
        sql=sql, params=tuple(params), tables=frozenset({"orders"}), id_columns=(), per_customer=False, demographic=False,
        k_column=None, limit=1000, rewrites=(), fingerprint="f" * 64, scope_label="all brands",
    )


def test_a_real_guarded_query_runs_and_its_array_param_reaches_bigquery_as_an_array_query_parameter(client, tmp_path, sleeps, monkeypatch):
    from src.guards.query import ArrayParam

    use_the_real_accepted_types(monkeypatch)
    gq = guarded(params=[ArrayParam("brands", "STRING", ("Brand A", "Brand B"))])
    outcome = runner_with(client, tmp_path, sleeps).run(gq)
    assert list(outcome.df["brand"]) == ["Brand A"]
    for job in client.jobs:  # the dry run and the real job
        (param,) = job.config.query_parameters
        assert isinstance(param, bigquery.ArrayQueryParameter)
        assert (param.name, param.array_type, param.values) == ("brands", "STRING", ["Brand A", "Brand B"])


def test_a_guarded_query_without_params_sends_none(client, tmp_path, sleeps, monkeypatch):
    use_the_real_accepted_types(monkeypatch)
    runner_with(client, tmp_path, sleeps).run(guarded())
    assert all(job.config.query_parameters == [] for job in client.jobs)


def test_a_look_alike_of_the_guarded_query_is_still_refused(client, tmp_path, sleeps, monkeypatch):
    from dataclasses import make_dataclass

    use_the_real_accepted_types(monkeypatch)
    look_alike = make_dataclass("GuardedQuery", [("sql", str), ("params", tuple)])(SELECT_ONE, ())  # same name, same fields
    with pytest.raises(AgentError) as err:
        runner_with(client, tmp_path, sleeps).run(look_alike)
    assert err.value.rule_id == "UNGUARDED_QUERY" and client.jobs == []


# no duplicate sources of truth ------------------------------------------------------------------------------------------

def test_the_dataset_comes_from_the_settings_only():
    import inspect

    from src.data_access.bq_client import BigQueryRunner

    assert "dataset_id" not in inspect.signature(BigQueryRunner.__init__).parameters
    assert not hasattr(BigQueryRunner, "dataset_id")


def test_the_dry_run_result_carries_only_what_can_vary():
    import dataclasses

    assert [f.name for f in dataclasses.fields(safe_runner.DryRunInfo)] == ["bytes_processed"]


# chaos (plan 04, F37) ---------------------------------------------------------------------------------------------------

def chaos_runner(client, tmp_path, sleeps, chaos) -> SafeQueryRunner:
    return SafeQueryRunner(make_settings(tmp_path), FakeRunner(client), sleep=sleeps.append, jitter=lambda: 1.0, chaos=chaos)


def test_an_injected_outage_is_retried_with_the_same_waits_as_a_real_one(client, tmp_path, sleeps):
    from src.llm.chaos import BqFault, Chaos

    chaos = Chaos.scripted().fail_bq(BqFault.UNAVAILABLE, times=2)
    outcome = chaos_runner(client, tmp_path, sleeps, chaos).run(Query())
    assert outcome.total_rows == 1 and sleeps == [1.0, 2.0]  # the same growing waits as a transient BigQuery failure
    assert chaos.injected == ["bq:unavailable", "bq:unavailable"]


def test_an_outage_that_does_not_end_is_bq_unavailable_and_starts_no_job(client, tmp_path, sleeps):
    from src.llm.chaos import BqFault, Chaos

    chaos = Chaos.scripted().fail_bq(BqFault.TIMEOUT, times=99)  # in the dry run a timeout means BigQuery does not answer
    with pytest.raises(AgentError) as err:
        chaos_runner(client, tmp_path, sleeps, chaos).run(Query())
    assert err.value.kind == ErrorKind.BQ_UNAVAILABLE and client.jobs == []


def test_an_injected_timeout_while_the_query_runs_is_a_query_timeout(client, tmp_path, sleeps):
    class FailOnTheSecondCall:  # the first call is the dry run, the second is the real job
        calls = 0

        def maybe_fail_bq(self):
            self.calls += 1
            return TimeoutError("injected") if self.calls == 2 else None

    with pytest.raises(AgentError) as err:
        chaos_runner(client, tmp_path, sleeps, FailOnTheSecondCall()).run(Query())
    assert err.value.kind == ErrorKind.QUERY_TIMEOUT and sleeps == []


def test_the_startup_schema_read_is_never_faulted(client, tmp_path, sleeps):
    from src.llm.chaos import BqFault, Chaos

    chaos = Chaos.scripted().fail_bq(BqFault.UNAVAILABLE, times=99)
    assert chaos_runner(client, tmp_path, sleeps, chaos).get_table("orders") is not None
    assert chaos.injected == []


def test_an_injected_fault_is_marked_on_the_span(client, tmp_path, sleeps):
    from src.llm.chaos import BqFault, Chaos

    tracer = Tracer(tmp_path / "traces", tmp_path / "runs.jsonl")
    chaos = Chaos.scripted().fail_bq(BqFault.UNAVAILABLE)
    with tracer.start_turn("hash0001", "conv-1", "t-1", trace_id="0a1b2c3d", chaos=True):
        chaos_runner(client, tmp_path, sleeps, chaos).run(Query())
    spans = spans_of(tracer, "0a1b2c3d")
    assert span_named(spans, "bq.dry_run")["attrs"]["chaos_fault"] == "unavailable"
    assert read_runs(tracer.metrics_file)[0][0]["chaos"] is True and read_runs(tracer.metrics_file)[0][0]["error_kinds"] == []


def test_our_own_startup_queries_are_never_faulted_only_the_agents_queries_are(client, tmp_path, sleeps):
    """Review finding: chaos was withheld by phase, so the startup brand list and the A23 count could be faulted and the chat
    could not start. The rule is by query type: a SystemQuery is our fixed SQL, never faulted."""
    from src.llm.chaos import BqFault, Chaos

    chaos = Chaos.scripted().fail_bq(BqFault.UNAVAILABLE, times=99)
    outcome = chaos_runner(client, tmp_path, sleeps, chaos).run(SystemQuery(SELECT_ONE))
    assert outcome.total_rows == 1 and chaos.injected == [] and sleeps == []
    with pytest.raises(AgentError):  # the agent's own query (here the stand-in for a guarded one) is faulted
        chaos_runner(client, tmp_path, sleeps, chaos).run(Query())
    assert chaos.injected  # and the fault was injected
