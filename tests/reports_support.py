"""A rig for the Saved Reports tests (plan 08): the real graph with the real tools on a migrated SQLite file, an optional
`SqliteSaver` on the same file, a clock the test moves, and a `restart()` that throws the whole agent away and builds it again
from the files, the way a new process would.

    env = ReportsEnv(project, fake_bigquery, checkpoints=True)
    env.seed("Revenue Q1")
    env.delete(text_match("revenue"))          # the graph stops at the HITL Gate
    env.restart()                              # a new process: nothing is left in memory
    env.rig.ask("yes")                         # the reply is delivered to the paused conversation
"""
from __future__ import annotations

import itertools
import json
import re
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.agent.checkpointer import close_checkpointer, open_checkpointer
from src.config import load_settings
from src.guards.output_guard import OutputGuard
from src.llm.routing import Step
from src.reports.store import ReportStore
from src.storage.db import Database, utc_now
from src.tools.base import LastAnswer, ToolContext
from src.tools.confirmation import id_set_hash
from src.tools.registry import ToolRegistry
from src.tools.saved_reports import SavedReportsTools
from tests.agent_support import Rig, guard_reply, make_rig
from tests.guard_support import ALICE

ID_PATTERN = re.compile(r"r-[0-9a-f]{8}")
BATCH_PATTERN = re.compile(r"b-[0-9a-f]{8}")
CURRENT = {"kind": "conversation", "text": None}
EVERYTHING = {"kind": "all", "text": None}
NEWEST = {"kind": "last", "text": None}


def text_match(text: str) -> dict[str, Any]:
    return {"kind": "text_match", "text": text}


class Clock:
    """The store's clock: it stands still until the test moves it."""

    def __init__(self, start: datetime = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)) -> None:
        self.moment = start

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, **delta: float) -> None:
        self.moment += timedelta(**delta)


class ReportsEnv:
    def __init__(self, project: Path, fake_bigquery, user=ALICE, *, config: str | None = None, checkpoints: bool = False,
                 clock: Clock | None = None, **rig_options: Any) -> None:
        self.project, self.fake_bigquery, self.user = project, fake_bigquery, user
        self.checkpoints = checkpoints
        self._rig_options = rig_options
        self._seeds = itertools.count(1)  # a key for each seeded report: the same title twice is two reports
        self.clock = clock or Clock()
        if config is not None:
            (project / "config.yaml").write_text(config, encoding="utf-8")
        self.settings = load_settings(project / "config.yaml", load_env_file=False)
        with Database.from_settings(self.settings) as database:
            database.migrate()
        self.saver = None
        self.rig: Rig
        self.store: ReportStore
        self.registry: ToolRegistry
        self._build(turns=0)

    # building and rebuilding ----------------------------------------------------------------------
    def _build(self, turns: int) -> None:
        self.store = ReportStore.from_settings(self.settings, hash_ids=lambda ids: id_set_hash(ids, self.rig.agent.deps.hash_key), now=self.clock)
        self.registry = ToolRegistry(SavedReportsTools(self.store, scan=OutputGuard().scan).tools())
        self.saver = open_checkpointer(self.settings) if self.checkpoints else None
        self.rig = make_rig(
            self.project, self.user, fake_bigquery=self.fake_bigquery, registry=self.registry, audit=self.store.audit.append,
            **({"checkpointer": self.saver} if self.saver is not None else {}), **self._rig_options,
        )
        self.rig.real_tools = True
        self.rig.turns = turns  # turn IDs carry on across a restart: a conversation is resumed, it does not start over
        self.rig.have_conversation(self.rig.conversation_id)

    def restart(self) -> None:
        """A new process: the old agent, store, registry and saver are dropped and everything is built again from the files."""
        turns = self.rig.turns
        close_checkpointer(self.saver)
        self._build(turns)

    def close(self) -> None:
        close_checkpointer(self.saver)

    # asking ---------------------------------------------------------------------------------------
    def say(self, intent: str, selector=None, text: str = "do it"):
        self.rig.gateway.queue(Step.INPUT_GUARD, guard_reply(intent, selector=selector))
        return self.rig.ask(text)

    def delete(self, selector, text: str = "delete those reports"):
        return self.say("delete_reports", selector, text)

    def reply(self, text: str):
        return self.rig.ask(text)

    # data -----------------------------------------------------------------------------------------
    def seed(self, title: str, body: str = "body", *, conversation: str | None = None, user=None, key: str | None = None,
             turn: str = "t-0", searchable: str = ""):
        """Save a report straight through the tool (no turn), as `user`, in `conversation`."""
        user = user or self.user
        conversation = conversation or self.rig.conversation_id
        self.have_conversation(conversation, user)
        ctx = ToolContext(
            user, conversation, turn, "00000000", LastAnswer(title, body, searchable), key or f"seed-{next(self._seeds)}",
        )
        return self.registry.get("saved_reports.save").run({}, ctx)

    def have_conversation(self, conversation: str, user=None) -> None:
        """The conversation row a report points to (a foreign key)."""
        with closing(self.store.connect()) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO conversations (id, user_id, created_at, last_active_at) VALUES (?, ?, ?, ?)",
                (conversation, (user or self.user).user_id, utc_now(), utc_now()),
            )

    def titles(self, user=None) -> list[str]:
        return sorted(report.title for report in self.store.list_reports((user or self.user).user_id))

    def rows(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        with closing(self.store.connect()) as conn:
            return [dict(row) for row in conn.execute(sql, args)]

    def execute(self, sql: str, *args: Any) -> None:
        """Run one statement on a fresh connection: a trigger that makes a write fail, a clock change, a row to tamper with."""
        with closing(self.store.connect()) as conn:
            conn.execute(sql, args)

    def audit(self, action: str | None = None) -> list[dict[str, Any]]:
        return self.store.audit.entries(action=action)

    def state(self, conversation: str | None = None):
        return self.rig.agent.graph.get_state({"configurable": {"thread_id": conversation or self.rig.conversation_id}})

    def everything_stored(self) -> str:
        """Every checkpoint, trace and audit row as one string, to search for something that must not be there."""
        thread = {"configurable": {"thread_id": self.rig.conversation_id}}
        blobs = [json.dumps(s.values, default=str) for s in self.rig.agent.graph.get_state_history(thread)]
        blobs += [path.read_text(encoding="utf-8") for path in (self.project / "data" / "traces").glob("*")]
        blobs += [json.dumps(row, default=str) for row in self.audit()]
        return "\n".join(blobs)
