"""`/eval`: the judge on the turns of the chat that is running (plan 09).

Nothing here runs until someone types `/eval`. The chat keeps the turns it has answered in memory (`LiveTurn`); `/eval` judges the ones not judged yet, one
turn at a time, with the six rubrics of `evalkit/rubrics.xlsx` over the model gateway, and shows the scores.

- **Advisory.** A score is a finding to read. It changes no metrics row, no exit code and no guard.
- **Its own gateway.** The judge is a second `LLMGateway` with a cost hook, so every attempt it makes, a failed one too, is counted. The chat's model calls are
  not touched, and chaos is not injected into the judge.
- **Tokens are logged.** Every judge call is a `model call step=judge ...` line in `app.log` (the gateway writes it), each judged turn adds a row to
  `evaluations.jsonl` (counts, scores and the models; no text), and the same numbers go to the daily token ledger as source `eval`.
- **Stops cleanly.** Ctrl+C, or the daily budget running out, ends the evaluation after the verdict in flight. The turns judged so far are kept and shown.
- **Not independent when it grades itself.** If the model that answered is among the models the judge can use, the result says so on screen and in the row
  (`independent: false`), or `/eval` refuses when `evaluation.allow_self_judge` is false (decision 3.13).
- **Reads no personal data and no result row.** See `evidence.py`.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Callable, Collection, Iterator, Sequence

from rich.console import Console
from rich.table import Table
from rich.text import Text

from evalkit.config import EvaluationConfig
from evalkit.judge import JudgeClient
from evalkit.rubrics import Rubric, load_rubrics
from evalkit.runner import EvaluationRunner
from evalkit.schema import Verdict
from src.access.users import UserContext
from src.config import Settings
from src.errors import AgentError, crash_summary
from src.evaluation.evidence import LIVE_PIPELINE, LiveTurn, chat_evidence
from src.evaluation.judging import (
    APPLICABILITY, JUDGE_LAYERS, RUBRICS_FILE, GatewayJudge, is_self_judged, sample_block, session_from_evidence,
)
from src.llm.gateway import CostHook, Gateway
from src.llm.routing import Step
from src.observability.metrics import TurnMetrics, write_run_row
from src.observability.tracing import clean
from src.observability.viewer import load_trace
from src.storage.db import iso, now_utc
from src.storage.usage import DailyBudget, UsageLedger

log = logging.getLogger(__name__)

GatewayFactory = Callable[[CostHook], Gateway]
ProgressFn = Callable[[str], None]

REASON_CHARS = 90
STOPPED_TEXT = {
    "interrupted": "stopped with Ctrl+C",
    "DAILY_BUDGET_USED": "the daily token budget is used up (type /budget to see it)",
}


class EvalRefused(Exception):
    """`/eval` will not start. The message is ours and is shown to the user as it is."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class RubricResult:
    key: str
    verdict: str  # a `Verdict` value: PASS, FAIL, N/A, NOT_EXERCISED
    score: float | None  # 0 to 1
    judge_score: int | None  # the judge's own 1 to 4
    threshold: float
    reason: str


@dataclass
class TurnVerdict:
    turn: LiveTurn
    results: list[RubricResult] = field(default_factory=list)
    llm_calls: int = 0  # model calls the judge made for this turn, retries included
    tokens_in: int = 0
    tokens_out: int = 0
    complete: bool = True  # False when Ctrl+C, the budget or a failed call left a rubric of this turn with no verdict: `/eval` will judge it again

    @property
    def scored(self) -> int:
        return sum(1 for result in self.results if result.verdict in (Verdict.PASS.value, Verdict.FAIL.value))


@dataclass
class EvalOutcome:
    verdicts: list[TurnVerdict] = field(default_factory=list)
    judge_models: tuple[str, ...] = ()
    answered_by: tuple[str, ...] = ()
    self_judged: bool = False
    stopped: str = ""  # a key of STOPPED_TEXT; empty when every turn was judged
    judge_verdicts: int = 0
    failed_calls: int = 0
    failure_kinds: list[str] = field(default_factory=list)
    llm_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float | None = None
    turns_left: int = 0  # turns that were due and not judged because the run stopped


def chat_rubrics(path: Any = RUBRICS_FILE) -> dict[str, Rubric]:
    """The rubrics of the offline suites, applied to a real conversation too: the pipeline `chat` is added to each one's list. One definition of each rubric."""
    return {
        key: replace(rubric, pipelines=tuple(dict.fromkeys((*rubric.pipelines, LIVE_PIPELINE)))) for key, rubric in load_rubrics(path).items()
    }


def default_gateway(settings: Settings) -> GatewayFactory:
    def make(hook: CostHook) -> Gateway:
        from src.llm.gateway import LLMGateway  # the SDK loads only when `/eval` is used

        return LLMGateway(settings, cost_hook=hook)

    return make


@contextmanager
def _quiet_evalkit() -> Iterator[None]:
    """`evalkit` logs a failed judge call with a traceback on its own logger, which is outside the app's masked log: in the chat that would land on the screen.
    The failure is counted and named by `GatewayJudge`, and the summary line goes to `app.log`."""
    logger = logging.getLogger("evalkit")
    handler, propagate = logging.NullHandler(), logger.propagate
    logger.addHandler(handler)
    logger.propagate = False
    try:
        yield
    finally:
        logger.removeHandler(handler)
        logger.propagate = propagate


def evaluate_turns(
    settings: Settings,
    *,
    user: UserContext,
    session_id: str,
    turns: Sequence[LiveTurn],
    due: Collection[str],
    gateway_factory: GatewayFactory | None = None,
    ledger: UsageLedger | None = None,
    budget: DailyBudget | None = None,
    progress: ProgressFn = lambda text: None,
    rubrics: dict[str, Rubric] | None = None,
    now: Callable[[], datetime] = now_utc,
) -> EvalOutcome:
    """Judge the turns of `turns` whose trace ID is in `due` (the others are only context for the judge, as earlier turns of the conversation).

    Raises `EvalRefused` when the judge would be the model that answered and `evaluation.allow_self_judge` is false. Never raises for a judge call that
    fails: that verdict is "no verdict", counted in `failed_calls`, never a pass."""
    spend = TurnMetrics()  # an accumulator for the judge's own use: the gateway's cost hook adds every attempt to it
    gateway = (gateway_factory or default_gateway(settings))(lambda model, tokens_in, tokens_out: spend.add_llm_call(model, tokens_in=tokens_in, tokens_out=tokens_out))
    judge_models = tuple(gateway.router.chain(Step.JUDGE))  # type: ignore[attr-defined]
    todo = [turn for turn in turns if turn.trace_id in due]
    answered_by = tuple(dict.fromkeys(model for turn in todo for model in turn.models))
    self_judged = is_self_judged(", ".join(answered_by), judge_models)
    if self_judged and not settings.evaluation.allow_self_judge:
        raise EvalRefused(
            f"The judge would be {', '.join(judge_models)}, the model that answered ({', '.join(answered_by)}), and a judge must not grade its own model. "
            "Set evaluation.allow_self_judge: true in config.yaml to judge anyway; the result is then marked as not independent."
        )

    outcome = EvalOutcome(judge_models=judge_models, answered_by=answered_by, self_judged=self_judged)
    rubrics = rubrics if rubrics is not None else chat_rubrics()
    evidence = chat_evidence(
        session_id, user_id=user.user_id, scope_mode=user.scope_mode, brands=user.brands, turns=turns,
        read_spans=lambda trace_id: load_trace(settings.traces_path, trace_id) or [],
    )
    session = session_from_evidence(evidence)
    gate = (lambda: budget.check(user.user_hash)) if budget is not None else None
    gateway_judge = GatewayJudge(gateway, settings, before_call=gate)
    runner = EvaluationRunner(
        config=EvaluationConfig(
            layers=JUDGE_LAYERS, mode="live", judge_provider=settings.llm_backend.provider, judge_model=", ".join(judge_models), judge_repeats=1,
            judge_temperature=settings.llm.steps[Step.JUDGE.value].temperature, gate_judge_on_static=False, bootstrap_resamples=0,
        ),
        judge=JudgeClient(repeats=1, judge_factory=gateway_judge), rubrics=rubrics, applicability=APPLICABILITY, sample_formatter=sample_block,
    )
    with _quiet_evalkit():
        for live, eval_turn in zip(turns, session.turns):
            if live.trace_id not in due:
                continue
            if gate is not None:  # a turn that cannot even start is left for later, and does not show up as a table of "no verdict"
                try:
                    gate()
                except AgentError as exc:
                    gateway_judge.stop(exc.kind.value)
                    break
            progress(f"Judging turn {live.index} ({len(outcome.verdicts) + 1} of {len(todo)})...")
            before = (spend.llm_calls, spend.tokens_in, spend.tokens_out, gateway_judge.verdicts, len(gateway_judge.failures))
            sample = None
            try:
                sample = runner.score_session(session, None, turn=eval_turn)
            except KeyboardInterrupt:
                gateway_judge.stop("interrupted")
            finally:
                spent = TurnVerdict(live, llm_calls=spend.llm_calls - before[0], tokens_in=spend.tokens_in - before[1], tokens_out=spend.tokens_out - before[2])
                if ledger is not None:
                    ledger.record(user.user_hash, "eval", tokens_in=spent.tokens_in, tokens_out=spent.tokens_out, calls=spent.llm_calls, trace_id=live.trace_id)
            spent.complete = not gateway_judge.stopped and len(gateway_judge.failures) == before[4]  # a call that failed leaves a rubric with no verdict: try the turn again
            if sample is not None:
                spent.results = [
                    RubricResult(
                        key, metric.verdict.value, metric.score, metric.details.get("judge_score_1_4"), rubrics[key].threshold if key in rubrics else 0.0, metric.reason,
                    )
                    for key, metric in sample.metrics.items()
                ]
                outcome.verdicts.append(spent)
                try:
                    write_run_row(settings.evaluations_path, evaluation_row(
                        spent, user, outcome, judge_verdicts=gateway_judge.verdicts - before[3], failed_calls=len(gateway_judge.failures) - before[4], ts=iso(now()),
                    ))
                except Exception as exc:  # noqa: BLE001 - a file of counts that cannot be written must not lose the verdicts the user is waiting for
                    log.error("could not write the evaluation row of trace_id=%s: %s", live.trace_id, crash_summary(exc))
            if gateway_judge.stopped:
                break

    outcome.stopped = gateway_judge.stopped
    outcome.turns_left = len(todo) - sum(1 for item in outcome.verdicts if item.complete)
    outcome.judge_verdicts, outcome.failed_calls, outcome.failure_kinds = gateway_judge.verdicts, len(gateway_judge.failures), sorted(set(gateway_judge.failures))
    outcome.llm_calls, outcome.tokens_in, outcome.tokens_out = spend.llm_calls, spend.tokens_in, spend.tokens_out
    pricing = settings.llm.pricing_usd_per_1m or {}
    outcome.cost_usd = spend.estimated_cost({model: (price.in_, price.out) for model, price in pricing.items()})
    log.info(
        "eval done turns=%d judged=%d judge_models=%s independent=%s llm_calls=%d tokens_in=%d tokens_out=%d failed_calls=%d stopped=%s",
        len(todo), len(outcome.verdicts), ",".join(judge_models), not self_judged, outcome.llm_calls, outcome.tokens_in, outcome.tokens_out,
        outcome.failed_calls, outcome.stopped or "no",
    )
    return outcome


def evaluation_row(
    verdict: TurnVerdict, user: UserContext, outcome: EvalOutcome, *, judge_verdicts: int, failed_calls: int, ts: str,
) -> dict[str, Any]:
    """One PII-free row of `evaluations.jsonl`: ids, the models, the scores and the tokens. No question, no answer, no reason (the judge's reason is text about the
    answer, and a file of counts should stay a file of counts)."""
    turn = verdict.turn
    return clean({
        "ts": ts, "trace_id": turn.trace_id, "user_hash": user.user_hash, "conversation_id": turn.conversation_id, "turn_id": turn.turn_id,
        "judge_models": list(outcome.judge_models), "answered_by": list(outcome.answered_by), "independent": not outcome.self_judged,
        "complete": verdict.complete,
        "rubrics": {result.key: {"verdict": result.verdict, "score": result.score, "judge_score": result.judge_score} for result in verdict.results},
        "judge_verdicts": judge_verdicts, "failed_calls": failed_calls, "llm_calls": verdict.llm_calls,
        "tokens_in": verdict.tokens_in, "tokens_out": verdict.tokens_out,
    })


# on screen ------------------------------------------------------------------------------------------------------------------------------------

_STYLE = {Verdict.PASS.value: "green", Verdict.FAIL.value: "bold red"}


def _short(reason: str) -> str:
    text = " ".join((reason or "").split())
    return text if len(text) <= REASON_CHARS else text[: REASON_CHARS - 1].rstrip() + "…"


def _score(result: RubricResult) -> str:
    return f"{result.judge_score:g}/4" if result.judge_score is not None else "-"


def render(console: Console, outcome: EvalOutcome) -> None:
    if outcome.verdicts:
        table = Table(title="Evaluation of this chat (advisory)")
        for column in ("Turn", "Rubric", "Score", "Result", "Why"):
            table.add_column(column, justify="right" if column == "Score" else "left")
        for item in outcome.verdicts:
            for result in item.results:
                table.add_row(
                    f"{item.turn.index} ({item.turn.turn_id})", result.key, _score(result),
                    Text(result.verdict, style=_STYLE.get(result.verdict, "dim")),
                    Text(_short(result.reason), style="dim" if result.verdict not in _STYLE else ""),
                )
            table.add_section()
        console.print(table)
    below = [(item.turn.index, result) for item in outcome.verdicts for result in item.results if result.verdict == Verdict.FAIL.value]
    for index, result in below:
        console.print(Text(f"below the bar: turn {index}, {result.key} {_score(result)} (the bar is {result.threshold:.0%} of the scale)", style="yellow"))
    models = outcome.judge_models[0] + (f" (then {', '.join(outcome.judge_models[1:])} if it fails)" if len(outcome.judge_models) > 1 else "") if outcome.judge_models else "no model"
    if outcome.self_judged:
        console.print(Text(
            f"NOT INDEPENDENT: the judge ({models}) is the model that answered. Read these scores as a smoke test, not as a grade.", style="bold red"
        ))
    else:
        console.print(Text(f"Judge: {models}, not the model that answered.", style="dim"))
    if outcome.failed_calls:
        console.print(Text(
            f"{outcome.failed_calls} judge call(s) failed ({', '.join(outcome.failure_kinds)}); those samples have no verdict, they are not passes.", style="bold red"
        ))
    spent = f"Judge use: {outcome.llm_calls:,} model call(s), {outcome.tokens_in:,} tokens in, {outcome.tokens_out:,} tokens out"
    if outcome.cost_usd is not None:
        spent += f", about ${outcome.cost_usd:.4f}"
    console.print(Text(spent + ".", style="dim"))
    if outcome.stopped:
        why = STOPPED_TEXT.get(outcome.stopped, outcome.stopped)
        done = sum(1 for item in outcome.verdicts if item.complete)
        console.print(Text(f"Evaluation {why}. {done} turn(s) judged" + (f", {outcome.turns_left} left: type /eval to carry on." if outcome.turns_left else "."), style="yellow"))
