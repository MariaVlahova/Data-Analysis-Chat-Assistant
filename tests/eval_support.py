"""Offline rigs for the evaluation (plan 09): the real app on the generated sample data, with a scripted model and no network.

`Offline` holds the settings of the local config, an `eval.harness.Harness` on them, and the `FakeGateway` the chat's agent was built with. The same
harness that a live run uses plays the conversation; only the model is made up. Two scripted models:

- `oracle` answers a golden case correctly: it writes the case's reference SQL (or a stated variation) and states the reference figures. A run with it must
  pass, which proves the machinery can pass; a run with a wrong figure must fail, which proves it can fail.
- `hijack` plays a model that was talked into an attack (`ModelScript` of `eval/datasets.py`): it writes the attacker's SQL and drafts the attacker's text.
"""
from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from eval.datasets import ChaosScenario, GoldenCase, GoldenTurn, ModelScript, TrajectoryCase
from eval.harness import Harness, Sample
from src.config import Settings, load_settings
from src.llm.chaos import Chaos
from src.llm.fake import FakeClock, FakeGateway, reply_for
from src.llm.routing import Step
from tests.agent_support import composer_reply, guard_reply, plan_reply, sql_reply
from tests.golden_support import REPO_ROOT, SEED_DIR

NO_DATA = "I did not find any data for this question."
SQL_STEPS = (Step.SQL_WRITE, Step.SQL_REPAIR, Step.LAST_REPAIR)
INTENT_OF_KIND = {"simple": "data_question", "why": "why_question", "report": "report_request"}
DATA_INTENTS = ("data_question", "why_question", "report_request")

# What a correct model writes where the reference SQL is not what it should run: the Result Guard is what hides the small groups, so the model asks for
# all of them.
ORACLE_SQL = {
    "small-groups-ceo": "SELECT u.state AS state, COUNT(*) AS customers FROM users u GROUP BY u.state ORDER BY customers DESC",
}
# The turns that are not about the data: the intent a correct Input Guard gives them, and the selector of a delete. `None`: the line is a reply to a
# confirmation, and no model reads it.
SPECIAL_TURNS: dict[str, list[tuple[str, dict | None] | None]] = {
    "schema-what-data-bob": [("schema_question", None)],
    "out-of-scope-alice": [("out_of_scope", None)],
    "save-then-delete-alice": [("data_question", None), ("save_report", None), ("delete_reports", {"kind": "last", "text": None}), None],
}

# What a correct model does on the turns of `eval/datasets/trajectories.yaml` that are not a plain data question (intent, selector, and the question as the Input Guard
# rewrites it so it needs no history), by case id. `False`: a data question; `None`: a reply to a confirmation, which no model reads. A turn the list does not reach is a
# data question that stands on its own.
TRAJECTORY_TURNS: dict[str, list[dict | bool | None]] = {
    "path-injection-stops-at-the-door": [{"intent": "out_of_scope"}],
    "path-schema-question-never-queries": [{"intent": "schema_question"}],
    "path-delete-waits-at-the-gate": [{"intent": "delete_reports", "selector": {"kind": "text_match", "text": "returns"}}, None],
    "memory-follow-up-keeps-the-period-and-the-metric": [
        False, {"standalone": "What was my revenue in 2024?"}, {"standalone": "What was my revenue in 2025?"},
    ],
    "memory-save-that-saves-what-was-shown": [False, {"intent": "save_report"}],
    "drift-a-refusal-leaves-the-memory-alone": [False, {"intent": "out_of_scope"}, {"intent": "save_report"}],
    "drift-memory-survives-a-restart": [False, {"standalone": "What was my revenue in 2024?"}, {"intent": "save_report"}],
}


def queries(ev) -> list[str]:
    """The SQL of every real query a turn sent to the data (the `bq.query` spans)."""
    return [str((span.get("attrs") or {}).get("sql", "")) for span in ev.spans if span.get("name") == "bq.query"]


def decisions(ev) -> list[tuple[str, str, str | None]]:
    """(guard, outcome, rule) of every guard decision a turn recorded, in order."""
    return [
        (str(item["attrs"].get("guard")), str(item["attrs"].get("outcome")), item["attrs"].get("rule"))
        for span in ev.spans for item in span.get("events") or [] if item.get("name") == "guard.decision"
    ]


def write_local_config(project: Path, local_db, *, golden: bool = False) -> Settings:
    """The shipped users.local.yaml, and a config like config.local.yaml that points at the test's copy of the sample data."""
    shutil.copy(REPO_ROOT / "users.local.yaml", project / "users.local.yaml")
    lines = ["app:", "  users_file: users.local.yaml", "bigquery:", "  backend: local", f'  local_db: "{local_db.path.as_posix()}"']
    if golden:
        shutil.copy(REPO_ROOT / "glossary.yaml", project / "glossary.yaml")
        lines += ["golden:", "  enabled: true", f'  seed_dir: "{SEED_DIR.as_posix()}"']
    (project / "config.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return load_settings(project / "config.yaml", load_env_file=False)


@dataclass
class Offline:
    settings: Settings
    harness: Harness
    gateway: FakeGateway | None
    out_dir: Path
    client: "SchemaClient | None" = None  # a chaos rig: the real gateway runs over this fake SDK client
    clock: FakeClock | None = None

    @property
    def scripted(self) -> FakeGateway:
        """The scripted gateway. The chaos rig has none (the real gateway runs over `client`): asking it for one is a mistake of the test, said plainly."""
        if self.gateway is None:
            raise AssertionError("this rig runs the real gateway over a fake SDK client (`chaos_eval`); script replies on `client.replies`, or use `offline_eval`")
        return self.gateway

    def reply(self, step: Step, reply: Any) -> None:
        self.scripted.always(step, reply)

    # a model that is right ---------------------------------------------------------------------------------------

    def oracle(self, case: GoldenCase) -> Callable[[int, str], None]:
        """`before_turn` for `harness.play_golden`: each turn gets the replies of a model that answers correctly."""
        user = self.harness.user(case.user)

        def before_turn(index: int, line: str) -> None:
            turn = case.turns[index - 1]
            gateway = self.scripted.reset_script()
            special = SPECIAL_TURNS.get(case.id, [])
            special_turn = special[index - 1] if index - 1 < len(special) else False
            if special_turn is None:
                return  # a reply to a confirmation: no model reads it
            intent, selector = special_turn if special_turn else (INTENT_OF_KIND.get(turn.turn_kind or "simple", "data_question"), None)
            gateway.always(Step.INPUT_GUARD, guard_reply(intent=intent, standalone=line, selector=selector))
            if intent not in ("data_question", "why_question", "report_request"):
                return
            ref = self.harness.reference(turn, user)
            sql = self.harness.macros.expand_sql(ORACLE_SQL.get(case.id) or turn.reference_sql or "SELECT 1 AS one", user.brands)
            for step in SQL_STEPS:
                gateway.always(step, sql_reply(sql))
            gateway.always(Step.PLANNER, plan_reply(line))
            gateway.always(Step.EVIDENCE_CHECK, {"enough": True, "missing": []})
            gateway.always(Step.COMPOSER, lambda call: composer_reply(self.oracle_body(turn, ref.values, str(call.contents), user.brands), "Answer"))

        return before_turn

    def oracle_body(self, turn: GoldenTurn, values: list[float], prompt: str, brands: tuple[str, ...]) -> str:
        """The text of a correct Composer: the reference figures, and whatever the turn says must be in the answer."""
        macros = self.harness.macros
        parts = ["Here are the figures: " + ", ".join(f"{value:,.2f}" for value in values) + "." if values else NO_DATA]
        if not values:
            # A model that reads its results states what it was given (an answer that states no value of the data is replaced, plan 12, A3): a turn with no
            # reference of its own, such as a follow-up that runs the default query, has the figure in its prompt.
            given = _FIRST_REVENUE.search(prompt)
            if given:
                parts = [f"Here is the figure: {given.group(1)}."]
        if turn.says or turn.says_any:
            parts += [macros.expand_text(text, brands) for text in (turn.says or turn.says_any[:1])]
        if "action_items" in turn.must_state:
            parts.append("Action items: review the pricing of the leading products.")
        if "data_ends" in turn.must_state:
            parts.append(f"The data ends on {macros.latest_date.isoformat()}.")
        if "no_data" in turn.must_state:
            parts.append(NO_DATA)
        labels = re.findall(r"Customer #\d+", prompt)
        if labels:
            parts.append("Customers: " + ", ".join(dict.fromkeys(labels)) + ".")
        return " ".join(parts)

    def trajectory(self, case: TrajectoryCase) -> Callable[[int, str], None]:
        """`before_turn` for `harness.play_trajectory`: the replies of a model that does what the case expects. A data turn runs the case's reference SQL (the revenue of
        2025 when it has none), a why turn plans two sub-questions, a recall states the figures of the turn it recalls, and the others are `TRAJECTORY_TURNS`."""
        user = self.harness.user(case.user)
        script = TRAJECTORY_TURNS.get(case.id, [])

        def before_turn(index: int, line: str) -> None:
            turn = case.turns[index - 1]
            step = script[index - 1] if index - 1 < len(script) else False
            if step is None:
                self.scripted.reset_script()
                return
            step = step or {}
            gateway = self.scripted.reset_script()
            intent = step.get("intent") or INTENT_OF_KIND.get(turn.turn_kind or "simple", "data_question")
            gateway.always(Step.INPUT_GUARD, guard_reply(intent=intent, standalone=step.get("standalone", line), selector=step.get("selector")))
            if intent not in DATA_INTENTS:
                return
            # A question that repeats an earlier one is answered with the earlier one's query and figures, as a model asked the same thing would.
            repeated = case.turns[(turn.memory.recalls or turn.memory.same_labels_as) - 1] if turn.memory and (turn.memory.recalls or turn.memory.same_labels_as) else turn
            values = self.harness.reference(turn, user).values or (self.harness.reference(repeated, user).values if turn.memory and turn.memory.recalls else [])
            sql = self.harness.macros.expand_sql(turn.reference_sql or repeated.reference_sql or REVENUE_2025, user.brands)
            for sql_step in SQL_STEPS:
                gateway.always(sql_step, sql_reply(sql))
            gateway.always(Step.PLANNER, plan_reply(line, f"{line} (split by brand)") if intent == "why_question" else plan_reply(line))
            gateway.always(Step.EVIDENCE_CHECK, {"enough": True, "missing": []})
            gateway.always(Step.COMPOSER, lambda call: composer_reply(self.oracle_body(turn, values, str(call.contents), user.brands), "Answer"))

        return before_turn

    # a model that was talked into it ----------------------------------------------------------------------------

    def hijack(self, script_for: Callable[[int], ModelScript], user_name: str) -> Callable[[int, str], None]:
        """`before_turn` for `harness.play_attack`: the replies of a model that does what the attack asks. `script_for(index)` is 0-based."""
        user = self.harness.user(user_name)

        def before_turn(index: int, line: str) -> None:
            script = script_for(index - 1)
            gateway = self.scripted.reset_script()
            gateway.always(Step.INPUT_GUARD, guard_reply(
                intent=script.intent, standalone=line, in_scope=script.in_scope, injection=script.injection_suspected, selector=script.selector,
            ))
            if script.intent in DATA_INTENTS:
                if script.sql is None:
                    raise AssertionError(f"the script of line {index} is a {script.intent} and says no SQL: the hijacked model has nothing to write")
                sql = self.harness.macros.expand_sql(" ".join(script.sql.split()), user.brands)
                for step in SQL_STEPS:
                    gateway.always(step, sql_reply(sql))
                gateway.always(Step.PLANNER, plan_reply(line))  # a why or report turn plans and checks its evidence first
                gateway.always(Step.EVIDENCE_CHECK, {"enough": True, "missing": []})
                gateway.always(Step.COMPOSER, composer_reply(script.body or "Here is what I found.", "Answer"))

        return before_turn


# faults, through the real gateway --------------------------------------------------------------------------------------------------------------------

# Revenue as the glossary defines it: plan 12 (D2) sends back a revenue query that does not leave out cancelled and returned items.
REVENUE_2025 = (
    "SELECT SUM(oi.sale_price) AS revenue FROM order_items oi "
    "WHERE oi.status NOT IN ('Cancelled', 'Returned') AND EXTRACT(YEAR FROM oi.created_at) = 2025"
)
_FIRST_REVENUE = re.compile(r'"revenue": (-?[0-9][0-9.]*)')


def revenue_in_results(contents: str) -> dict:
    """The Composer of a model that reads its results: it states the first revenue figure it was given (an answer that states no value of the data is replaced, plan 12, A3)."""
    match = _FIRST_REVENUE.search(contents)
    return composer_reply(f"Your revenue for the year was {match.group(1)}." if match else "Your revenue for the year is in the table above.", "Revenue")


class _SchemaModels:
    def __init__(self, owner: "SchemaClient") -> None:
        self._owner = owner

    def generate_content(self, *, model: str, contents: Any, config: Any):
        self._owner.requests.append((model, config))
        name = getattr(getattr(config, "response_schema", None), "__name__", "")
        if name not in self._owner.replies:
            raise AssertionError(f"SchemaClient has no reply for the schema {name!r}")
        reply = self._owner.replies[name]
        return reply_for(json.dumps(reply(str(contents)) if callable(reply) else reply))

    def list(self):
        return []


class SchemaClient:
    """A fake SDK client that answers by the schema a step asks for, so the real gateway, with its retries, breaker and chaos, can run a whole turn whatever
    order the steps come in (a repair or a re-ask changes the order, and a queue of replies would not survive that). It has what `LLMGateway` needs of
    `genai.Client` (`models.generate_content`, `models.list`) and nothing else: faults are injected by `Chaos`, not by rules on the client. `replies` is a plain
    dict a test may change between turns: the Input Guard's reply, for one, decides which kind of request the turn is. A reply may be a function of the prompt's
    contents, which is how the Composer states a figure of the result it was given."""

    def __init__(self) -> None:
        self.requests: list = []  # (model, config) of every call
        self.models = _SchemaModels(self)
        self.replies: dict[str, Any] = {
            "InputGuardOut": guard_reply(standalone="What was my revenue in 2025?"), "SqlOut": sql_reply(REVENUE_2025),
            "ComposerOut": revenue_in_results, "PlanOut": plan_reply("Revenue in 2025"),
            "EvidenceOut": {"enough": True, "missing": []},
        }


def scripted_chaos(scenario: ChaosScenario) -> Chaos | None:
    """The `Chaos` of a scenario with scripted faults; None for a random profile (the harness builds that one from level and seed)."""
    if not scenario.faults:
        return None
    chaos = Chaos.scripted()
    for fault in scenario.faults:
        if fault.kind == "llm":
            chaos.fail_llm(fault.fault, times=fault.times, step=Step(fault.step) if fault.step else None)
        elif fault.kind == "bq":
            chaos.fail_bq(fault.fault, times=fault.times)
        else:
            chaos.corrupt_next_sql(fault.fault, times=fault.times)
    return chaos


def play_chaos(offline: Offline, scenario: ChaosScenario, before_turn: Callable[[int, str], None] | None = None) -> Sample:
    return offline.harness.play_chaos(scenario, chaos_engine=scripted_chaos(scenario), before_turn=before_turn)
