"""`SafeQueryRunner`: the only way agent code runs SQL on BigQuery (plan 03, S2, S6, F33, NFR6, NFR7).

Every query gets a dry run, a byte cap, a timeout, job labels and a row cap. BigQuery failures leave here as
typed `AgentError`s (`errors_map.py`). The SQL Guard (plan 05) runs before this; this layer does not parse SQL.

Raw rows: `QueryOutcome.df` goes straight to the Result Guard in the node that ran the query. It is never put in
graph state, a span or a log line (its `repr` hides it too).
"""
from __future__ import annotations

import logging
import random
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence, TypeVar

import pandas as pd
from google.cloud import bigquery

from src.config import Settings
from src.data_access.bq_client import BigQueryRunner
from src.data_access.errors_map import Phase, classify, job_may_still_run, setup_error
from src.errors import AgentError, ErrorKind, crash_summary
from src.guards.query import ArrayParam, GuardedQuery
from src.llm.budget import call_timeout
from src.observability import current_trace_id, guard_decision, span, turn_metrics
from src.observability.tracing import Span

log = logging.getLogger(__name__)

T = TypeVar("T")

APP_LABEL = "opsfleet-agent"
ENV_LABEL = "prototype"
READ_ONLY_STATEMENT = "SELECT"

# Waits between transient retries: 1 s, 2 s, 4 s ... up to 8 s, each scaled by 50% to 100% (jitter). (impl)
RETRY_BASE_S = 1.0
RETRY_CAP_S = 8.0
# The server-side job timeout is a backstop for a client that cannot reach BigQuery to cancel. It sits a little
# after the client timeout, so the client normally gives up first and cancels the job itself.
JOB_TIMEOUT_GRACE_S = 5.0
CANCEL_TIMEOUT_S = 5.0


class QueryLike(Protocol):
    """What the runner reads from a query. Having these fields is not enough to be run: see `ACCEPTED_QUERY_TYPES`."""

    sql: str
    params: Sequence[Any]  # BigQuery query parameters, for example ArrayQueryParameter("brands", "STRING", [...])


class BudgetLike(Protocol):
    """What the runner needs from plan 04's `TurnBudget`."""

    @property
    def bytes_remaining(self) -> int: ...

    def charge_bytes(self, n: int) -> None: ...

    def check_time(self) -> None: ...

    def remaining_seconds(self) -> float: ...


class FaultSource(Protocol):
    """What the runner needs from plan 04's `Chaos` (`--chaos`): an exception to fail the next call with, or None."""

    def maybe_fail_bq(self) -> BaseException | None: ...


@dataclass(frozen=True)
class SystemQuery:
    """Fixed SQL that our own code wrote (startup checks, `doctor`), not the model.

    It skips the SQL Guard on purpose: the guard would scope it to one user's brands, and these queries need
    every brand. It still gets the dry run, the byte cap, the timeout and the labels.

    `max_rows` replaces the default row cap (`bigquery.sql_row_limit`) for a query that needs more rows, such as
    the full brand list. Only this type can carry it, so a query that came through the SQL Guard cannot lift the cap.
    """

    sql: str
    params: tuple[Any, ...] = ()
    max_rows: int | None = None

    def __post_init__(self) -> None:
        if self.max_rows is not None and self.max_rows < 1:
            raise ValueError("max_rows must be 1 or more")


# The only query types `run` and `dry_run` accept. A structural match is not enough: any object with `.sql` and
# `.params` would otherwise run, including raw model SQL with no brand scope. A `GuardedQuery` comes from plan 05's
# SQL Guard and nowhere else, and carries a seal that `_require_accepted` checks (`src/guards/query.py`); a `SystemQuery`
# is our own fixed SQL. Nothing else should ever be added here.
ACCEPTED_QUERY_TYPES: tuple[type, ...] = (SystemQuery, GuardedQuery)


@dataclass(frozen=True)
class DryRunInfo:
    bytes_processed: int


@dataclass(eq=False)  # the generated __eq__ would compare DataFrames and raise
class QueryOutcome:
    df: pd.DataFrame = field(repr=False)  # raw rows: straight to the Result Guard, never into graph state
    total_rows: int  # the full count BigQuery reports, even when `df` was cut at the row cap
    bytes_processed: int
    bytes_billed: int
    elapsed_ms: int
    cache_hit: bool


def make_runner(settings: Settings) -> BigQueryRunner:
    """The one place a BigQuery client is created. Tests replace this with a fake."""
    return BigQueryRunner(project_id=settings.gcp_project)


def label_value(value: str | None, default: str = "unknown") -> str:
    """A valid BigQuery label value: lowercase letters, digits, underscores and dashes, up to 63 characters."""
    cleaned = re.sub(r"[^a-z0-9_-]", "_", (value or "").lower())[:63]
    return cleaned or default


def job_labels() -> dict[str, str]:
    """Cost attribution labels. The user is the hash from the turn's metrics, never the login name."""
    if current_trace_id() is None:  # startup and `doctor` queries run outside a turn
        user, trace, kind = "system", "none", "system"
    else:
        metrics = turn_metrics()
        user, trace, kind = label_value(metrics.user_hash), label_value(metrics.trace_id), label_value(metrics.turn_kind)
    return {"app": APP_LABEL, "env": ENV_LABEL, "user": user, "trace": trace, "kind": kind}


def _faultable(gq: QueryLike) -> bool:
    """Chaos hits the queries the agent runs for a question. A `SystemQuery` (startup brand list, latest date, `doctor`)
    is our own fixed SQL that the chat needs in order to start, so it is never faulted."""
    return not isinstance(gq, SystemQuery)


def _param_names(gq: QueryLike) -> list[str]:
    return [str(getattr(param, "name", "?")) for param in gq.params]


def _bq_params(gq: QueryLike) -> list[Any]:
    """The query's parameters as BigQuery's. The SQL Guard hands over plain `ArrayParam`s because nothing outside
    this package may import the BigQuery library; a `SystemQuery` brings its own, already in BigQuery's form."""
    return [
        bigquery.ArrayQueryParameter(param.name, param.element_type, list(param.values)) if isinstance(param, ArrayParam) else param
        for param in gq.params
    ]


class SafeQueryRunner:
    def __init__(
        self,
        settings: Settings,
        runner: BigQueryRunner | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
        chaos: FaultSource | None = None,
    ) -> None:
        self._settings = settings
        self._config = settings.bigquery
        self._runner = runner
        self._lock = threading.Lock()  # several metadata reads run at once at startup; the client is made once
        self._sleep = sleep
        self._jitter = jitter
        self._chaos = chaos  # `--chaos` (plan 04): faults replace a dry run or a job call and are retried like real ones

    @property
    def _bq(self) -> BigQueryRunner:
        """The raw client holder. Private on purpose: everything that talks to BigQuery goes through this class,
        so the dry run, byte cap, timeout and labels cannot be skipped. Built on first use, so creating a
        SafeQueryRunner never needs credentials."""
        if self._runner is None:
            with self._lock:
                if self._runner is None:
                    try:
                        self._runner = make_runner(self._settings)
                    except Exception as exc:  # noqa: BLE001 - whatever stops the client from being created is a setup problem
                        raise (classify(exc, "metadata") or setup_error(exc)) from None
        return self._runner

    def table_ref(self, table_name: str) -> str:
        """The backticked, fully qualified name for a system query. The dataset comes from config."""
        dataset = self._config.dataset
        if not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_]+", dataset) or not re.fullmatch(r"[A-Za-z0-9_]+", table_name):
            raise AgentError(ErrorKind.INTERNAL, detail="bigquery.dataset or table name has unexpected characters")
        return f"`{dataset}.{table_name}`"

    # table metadata ------------------------------------------------------------------------------

    def get_table(self, table_name: str) -> bigquery.Table:
        """Schema and size of one table. A metadata call: free, it scans no data."""
        ref = f"{self._config.dataset}.{table_name}"
        return self._attempt(
            lambda: self._bq.client.get_table(ref, retry=None, timeout=self._config.query_timeout_s), "metadata"
        )

    # dry run -------------------------------------------------------------------------------------

    def dry_run(self, gq: QueryLike, budget: BudgetLike | None = None) -> DryRunInfo:
        """Ask BigQuery to parse the query and say how much it would scan. Free. Raises the typed errors.

        Over the per-query cap, or over what is left for the turn: `QUERY_TOO_EXPENSIVE`, and no job is started.
        A dry run that reports no statement type or no byte estimate is refused too: without them neither the
        read-only check nor the cost gate can be trusted.
        """
        self._require_accepted(gq)
        with span("bq.dry_run", "query", sql=gq.sql, param_names=_param_names(gq)) as sp:
            self._check_time(budget)
            job = self._attempt(
                lambda: self._start_dry_run(gq, self._call_timeout(budget)), "dry_run", sp, budget, faultable=_faultable(gq)
            )
            statement = getattr(job, "statement_type", None)
            sp.set(statement_type=statement)
            # BigQuery's own parse backs up the SQL Guard: only a plain SELECT runs. A type that is missing counts as
            # a refusal too (fail closed), so a change in what a dry run reports is loud, not silent. It is counted
            # as an SQL guard block, because a query that gets here has slipped past plan 05's checks.
            if statement != READ_ONLY_STATEMENT:
                guard_decision("sql", "block", rule="NOT_SELECT", statement_type=statement)
                raise AgentError(
                    ErrorKind.GUARD_VIOLATION, detail=f"statement type {statement!r} is not allowed", rule_id="NOT_SELECT"
                )
            reported = job.total_bytes_processed
            if reported is None:  # 0 is a real answer (a query that scans nothing); None is "no answer", not "free"
                raise AgentError(ErrorKind.INTERNAL, detail="the dry run did not report how many bytes the query would scan")
            processed = int(reported)
            sp.set(bytes_processed=processed)
            self._gate(processed, budget)
            return DryRunInfo(bytes_processed=processed)

    @staticmethod
    def _require_accepted(gq: QueryLike) -> None:
        # A GuardedQuery must also be authentic: still exactly what the SQL Guard sealed. `dataclasses.replace(gq, sql=...)`
        # makes an instance too, but not one with a matching seal.
        if not isinstance(gq, ACCEPTED_QUERY_TYPES) or (isinstance(gq, GuardedQuery) and not gq.authentic):
            guard_decision("sql", "block", rule="UNGUARDED_QUERY", query_type=type(gq).__name__)
            raise AgentError(
                ErrorKind.GUARD_VIOLATION, detail=f"a {type(gq).__name__} did not come from the SQL Guard", rule_id="UNGUARDED_QUERY"
            )

    def _start_dry_run(self, gq: QueryLike, timeout: float) -> Any:
        config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False, query_parameters=_bq_params(gq))
        return self._bq.client.query(gq.sql, job_config=config, retry=None, job_retry=None, timeout=timeout)

    def _call_timeout(self, budget: BudgetLike | None) -> float:
        """The time one BigQuery call may take: the configured timeout, or what is left of the turn if that is less."""
        return call_timeout(self._config.query_timeout_s, budget)

    def _gate(self, processed: int, budget: BudgetLike | None) -> None:
        cap = self._config.max_bytes_billed
        if processed > cap:
            raise AgentError(
                ErrorKind.QUERY_TOO_EXPENSIVE, detail=f"estimate of {processed} bytes is over the cap of {cap} bytes per query"
            )
        if budget is not None and processed > budget.bytes_remaining:
            raise AgentError(
                ErrorKind.QUERY_TOO_EXPENSIVE,
                detail=f"estimate of {processed} bytes is over the {max(budget.bytes_remaining, 0)} bytes left for this turn",
            )

    # run -----------------------------------------------------------------------------------------

    def run(self, gq: QueryLike, budget: BudgetLike | None = None) -> QueryOutcome:
        """Dry run, cost gate, then the real job with a byte cap, timeout, labels and row cap."""
        info = self.dry_run(gq, budget)
        row_cap = self._config.sql_row_limit
        if isinstance(gq, SystemQuery) and gq.max_rows is not None:  # only our own fixed SQL may ask for more
            row_cap = gq.max_rows
        with span(
            "bq.query", "query", sql=gq.sql, param_names=_param_names(gq), bytes_estimated=info.bytes_processed, row_cap=row_cap
        ) as sp:
            self._check_time(budget)
            started = time.perf_counter()
            abandoned: list[int] = []  # jobs we started and then cancelled, as estimates of what they may have cost
            try:
                job, rows, df = self._attempt(
                    lambda: self._execute(gq, row_cap, self._call_timeout(budget), info.bytes_processed, abandoned),
                    "query", sp, budget, faultable=_faultable(gq),
                )
            finally:
                self._account_abandoned(abandoned, budget, sp)
            cache_hit = bool(getattr(job, "cache_hit", False))
            outcome = QueryOutcome(
                df=df,
                total_rows=int(rows.total_rows) if rows.total_rows is not None else len(df),
                bytes_processed=int(job.total_bytes_processed or 0),
                # A cache hit bills nothing, whatever the job reports.
                bytes_billed=0 if cache_hit else int(job.total_bytes_billed or 0),
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                cache_hit=cache_hit,
            )
            sp.set(
                rows=len(df), total_rows=outcome.total_rows, truncated=outcome.total_rows > len(df),
                bytes_processed=outcome.bytes_processed, bytes_billed=outcome.bytes_billed,
                cache_hit=cache_hit, elapsed_ms=outcome.elapsed_ms,
            )
            turn_metrics().add_query(outcome.bytes_billed)
            if budget is not None:
                budget.charge_bytes(outcome.bytes_billed)
            return outcome

    def _execute(
        self, gq: QueryLike, row_cap: int, timeout: float, estimate: int, abandoned: list[int]
    ) -> tuple[Any, Any, pd.DataFrame]:
        config = bigquery.QueryJobConfig(
            maximum_bytes_billed=self._config.max_bytes_billed,
            labels=job_labels(),
            query_parameters=_bq_params(gq),
            use_query_cache=True,
        )
        if hasattr(config, "job_timeout_ms"):  # present in google-cloud-bigquery 3.46.2; older versions rely on the client timeout
            config.job_timeout_ms = int((timeout + JOB_TIMEOUT_GRACE_S) * 1000)
        try:
            job = self._bq.client.query(gq.sql, job_config=config, retry=None, job_retry=None, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            # No job handle yet, so a timeout here is the HTTP call stalling, not the question being slow: it is
            # retried as an unavailable BigQuery. (A job that was created but never acknowledged ends at its
            # server-side job timeout.)
            err = classify(exc, "dry_run")
            if err is None:
                raise
            raise err from None
        try:
            rows = job.result(max_results=row_cap, timeout=timeout, retry=None, job_retry=None)
            # No Storage Read API client: it is billed separately and the capped result is small.
            return job, rows, rows.to_dataframe(create_bqstorage_client=False)
        except BaseException as exc:
            if isinstance(exc, KeyboardInterrupt) or job_may_still_run(exc):
                self._cancel(job)  # nobody is waiting for this job any more (or a retry starts another), so stop paying for it
                abandoned.append(estimate)
            raise

    @staticmethod
    def _account_abandoned(abandoned: list[int], budget: BudgetLike | None, sp: Span) -> None:
        """Count the jobs that were cancelled or lost. BigQuery may bill a cancelled job up to its full cost, and the
        library reports no bill for it, so each is charged at the dry-run estimate: an upper bound, so the free-tier
        warning and the turn budget err on the side of caution, and a loop of timeouts shows in `runs.jsonl`."""
        if not abandoned:
            return
        sp.set(abandoned_jobs=len(abandoned), abandoned_bytes_estimated=sum(abandoned))
        for estimate in abandoned:
            turn_metrics().add_query(estimate)
            if budget is not None:
                try:
                    budget.charge_bytes(estimate)
                except AgentError:
                    pass  # over budget: the next gate stops the turn, and this must not replace the error in flight

    def _cancel(self, job: Any) -> None:
        try:
            job.cancel(retry=None, timeout=CANCEL_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - best effort; the server-side job timeout is the backstop
            log.warning("could not cancel a BigQuery job: %s", crash_summary(exc))

    # retries -------------------------------------------------------------------------------------

    def _attempt(
        self, operation: Callable[[], T], phase: Phase, sp: Span | None = None, budget: BudgetLike | None = None,
        *, faultable: bool = False,
    ) -> T:
        """Run `operation`. A BigQuery failure becomes an `AgentError`; a transient one is retried with growing
        waits and jitter (brief R5). This is the only retry loop: the library's own retries are switched off.

        `faultable`: `--chaos` may replace the call with an injected fault. Only the agent's own queries are (see
        `_faultable`); startup and `doctor` reads never are, because the chat must be able to start."""
        attempt = 0
        while True:
            try:
                fault = self._chaos.maybe_fail_bq() if self._chaos is not None and faultable else None
                if fault is not None:
                    if sp is not None:
                        sp.set(chaos_fault=getattr(fault, "chaos_fault", "unknown"))
                    raise fault
                return operation()
            except Exception as exc:  # noqa: BLE001
                err = classify(exc, phase)
                if err is None:
                    raise  # not a BigQuery problem: the turn boundary reports it as INTERNAL
                if err.kind == ErrorKind.QUERY_TIMEOUT:
                    self._check_time(budget)  # the call was cut short by the turn's deadline: that is TURN_TIMEOUT
                if not err.retryable or attempt >= self._config.transient_retries:
                    raise err from None
                self._check_time(budget)  # no point waiting when the turn is already out of time
                wait = self._backoff(attempt)
                if budget is not None:
                    wait = min(wait, max(budget.remaining_seconds(), 0.0))  # never sleep past the deadline
                if sp is not None:
                    sp.event("retry", attempt=attempt + 1, error_kind=err.kind, wait_s=round(wait, 2))
                self._sleep(wait)
                self._check_time(budget)  # a retry never starts after the turn's deadline
                attempt += 1

    def _backoff(self, attempt: int) -> float:
        return min(RETRY_CAP_S, RETRY_BASE_S * 2**attempt) * (0.5 + 0.5 * self._jitter())

    @staticmethod
    def _check_time(budget: BudgetLike | None) -> None:
        if budget is not None:
            budget.check_time()  # raises TURN_TIMEOUT
