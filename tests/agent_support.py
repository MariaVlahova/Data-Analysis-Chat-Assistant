"""A rig for the graph tests (plan 07): the real graph, guards and query runner, with a scripted gateway and the fake BigQuery
client. No network. `Rig.ask` runs one turn inside a trace, the way the CLI does, so spans and the metrics row are real.

    rig = make_rig(project)
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply()).queue(Step.SQL_WRITE, sql_reply(SQL)).queue(Step.COMPOSER, composer_reply("..."))
    rig.client.add("revenue", df=pd.DataFrame(...))
    result = rig.ask("what was revenue?")
"""
from __future__ import annotations

import json
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.access.users import UserContext
from src.agent.runtime import AgentRuntime, TurnResult, build_agent
from src.config import Settings, load_settings
from src.errors import new_trace_id
from src.guards.allowlist import load_allowlist
from src.llm.fake import FakeGateway
from src.observability import Tracer, span
from src.storage.db import Database, open_connection, utc_now
from tests.fake_bigquery import FakeClient
from tests.guard_support import ALICE

# Revenue as the glossary defines it (plan 12, D2 sends back a revenue query that does not leave out cancelled and returned items).
REVENUE_SQL = (
    "SELECT p.brand, SUM(oi.sale_price) AS revenue FROM order_items oi JOIN products p ON p.id = oi.product_id "
    "WHERE oi.status NOT IN ('Cancelled', 'Returned') GROUP BY p.brand ORDER BY revenue DESC"
)


def guard_reply(
    intent: str = "data_question", standalone: str = "", *, in_scope: bool = True, injection: bool = False, selector: dict | None = None,
) -> dict[str, Any]:
    return {"in_scope": in_scope, "injection_suspected": injection, "intent": intent, "standalone_question": standalone, "selector": selector}


def sql_reply(sql: str = REVENUE_SQL, interpretation: str = "Revenue is the sum of item sale prices.") -> dict[str, Any]:
    return {"sql": sql, "interpretation": interpretation}


def composer_reply(body: str, title: str = "") -> dict[str, Any]:
    return {"title": title, "body_markdown": body}


def plan_reply(*subquestions: str, assumptions: list[str] | None = None, date_range: dict | None = None) -> dict[str, Any]:
    return {
        "subquestions": [{"id": f"s{n}", "text": text, "depends_on": [], "tables": ["order_items", "products"]} for n, text in enumerate(subquestions, 1)],
        "assumptions": assumptions or [], "date_range": date_range,
    }


@dataclass
class Rig:
    agent: AgentRuntime
    gateway: FakeGateway
    client: FakeClient
    tracer: Tracer
    settings: Settings
    user: UserContext
    conversation_id: str = "conv-test0001"
    turns: int = 0
    progress: list[str] = field(default_factory=list)
    last_trace_id: str = ""
    last_row: dict[str, Any] = field(default_factory=dict)
    baseline_jobs: int = 0  # the BigQuery jobs the catalog's startup reads already ran when the rig was made
    real_tools: bool = False  # the rig uses the real Saved Reports tools, which need migrated tables (plan 08)

    def have_conversation(self, conversation_id: str) -> None:
        """The conversation row a saved report points to (a foreign key). A no-op for a rig on the in-memory stub."""
        if not self.real_tools:
            return
        with closing(open_connection(self.settings.db_path)) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO conversations (id, user_id, created_at, last_active_at) VALUES (?, ?, ?, ?)",
                (conversation_id, self.user.user_id, utc_now(), utc_now()),
            )

    @property
    def queries(self) -> list:
        """The real (not dry-run) BigQuery jobs the agent started since the rig was made."""
        return [job for job in self.client.jobs[self.baseline_jobs:] if not job.dry_run]

    def ask(self, text: str, conversation_id: str | None = None) -> TurnResult:
        self.turns += 1
        trace_id = new_trace_id()
        conversation = conversation_id or self.conversation_id
        self.have_conversation(conversation)
        with self.tracer.start_turn(self.user.user_hash, conversation, f"t-{self.turns}", trace_id=trace_id, question=text) as turn:
            result = self.agent.run(
                text=text, conversation_id=conversation, turn_id=f"t-{self.turns}", trace_id=trace_id,
                user_hash=self.user.user_hash, progress=self.progress.append,
            )
        self.last_trace_id, self.last_row = trace_id, turn.row
        return result

    def spans(self, trace_id: str | None = None) -> list[dict[str, Any]]:
        path = self.tracer.trace_path(trace_id or self.last_trace_id)
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def span_names(self, trace_id: str | None = None) -> list[str]:
        return [s["name"] for s in self.spans(trace_id)]

    def calls(self, step) -> int:
        return len(self.gateway.calls_for(step))


def make_rig(project: Path, user: UserContext = ALICE, *, fake_bigquery: FakeClient, config: str | None = None, **agent_options: Any) -> Rig:
    """The graph for `user`, in the temporary project folder. `fake_bigquery` is the conftest fixture's client."""
    from src.data_access.catalog import load_catalog

    if config is not None:
        (project / "config.yaml").write_text(config, encoding="utf-8")
    settings = load_settings(project / "config.yaml", load_env_file=False)
    allowlist = load_allowlist(settings)
    catalog = load_catalog(settings).catalog.with_policy(allowlist)
    from src.data_access.safe_runner import SafeQueryRunner

    gateway = agent_options.pop("gateway", None) or FakeGateway()
    # No real waiting between BigQuery retries: a test of an outage must not take seconds.
    runner = agent_options.pop("runner", None) or SafeQueryRunner(settings, sleep=lambda seconds: None, chaos=agent_options.get("chaos"))
    real_tools = "registry" not in agent_options
    if real_tools:
        # The default tools are the real Saved Reports on data/db.sqlite3 (plan 08): migrate it, as `chat` does at startup.
        with Database.from_settings(settings) as database:
            database.migrate()
    agent = build_agent(
        settings, user=user, catalog=catalog, allowlist=allowlist, gateway=gateway, runner=runner,
        load_user=agent_options.pop("load_user", lambda: user), **agent_options,
    )
    rig = Rig(
        agent, gateway, fake_bigquery, Tracer(project / "data" / "traces", project / "data" / "runs.jsonl"), settings, user,
        baseline_jobs=len(fake_bigquery.jobs), real_tools=real_tools,
    )
    rig.have_conversation(rig.conversation_id)
    return rig


def make_real_gateway_rig(project: Path, user: UserContext = ALICE, *, fake_bigquery: FakeClient, debug_prompts: bool = False, **replies: Any):
    """The same graph with the REAL `LLMGateway` over a fake SDK client that answers by schema (`SchemaClient`), for what only the real gateway does: the exchange
    file of `--debug-prompts` (plan 12). `replies` replace the client's replies by schema name (`InputGuardOut`, `SqlOut`, `ComposerOut`, `PlanOut`, `EvidenceOut`).
    Returns the rig and the client."""
    from src.llm.fake import FakeClock
    from src.llm.gateway import LLMGateway
    from tests.eval_support import SchemaClient

    settings = load_settings(project / "config.yaml", load_env_file=False)
    client = SchemaClient()
    client.replies.update({"InputGuardOut": guard_reply(standalone="Revenue by brand"), "SqlOut": sql_reply(), **replies})
    clock = FakeClock()
    rig = make_rig(project, user, fake_bigquery=fake_bigquery, gateway=LLMGateway(settings, client=client, clock=clock, sleep=clock.sleep))
    rig.tracer.debug_prompts = debug_prompts
    return rig, client


def echo_turn(session, text, *, trace_id, progress):
    """A stand-in turn for CLI tests that are about the session, the trace or the numbering, not about the agent."""
    progress("Thinking...")
    with span("echo", "step", summary="a test turn: the question is echoed, nothing is looked up"):
        return f"You asked: {text}\n\n_{session.conversation_id}, {session.turn_id}, scope: {session.user.scope_mode}_"
