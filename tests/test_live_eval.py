"""`/eval` in the chat (plan 09): the judge starts only when it is asked for, it logs what it spends, it stops cleanly, and it is never a pass when it fails.

The chat's turn is a stand-in (it spends tokens and leaves the spans of an answered data question). The judge is the real `LLMGateway` over a scripted SDK client,
so the retries, the cost hook and the token counting are the real code, and no test touches a network.
"""
import json
import re
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from src.cli import main as cli_main
from src.cli.main import app
from src.errors import AgentError, ErrorKind
from src.llm.fake import FakeClock, FakeGenaiClient, unavailable
from src.llm.fake import FakeGateway
from src.llm.gateway import LLMGateway
from src.llm.routing import Step
from src.observability import span, turn_metrics

runner = CliRunner(env={"COLUMNS": "220"})
LITE, FLASH = "gemini-3.5-flash-lite", "gemini-3.8-flash"
PASS_VERDICT = json.dumps({"score": 4, "reason": "scripted: this is good work"})
LOW_VERDICT = json.dumps({"score": 2, "reason": "scripted: this misses the point"})
RUBRICS = ("tool_use_quality", "final_response_quality", "groundedness", "safety", "answer_completeness")  # hitl_appropriateness does not apply to a read-only turn


def chat(config_path: Path, user: str, text: str, *extra: str):
    return runner.invoke(app, ["chat", "--user", user, "--config", str(config_path), *extra], input=text)


def answering(model: str = "fake-model", answer: str = "Revenue was 1,000.00 last month.", tokens: tuple[int, int] = (800, 100)):
    """A stand-in turn that looks like an answered data question: the model that answered, its tokens, the SQL loop and one query that returned rows."""

    def turn(session, text, *, trace_id, progress):
        turn_metrics().add_llm_call(model, tokens_in=tokens[0], tokens_out=tokens[1])
        turn_metrics().set_intent("data_question", "simple")
        with span("sql_loop", "step"):
            with span("bq.query", "query", sql="SELECT SUM(sale_price) AS revenue FROM order_items", rows=1):
                pass
        return answer

    return turn


class Judge:
    """The scripted SDK client behind the judge's gateway, and the patch that hands the chat's `/eval` a gateway made on it."""

    def __init__(self, monkeypatch, *, tokens=(200, 20)):
        self.client = FakeGenaiClient([LITE, FLASH])
        self.tokens = tokens
        self.hooks: list = []
        monkeypatch.setattr("src.evaluation.live.default_gateway", self.factory)

    def factory(self, settings):
        def make(hook):
            clock = FakeClock()
            return LLMGateway(settings, client=self.client, cost_hook=hook, clock=clock, sleep=clock.sleep)

        return make

    def always(self, verdict: str = PASS_VERDICT) -> "Judge":
        self.client.reply(verdict, times=None, tokens_in=self.tokens[0], tokens_out=self.tokens[1])
        return self

    @property
    def calls(self) -> int:
        return len(self.client.requests)


def evaluation_rows(project: Path) -> list[dict]:
    path = project / "data" / "evaluations.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def usage_rows(project: Path, source: str) -> list[sqlite3.Row]:
    conn = sqlite3.connect(project / "data" / "db.sqlite3")
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM token_usage WHERE source = ? ORDER BY seq", (source,)).fetchall()
    finally:
        conn.close()


# nothing runs by itself -------------------------------------------------------------------------------------------------------------------------

def test_the_chat_makes_no_judge_call_until_eval_is_typed(project, config_path, monkeypatch):
    judge = Judge(monkeypatch).always()
    monkeypatch.setattr(cli_main, "run_turn", answering())
    result = chat(config_path, "alice", "one\ntwo\nthree\n/quit\n")
    assert result.exit_code == 0 and result.output.count("Revenue was 1,000.00") == 3
    assert judge.calls == 0
    assert not (project / "data" / "evaluations.jsonl").exists() and usage_rows(project, "eval") == []


def test_the_real_graph_never_calls_the_judge_step(scripted_gateway, config_path):
    """The whole agent on a scripted gateway: a data question uses the Input Guard, the SQL writer and the Composer, and never `Step.JUDGE`."""
    from tests.agent_support import composer_reply, guard_reply, sql_reply

    scripted_gateway.queue(Step.INPUT_GUARD, guard_reply()).queue(Step.SQL_WRITE, sql_reply()).queue(Step.COMPOSER, composer_reply("Brand A led with 1,000."))
    result = chat(config_path, "alice", "which brand led?\n/quit\n")
    assert result.exit_code == 0
    assert scripted_gateway.calls_for(Step.JUDGE) == []


def test_eval_is_available_by_default_and_the_help_lists_it_and_budget(config_path):
    result = chat(config_path, "alice", "/help\n/quit\n")
    assert "/eval [last|all]" in result.output and "/budget [clear]" in result.output


# judging, and the tokens it spends --------------------------------------------------------------------------------------------------------------

def test_eval_judges_the_answered_turn_and_logs_the_tokens_it_spent(project, config_path, monkeypatch):
    judge = Judge(monkeypatch, tokens=(200, 20)).always()
    monkeypatch.setattr(cli_main, "run_turn", answering())
    result = chat(config_path, "alice", "What was revenue last month?\n/eval\n/budget\n/quit\n")
    assert result.exit_code == 0, result.output

    for key in RUBRICS:
        assert key in result.output
    assert "Evaluation of this chat (advisory)" in result.output
    assert judge.calls == 5  # one verdict for each rubric that can apply
    assert "Judge use: 5 model call(s), 1,000 tokens in, 100 tokens out." in result.output
    assert "Judge: " in result.output and "NOT INDEPENDENT" not in result.output  # fake-model answered, the judge is Gemini

    (row,) = evaluation_rows(project)
    assert (row["llm_calls"], row["tokens_in"], row["tokens_out"], row["judge_verdicts"], row["failed_calls"]) == (5, 1000, 100, 5, 0)
    assert row["independent"] is True and row["complete"] is True and row["answered_by"] == ["fake-model"] and row["judge_models"][0] == FLASH
    assert set(row["rubrics"]) >= set(RUBRICS) and all(item["verdict"] == "PASS" for key, item in row["rubrics"].items() if key in RUBRICS)
    assert re.fullmatch(r"[0-9a-f]{8}", row["trace_id"])

    (spent,) = usage_rows(project, "eval")
    assert (spent["calls"], spent["tokens_in"], spent["tokens_out"], spent["trace_id"]) == (5, 1000, 100, row["trace_id"])
    assert "evaluation  1 turn(s), 5 model call(s), 1,000 tokens in, 100 tokens out" in result.output  # /budget counts it
    log = (project / "data" / "logs" / "app.log").read_text(encoding="utf-8")
    assert len(re.findall(r"model call step=judge model=\S+ attempt=1 tokens_in=200 tokens_out=20 ", log)) == 5
    assert re.search(r"eval done turns=1 judged=1 .* llm_calls=5 tokens_in=1000 tokens_out=100 failed_calls=0 stopped=no", log)


def test_the_row_of_an_evaluation_holds_no_text(project, config_path, monkeypatch):
    Judge(monkeypatch).always()
    monkeypatch.setattr(cli_main, "run_turn", answering(answer="Revenue was 1,000.00 last month.", model="fake-model"))
    chat(config_path, "alice", "What was revenue last month?\n/eval\n/quit\n")
    text = (project / "data" / "evaluations.jsonl").read_text(encoding="utf-8")
    assert "Revenue was" not in text and "What was revenue" not in text and "scripted:" not in text  # no question, answer or reason


def test_a_score_below_the_bar_is_named_and_a_judge_never_changes_the_metrics_row(project, config_path, monkeypatch):
    Judge(monkeypatch).always(LOW_VERDICT)
    monkeypatch.setattr(cli_main, "run_turn", answering())
    result = chat(config_path, "alice", "a question\n/eval\n/quit\n")
    assert "below the bar: turn 1, groundedness 2/4" in result.output
    rows = [json.loads(line) for line in (project / "data" / "runs.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1 and rows[0]["outcome"] == "answered" and rows[0]["llm_calls"] == 1  # the judge's calls are not in the chat's row


def test_the_judge_that_is_the_model_that_answered_is_marked_not_independent(project, config_path, monkeypatch):
    Judge(monkeypatch).always()
    monkeypatch.setattr(cli_main, "run_turn", answering(model=FLASH))
    result = chat(config_path, "alice", "a question\n/eval\n/quit\n")
    assert "NOT INDEPENDENT" in result.output
    assert evaluation_rows(project)[0]["independent"] is False


def test_eval_refuses_to_grade_its_own_model_when_the_config_says_so(project, monkeypatch):
    judge = Judge(monkeypatch).always()
    (project / "config.yaml").write_text("evaluation:\n  allow_self_judge: false\n", encoding="utf-8")
    monkeypatch.setattr(cli_main, "run_turn", answering(model=FLASH))
    result = chat(project / "config.yaml", "alice", "a question\n/eval\n/quit\n")
    assert "must not grade its own model" in result.output and "allow_self_judge: true" in result.output
    assert judge.calls == 0 and evaluation_rows(project) == []


def test_a_model_that_is_not_the_judge_is_allowed_even_when_self_judging_is_off(project, monkeypatch):
    Judge(monkeypatch).always()
    (project / "config.yaml").write_text("evaluation:\n  allow_self_judge: false\n", encoding="utf-8")
    monkeypatch.setattr(cli_main, "run_turn", answering(model="some-other-model"))
    assert "Evaluation of this chat" in chat(project / "config.yaml", "alice", "a question\n/eval\n/quit\n").output


# which turns --------------------------------------------------------------------------------------------------------------------------------------

def test_eval_judges_only_the_turns_not_judged_yet_and_last_and_all_judge_again(project, config_path, monkeypatch):
    judge = Judge(monkeypatch).always()
    monkeypatch.setattr(cli_main, "run_turn", answering())
    result = chat(config_path, "alice", "one\n/eval\ntwo\n/eval\n/eval\n/eval last\n/eval all\n/quit\n")
    assert result.exit_code == 0, result.output
    assert judge.calls == 5 + 5 + 0 + 5 + 10  # turn 1; turn 2; nothing left; the newest again; both again
    assert "Every answer of this chat has been judged" in result.output
    assert [row["turn_id"] for row in evaluation_rows(project)] == ["t-1", "t-2", "t-2", "t-1", "t-2"]


def test_a_new_conversation_starts_with_nothing_to_judge(config_path, monkeypatch):
    judge = Judge(monkeypatch).always()
    monkeypatch.setattr(cli_main, "run_turn", answering())
    result = chat(config_path, "alice", "one\n/new\n/eval\n/quit\n")
    assert "Nothing to evaluate yet" in result.output and judge.calls == 0


def test_eval_with_nothing_asked_yet_says_so(config_path, monkeypatch):
    judge = Judge(monkeypatch).always()
    result = chat(config_path, "alice", "/eval\n/quit\n")
    assert "Nothing to evaluate yet. Ask a question first" in result.output and judge.calls == 0


def test_a_word_eval_does_not_know_prints_the_usage(config_path, monkeypatch):
    Judge(monkeypatch).always()
    monkeypatch.setattr(cli_main, "run_turn", answering())
    assert "Usage: /eval [last|all]" in chat(config_path, "alice", "one\n/eval everything\n/quit\n").output


def test_evaluation_can_be_switched_off_in_the_config(project, monkeypatch):
    judge = Judge(monkeypatch).always()
    (project / "config.yaml").write_text("evaluation:\n  enabled: false\n", encoding="utf-8")
    monkeypatch.setattr(cli_main, "run_turn", answering())
    result = chat(project / "config.yaml", "alice", "one\n/eval\n/quit\n")
    assert "Evaluation is switched off" in result.output and judge.calls == 0


def test_the_earlier_turns_of_the_chat_are_context_for_the_judge(config_path, monkeypatch):
    judge = Judge(monkeypatch).always()
    monkeypatch.setattr(cli_main, "run_turn", answering(answer="Brand A had the most revenue."))
    chat(config_path, "alice", "which brand had the most revenue?\nand the month before?\n/eval last\n/quit\n")
    prompts = [str(request.contents) for request in judge.client.requests]
    assert prompts and all("EARLIER TURNS IN THIS CONVERSATION" in prompt and "which brand had the most revenue?" in prompt for prompt in prompts)
    assert all("REQUEST: and the month before?" in prompt for prompt in prompts)


# stopping, failing, and the budget ----------------------------------------------------------------------------------------------------------------

def test_ctrl_c_stops_the_judge_not_the_chat_and_the_turn_is_judged_again_later(project, config_path, monkeypatch):
    judge = Judge(monkeypatch)
    judge.client.reply(PASS_VERDICT, times=1, tokens_in=200, tokens_out=20)
    judge.client.fail(KeyboardInterrupt(), times=1)
    judge.always()
    monkeypatch.setattr(cli_main, "run_turn", answering())
    result = chat(config_path, "alice", "one\n/eval\n/eval\n/quit\n")
    assert result.exit_code == 0 and "Goodbye" in result.output  # the chat went on after Ctrl+C
    assert "Evaluation stopped with Ctrl+C. 0 turn(s) judged, 1 left: type /eval to carry on." in result.output
    first, second = evaluation_rows(project)
    assert first["complete"] is False and first["trace_id"] == second["trace_id"]  # the interrupted turn was not marked as judged ...
    assert second["complete"] is True and all(item["verdict"] == "PASS" for key, item in second["rubrics"].items() if key in RUBRICS)  # ... so the next /eval did it
    assert first["failed_calls"] >= 1 and second["failed_calls"] == 0
    assert result.output.count("Evaluation of this chat (advisory)") == 2


def test_ctrl_c_leaves_the_tokens_it_spent_in_the_ledger(project, config_path, monkeypatch):
    judge = Judge(monkeypatch, tokens=(200, 20))
    judge.client.reply(PASS_VERDICT, times=1, tokens_in=200, tokens_out=20)
    judge.client.fail(KeyboardInterrupt(), times=1)
    monkeypatch.setattr(cli_main, "run_turn", answering())
    chat(config_path, "alice", "one\n/eval\n/quit\n")
    (spent,) = usage_rows(project, "eval")
    # The answered call is counted. The interrupted one never came back, so there is nothing to count: what the provider charged for it is not known.
    assert (spent["calls"], spent["tokens_in"], spent["tokens_out"]) == (1, 200, 20)


def test_a_failing_judge_is_no_verdict_and_never_a_pass_and_the_turn_is_tried_again(project, config_path, monkeypatch):
    judge = Judge(monkeypatch)
    judge.client.fail(unavailable(), times=None)
    monkeypatch.setattr(cli_main, "run_turn", answering())
    result = chat(config_path, "alice", "one\n/eval\n/eval\n/quit\n")
    assert "judge call(s) failed" in result.output and "LLM_UNAVAILABLE" in result.output
    assert "PASS" not in result.output.split("Evaluation of this chat")[1].split("judge call")[0].replace("PASS_BY_PROXY", "")
    rows = evaluation_rows(project)
    assert len(rows) == 2 and all(row["complete"] is False and row["failed_calls"] > 0 for row in rows)  # the second /eval tried it again
    assert all(item["verdict"] != "PASS" for row in rows for item in row["rubrics"].values())


def test_the_daily_cap_stops_the_judge_before_it_starts_a_turn(project, monkeypatch):
    judge = Judge(monkeypatch).always()
    (project / "config.yaml").write_text("budget:\n  daily_tokens_per_user: 500\n", encoding="utf-8")
    monkeypatch.setattr(cli_main, "run_turn", answering(tokens=(800, 100)))
    result = chat(project / "config.yaml", "alice", "one\n/eval\n/quit\n")
    assert "the daily token budget is used up" in result.output and "0 turn(s) judged, 1 left" in result.output
    assert judge.calls == 0 and evaluation_rows(project) == []


def test_the_judge_spends_the_same_daily_budget_and_clear_gives_it_back(project, monkeypatch):
    Judge(monkeypatch, tokens=(300, 0)).always()
    config = project / "config.yaml"
    config.write_text("budget:\n  daily_tokens_per_user: 2000\n", encoding="utf-8")
    monkeypatch.setattr(cli_main, "run_turn", answering(tokens=(100, 0)))  # a chat turn: 100 tokens
    result = chat(config, "alice", "one\ntwo\n/eval\n/budget\nthree\n/quit\n")
    # The judge spends 5 x 300 = 1,500 on each turn. The check before the second turn sees 200 + 1,500 < 2,000 and lets it start (the cap is soft).
    assert "total       3,200 of 2,000 tokens (160%); the cap is used up" in result.output
    assert result.output.count("usage budget for your account is used up") == 1  # turn three was refused
    cleared = chat(config, "alice", "/budget clear\nfour\n/quit\n")
    assert "Revenue was 1,000.00 last month." in cleared.output and "usage budget for your account" not in cleared.output


def test_an_answer_with_personal_data_is_withheld_from_the_judge(project, config_path, monkeypatch):
    judge = Judge(monkeypatch).always()
    monkeypatch.setattr(cli_main, "run_turn", answering(answer="Write to jane.doe@example.com about it."))
    result = chat(config_path, "alice", "one\n/eval\n/quit\n")
    assert judge.calls == 0
    assert "withheld" in result.output
    assert evaluation_rows(project) == [] or "jane.doe@example.com" not in (project / "data" / "evaluations.jsonl").read_text(encoding="utf-8")


def test_a_crash_inside_the_evaluation_does_not_close_the_chat(project, config_path, monkeypatch):
    def broken(settings):
        raise RuntimeError("boom with a secret value")

    monkeypatch.setattr("src.evaluation.live.default_gateway", broken)
    monkeypatch.setattr(cli_main, "run_turn", answering())
    result = chat(config_path, "alice", "one\n/eval\ntwo\n/quit\n")
    assert result.exit_code == 0 and "The evaluation could not run" in result.output and "secret value" not in result.output
    log = (project / "data" / "logs" / "app.log").read_text(encoding="utf-8")
    assert "the evaluation crashed: RuntimeError" in log and "secret value" not in log


def test_the_judges_failures_do_not_print_a_traceback_on_the_screen(config_path, monkeypatch):
    """`evalkit` logs a failed judge call on its own logger. With no handler on the root logger (as in the real chat, where pytest's capture is not installed) Python's
    last-resort handler would print that line and its traceback to stderr, onto the executive's screen."""
    import logging

    monkeypatch.setattr(logging.getLogger(), "handlers", [])
    judge = Judge(monkeypatch)
    judge.client.fail(unavailable(), times=None)
    monkeypatch.setattr(cli_main, "run_turn", answering())
    result = chat(config_path, "alice", "one\n/eval\n/quit\n")
    assert "judge call(s) failed" in result.output  # the failure is shown, in our words
    assert "Traceback" not in result.output and "failed on attempt" not in result.output


# the pieces ---------------------------------------------------------------------------------------------------------------------------------------

def test_the_chat_rubrics_are_the_offline_rubrics_with_the_chat_pipeline_added():
    from evalkit.rubrics import load_rubrics
    from src.evaluation.judging import RUBRICS_FILE
    from src.evaluation.live import chat_rubrics

    offline, live = load_rubrics(RUBRICS_FILE), chat_rubrics()
    assert list(live) == list(offline)
    for key, rubric in live.items():
        assert rubric.pipelines[-1] == "chat" and set(offline[key].pipelines) <= set(rubric.pipelines)
        assert rubric.as_prompt_block() == offline[key].as_prompt_block() and rubric.threshold == offline[key].threshold


def test_a_stopped_judge_answers_every_later_question_at_once():
    from pydantic import BaseModel

    from src.config import Settings
    from src.evaluation.judging import GatewayJudge, JudgeError

    class Verdict(BaseModel):
        score: int | None = None

    judge = GatewayJudge(FakeGateway(), Settings())
    judge.stop("interrupted")
    judge.stop("something else")  # the first reason is kept
    with pytest.raises(JudgeError, match="stopped: interrupted"):
        judge.ask(Verdict, "prompt")
    assert judge.failures == ["stopped"] and judge.stopped == "interrupted"


def test_a_before_call_hook_that_raises_stops_the_judge_for_good():
    from pydantic import BaseModel

    from src.config import Settings
    from src.evaluation.judging import GatewayJudge, JudgeError

    class Verdict(BaseModel):
        score: int | None = None

    def refuse():
        raise AgentError(ErrorKind.DAILY_BUDGET_USED, detail="1 of 1 tokens used today")

    gateway = FakeGateway()
    judge = GatewayJudge(gateway, Settings(), before_call=refuse)
    for _ in range(2):
        with pytest.raises(JudgeError):
            judge.ask(Verdict, "prompt")
    assert gateway.calls == [] and judge.stopped == "DAILY_BUDGET_USED"


def test_an_evaluations_file_that_cannot_be_written_does_not_lose_the_verdicts(project, config_path, monkeypatch):
    Judge(monkeypatch).always()
    monkeypatch.setattr(cli_main, "run_turn", answering())

    def broken(path, row):
        raise OSError("disk full")

    monkeypatch.setattr("src.evaluation.live.write_run_row", broken)
    result = chat(config_path, "alice", "one\n/eval\n/quit\n")
    assert "Evaluation of this chat (advisory)" in result.output and "The evaluation could not run" not in result.output
    assert "could not write the evaluation row" in (project / "data" / "logs" / "app.log").read_text(encoding="utf-8")
