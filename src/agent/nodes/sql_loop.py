"""`sql_loop` (plan 07, decision 3.10 and 3.11, F30 to F34): write, guard, run and check the SQL of each sub-question.

One node, on purpose. The raw DataFrame that `SafeQueryRunner.run` returns is a local variable here and goes straight into
`ResultGuard.apply` (plan 06). Only a `GuardedResult`, as a plain dict, leaves this function. The model never sees a row that
has not been through the Result Guard.

Per sub-question, code decides every step and the model only writes SQL:

  write_sql -> SQL Guard -> (dry run, cost gate, run) -> Result Guard -> classify

  ok                                  keep the result.
  SQL_SYNTAX, SQL_UNKNOWN_COLUMN      repair with the failing SQL, the normalized error and the exact column list, at most
                                      `loop.sql_repairs` times (2). The last repair uses Pro when it is enabled.
  empty result                        one check-and-broaden query (`loop.broaden_attempts`), then an honest "no data". Not when
                                      groups were hidden for being small: widening would not help.
  GUARD_VIOLATION                     no retry. Status `blocked`, with the refusal text of the rule (F32).
  too expensive / too slow            status `too_expensive` / `too_slow`: the answer asks the user to narrow (F33).
  the same SQL again                  stop: REPEATED_SQL. The same error as the last attempt: stop: REPEATED_ERROR (F34).
  BUDGET_EXHAUSTED, TURN_TIMEOUT      stop the whole loop; keep what was gathered.
  BQ_UNAVAILABLE, LLM_*               this sub-question failed; go on with the next one.

Anything else (RESULT_GUARD_FAILED, NO_ACCESS, INTERNAL) is not for this loop to handle. It leaves the node, the error
boundary catches it, and the turn ends with a friendly message. A guard that fails is never worked around.

Sub-questions run one after the other: `loop.parallel_subqueries` is read, and a value above 1 is capped by the rate limiter
(S12), but this build runs them in order (see the plan's "As built").
"""
from __future__ import annotations

import dataclasses
import hashlib
import logging
import re

from pydantic import BaseModel

from src import pii
from src.agent.boundary import node
from src.agent.deps import Deps, TurnRuntime
from src.agent.nodes.policy import current_user
from src.agent.prompts.locked_rules import relevant_definitions
from src.agent.prompts.templates import format_rows, format_summary, sql_prompt
from src.agent.state import (
    BLOCKED, EMPTY, FAILED, OK, SKIPPED, TOO_EXPENSIVE, TOO_SLOW, Attempts, SubQuestion, SubResult, TurnState, error_record,
)
from src.access.users import UserContext
from src.errors import LLM_FAILURE_KINDS, TURN_LIMIT_KINDS, AgentError, ErrorKind
from src.guards.query import GuardedQuery
from src.guards.refusals import violation
from src.guards.result_guard import GuardedResult
from src.llm.routing import Step
from src.observability import event, span, turn_metrics
from src.textutil import squash

log = logging.getLogger(__name__)

SYNTAX_KINDS = frozenset({ErrorKind.SQL_SYNTAX, ErrorKind.SQL_UNKNOWN_COLUMN})
MAX_SQL_CHARS = 4000  # the SQL kept in the state, for the trace and for debugging
EARLIER_ROWS = 10  # rows of a dependency's result that a later sub-question may see

_QUOTED = re.compile(r"""'[^']*'|"[^"]*"|`[^`]*`""")
_POSITION = re.compile(r"\[\d+:\d+\]|\b(?:line|column|position) \d+\b", re.IGNORECASE)  # where in the SQL: "at [1:8]", "line 2"
_FENCE = re.compile(r"^```(?:sql)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)


class SqlOut(BaseModel):
    sql: str
    interpretation: str = ""


def error_signature(kind: ErrorKind, detail: str) -> str:
    """The error with its literals and its position removed: two failures with the same signature are the same mistake (F34). A
    name stays, digits and all: `revenue_2024` and `revenue_2025` are two columns, and a repair that moves from one to the other
    has not repeated itself."""
    return f"{kind.value}:{_POSITION.sub('[#]', _QUOTED.sub('?', detail))}"


def _sql_hash(sql: str) -> str:
    return hashlib.sha256(squash(sql).encode("utf-8")).hexdigest()[:16]


def _strip_fence(sql: str) -> str:
    stripped = sql.strip()
    match = _FENCE.match(stripped)
    return match.group(1) if match else stripped


def _result(
    status: str, *, gq: GuardedQuery | None = None, guarded: GuardedResult | None = None, interpretation: str = "",
    err: AgentError | None = None, notice: str | None = None,
) -> SubResult:
    return {
        "status": status,
        # Masked like the trace copy, and masked first: a literal in the SQL can echo something the user typed, and cutting first
        # could leave half an e-mail that no pattern matches.
        "final_sql": pii.mask(gq.sql)[:MAX_SQL_CHARS] if gq is not None and status in (OK, EMPTY) else "",
        "interpretation": interpretation,
        "guarded": dataclasses.asdict(guarded) if guarded is not None else None,
        "error_kind": err.kind.value if err is not None else None,
        "error_message": err.user_message if err is not None else None,
        "rule_id": err.rule_id if err is not None else None,
        "notice": notice,
        "scope_label": gq.scope_label if gq is not None else None,
    }


class _Runner:
    """One sub-question's attempts. Holds the user and the runtime so the methods stay short."""

    def __init__(self, deps: Deps, runtime: TurnRuntime, user: UserContext, state: TurnState) -> None:
        self.deps, self.rt, self.user, self.state = deps, runtime, user, state
        self.loop = deps.settings.loop
        self.context = state.get("context", {})
        self.plan = state.get("plan", {})

    # the model writes -------------------------------------------------------------------------------------------
    def write(self, sub: SubQuestion, mode: str, failing_sql: str, error: str, attempts: Attempts, results: dict[str, SubResult]) -> SqlOut:
        step = Step.SQL_WRITE
        if mode == "repair":  # the last repair is the one that may use the Pro model (decision 3.2)
            step = Step.LAST_REPAIR if attempts["repairs"] >= self.loop.sql_repairs else Step.SQL_REPAIR
        elif mode == "broaden":
            step = Step.SQL_REPAIR
        slice_ = self.deps.catalog.prune(sub["tables"])
        parts = sql_prompt(
            mode=mode, subquestion=sub["text"], schema_slice=slice_, hints=self.context.get("date_hints", {}),
            date_range=self.plan.get("date_range"),
            definitions=relevant_definitions(sub["text"], self.context.get("glossary_terms")),
            golden=self.context.get("trio_examples", []), earlier=self._earlier(sub, results),
            failing_sql=failing_sql or None, error=error or None, columns=slice_,
        )
        reply = self.deps.gateway.generate(
            step, system=parts.system, contents=parts.contents, schema=SqlOut, budget=self.rt.budget, prompt=parts.attrs(),
        )
        out = reply.parsed
        sql = _strip_fence(out.sql)
        if self.deps.chaos is not None:
            sql = self.deps.chaos.maybe_corrupt_sql(sql)  # `--chaos`: the guard and the repair loop must cope
        return SqlOut(sql=sql, interpretation=squash(out.interpretation, 300))

    @staticmethod
    def _earlier(sub: SubQuestion, results: dict[str, SubResult]) -> dict[str, str]:
        """The guarded results of the sub-questions this one declared a dependency on, and no others."""
        shown: dict[str, str] = {}
        for dep in sub["depends_on"]:
            guarded = (results.get(dep) or {}).get("guarded")
            if guarded and results[dep].get("status") == OK:
                limited = {**guarded, "rows": guarded.get("rows", [])[:EARLIER_ROWS]}
                shown[dep] = format_rows(limited) + "\nsummary: " + format_summary(guarded)
        return shown

    # guard, run, guard ------------------------------------------------------------------------------------------
    def guard_run(self, sql: str, attempts: Attempts) -> tuple[GuardedQuery, GuardedResult]:
        """The SQL Guard, the dry run and the query, the Result Guard. The raw rows are a local here and go no further."""
        text_hash = _sql_hash(sql)
        if text_hash in attempts["fingerprints"]:
            raise AgentError(ErrorKind.REPEATED_SQL, detail="the same query was written before")
        attempts["fingerprints"].append(text_hash)
        gq = self.deps.sql_guard.check(sql, self.user)
        if self.deps.result_guard.references_session_label(gq.sql):  # the digits of a label are no customer's ID (plan 07)
            raise violation("SESSION_LABEL", "the query uses the number of a customer label")
        if gq.fingerprint in attempts["fingerprints"]:  # the same query in another spelling
            raise AgentError(ErrorKind.REPEATED_SQL, detail="the same query was written before")
        attempts["fingerprints"].append(gq.fingerprint)
        outcome = self.deps.runner.run(gq, self.rt.budget)  # the dry run and the cost gate are part of `run`
        df, total_rows = outcome.df, outcome.total_rows
        guarded = self.deps.result_guard.apply(df, gq, total_rows)
        return gq, guarded

    # one sub-question -------------------------------------------------------------------------------------------
    def run(self, sub: SubQuestion, results: dict[str, SubResult]) -> tuple[SubResult, Attempts]:
        attempts: Attempts = {"repairs": 0, "broaden_used": 0, "broaden_ran": 0, "fingerprints": [], "last_error_signature": None}
        with span("sub_question", "step", id=sub["id"]) as sp:
            result = self._attempts(sub, results, attempts)
            sp.set(status=result["status"], repairs=attempts["repairs"], broaden_used=attempts["broaden_used"], broaden_ran=attempts["broaden_ran"])
        return result, attempts

    def _attempts(self, sub: SubQuestion, results: dict[str, SubResult], attempts: Attempts) -> SubResult:
        mode, failing_sql, error, interpretation = "write", "", "", ""
        empty_before: tuple[GuardedQuery, GuardedResult] | None = None
        original = ""  # what the first query was read as: the widened query's reading must not replace it for an empty result

        def end(status: str, err: AgentError | None = None, notice: str | None = None) -> SubResult:
            """The result when the loop stops here. A query that was only a widening of an empty one cannot turn that honest
            "no data" into a refusal or a failure: when the widening is refused, costs too much, is too slow, cannot be written or
            fixed, or meets the budget, the sub-question is empty. A guard that fails (RESULT_GUARD_FAILED) or a bug (INTERNAL) is not
            handled here: it ends the turn, and nothing is shown "from what we have" (CLAUDE.md)."""
            if empty_before is not None:
                return _result(EMPTY, gq=empty_before[0], guarded=empty_before[1], interpretation=original)
            return _result(status, interpretation=interpretation, err=err, notice=notice)

        while True:
            if not self.rt.has_room():  # the last calls are kept for the answer
                if empty_before is not None:
                    return end(EMPTY)
                raise AgentError(ErrorKind.BUDGET_EXHAUSTED, detail="the model calls that are left are kept for the answer")
            try:
                written = self.write(sub, mode, failing_sql, error, attempts, results)
            except AgentError as exc:
                if exc.kind in LLM_FAILURE_KINDS or (exc.kind in TURN_LIMIT_KINDS and empty_before is not None):
                    return end(FAILED, exc)
                raise
            interpretation = written.interpretation or interpretation
            sql = written.sql

            try:
                gq, guarded = self.guard_run(sql, attempts)
            except AgentError as err:
                kind = err.kind
                if kind in TURN_LIMIT_KINDS:
                    if empty_before is not None:  # the next sub-question meets the same limit on its own
                        return end(FAILED, err)
                    raise
                if kind is ErrorKind.GUARD_VIOLATION:
                    return end(BLOCKED, err, err.user_message)
                if kind is ErrorKind.QUERY_TOO_EXPENSIVE:
                    return end(TOO_EXPENSIVE, err)
                if kind is ErrorKind.QUERY_TIMEOUT:
                    return end(TOO_SLOW, err)
                if kind is ErrorKind.BQ_UNAVAILABLE:
                    return end(FAILED, err)
                if kind is ErrorKind.REPEATED_SQL:
                    event("loop.stop", reason=kind.value)  # with an empty result before it, nothing is left to widen
                    return end(FAILED, err)
                if kind in SYNTAX_KINDS:
                    signature = error_signature(kind, err.detail)
                    if attempts["last_error_signature"] == signature:
                        event("loop.stop", reason=ErrorKind.REPEATED_ERROR.value)
                        return end(FAILED, AgentError(ErrorKind.REPEATED_ERROR, detail=f"the same error twice: {kind.value}"))
                    attempts["last_error_signature"] = signature
                    if attempts["repairs"] >= self.loop.sql_repairs:
                        return end(FAILED, err)
                    attempts["repairs"] += 1
                    turn_metrics().add_repair()
                    mode, failing_sql, error = "repair", sql, err.detail
                    continue
                raise  # RESULT_GUARD_FAILED, NO_ACCESS, INTERNAL: not for this loop to work around

            attempts["last_error_signature"] = None
            if empty_before is not None:  # this query is the widening (or its repair), and it has run: the note may say so
                attempts["broaden_ran"] += 1
            if guarded.status == "withheld":  # the Result Guard could not check the group sizes
                withheld = AgentError(ErrorKind.GUARD_VIOLATION, detail="the result was withheld", rule_id="GROUP_SIZE_UNKNOWN")
                if empty_before is not None:
                    return end(BLOCKED, withheld)
                return _result(BLOCKED, gq=gq, guarded=guarded, interpretation=interpretation, err=withheld,
                               notice=guarded.notices[0] if guarded.notices else None)
            if guarded.status == "empty":
                if guarded.suppressed_groups == 0 and attempts["broaden_used"] < self.loop.broaden_attempts:
                    attempts["broaden_used"] += 1
                    empty_before, original = (gq, guarded), interpretation
                    event("loop.broaden", sub_question=sub["id"])
                    mode, failing_sql, error = "broaden", sql, ""
                    continue
                return _result(EMPTY, gq=gq, guarded=guarded, interpretation=interpretation)
            return _result(OK, gq=gq, guarded=guarded, interpretation=interpretation)


def make_sql_loop(deps: Deps):
    if deps.settings.loop.parallel_subqueries > 1:
        log.warning(
            "loop.parallel_subqueries is %d, but this build runs sub-questions one after the other (S12)",
            deps.settings.loop.parallel_subqueries,
        )

    @node(deps, "sql_loop")
    def sql_loop(state: TurnState) -> dict:
        runtime = deps.runtime(state)
        runner = _Runner(deps, runtime, current_user(deps, state), state)
        results: dict[str, SubResult] = dict(state.get("results", {}))
        attempts: dict[str, Attempts] = dict(state.get("attempts", {}))
        errors = list(state.get("errors", []))
        todo = [sub for sub in state.get("plan", {}).get("subquestions", []) if sub["id"] not in results]
        stopped = False
        for position, sub in enumerate(todo, 1):
            if stopped:
                results[sub["id"]] = _result(SKIPPED)
                continue
            runtime.say(f"Querying ({position} of {len(todo)})..." if len(todo) > 1 else "Querying the data...")
            try:
                results[sub["id"]], attempts[sub["id"]] = runner.run(sub, results)
            except AgentError as exc:
                if exc.kind not in TURN_LIMIT_KINDS:
                    raise
                stopped = True  # the budget or the time is gone: keep what was gathered
                results[sub["id"]] = _result(SKIPPED, err=exc)
                errors.append(error_record(exc))
        return {"results": results, "attempts": attempts, "errors": errors}

    return sql_loop

