"""What the node functions close over (plan 07): clients, guards, the registry, and the per-turn runtime.

None of this is graph state. The pseudonymizer holds real customer IDs, the clients hold keys, and the budget is a clock, so
they live here, in one object that the node factories capture. `copy` and `pickle` refuse the pseudonymizer on purpose: if
one of these ever reached a checkpoint, the write would fail loudly (plan 06).

`TurnRuntime` is the part that belongs to one run of one turn: the budget, the progress callback, the user as the policy
check read them this turn, and a scratch space for text the Output Guard has not passed yet. It is looked up by conversation
(one active run per conversation, A19). It is not serializable, and a restart starts fresh, which is fine: the resume path
after a confirmation makes no model call.
"""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable

from src.access.users import UserContext
from src.cli.render import ProgressFn
from src.config import Settings
from src.data_access.catalog import SchemaCatalog
from src.data_access.safe_runner import SafeQueryRunner
from src.errors import AgentError, ErrorKind
from src.guards.allowlist import Allowlist
from src.guards.output_guard import OutputGuard
from src.guards.result_guard import ResultGuard
from src.guards.sql_guard import SQLGuard
from src.llm.budget import TurnBudget
from src.llm.chaos import Chaos
from src.llm.gateway import Gateway
from src.storage.audit import AuditEntry
from src.tools.registry import ToolRegistry

# The entries of `TurnRuntime.scratch`: text the Output Guard has not passed. One list, used by every writer and by the error boundary
# that drops them all, so a new entry cannot be left out of the drop.
DRAFT, FINAL_TEXT, TITLE, NOTES = "draft", "final_text", "title", "notes"
UNCHECKED_KEYS = (DRAFT, FINAL_TEXT, TITLE, NOTES)

SLOW_AFTER_S = 10.0  # NFR2: progress shows the time once a turn has taken this long
COMPOSER_RESERVE = 1  # model calls the SQL loop and the Evidence Check leave for the Composer


@dataclass
class TurnRuntime:
    budget: TurnBudget
    progress: ProgressFn = lambda text: None
    user: UserContext | None = None  # read by `policy_context` at the start of every turn
    scratch: dict[str, Any] = field(default_factory=dict)  # text that has not passed the Output Guard: never copied into state

    def hold(self, key: str, value: Any) -> None:
        """Keep unchecked text for a later node. Only the named entries: anything else would also escape the boundary's drop."""
        if key not in UNCHECKED_KEYS:
            raise AgentError(ErrorKind.INTERNAL, detail=f"{key!r} is not an entry of the scratch space")
        self.scratch[key] = value

    def drop_unchecked(self) -> None:
        for key in UNCHECKED_KEYS:
            self.scratch.pop(key, None)

    def say(self, text: str) -> None:
        """One short progress line. After `SLOW_AFTER_S` it carries the time so far. Never the answer (S9)."""
        seconds = self.budget.elapsed_seconds
        self.progress(f"{text} ({seconds:.0f} s)" if seconds >= SLOW_AFTER_S else text)

    def has_room(self, reserve: int = COMPOSER_RESERVE) -> bool:
        """May a model call be made and still leave `reserve` calls, and some time, for the answer?"""
        return self.budget.calls_remaining > reserve and self.budget.remaining_seconds() > 0


@dataclass
class Deps:
    settings: Settings
    gateway: Gateway
    runner: SafeQueryRunner
    sql_guard: SQLGuard
    result_guard: ResultGuard
    output_guard: OutputGuard
    catalog: SchemaCatalog
    allowlist: Allowlist
    registry: ToolRegistry
    load_user: Callable[[], UserContext]  # reads users.yaml again each turn, so a change of brands applies at once (NFR15)
    persona_text: Callable[[], str] = lambda: ""  # the Composer's persona block, "" when there is none
    glossary: Callable[[str], list[str]] = lambda question: []  # plan 10 fills this hook
    golden: Callable[[str], list[dict[str, str]]] = lambda question: []  # plan 10 fills this hook
    chaos: Chaos | None = None
    today: Callable[[], date] = lambda: datetime.now(timezone.utc).date()  # UTC (A17); a test sets the date
    clock: Callable[[], float] = time.monotonic  # the turn budget's clock; a test moves it to reach the time limit
    # The key of the hash that binds a confirmation to the exact set of IDs. `build_agent` derives a lasting one from the
    # installation's salt, so a confirmation still matches after a restart; this default is for a `Deps` made by hand.
    hash_key: bytes = field(default_factory=lambda: secrets.token_bytes(32), repr=False)
    # Where the gate's events go (plan 08, F26): a delete was asked for, confirmed, cancelled or found stale. A row has the user hash,
    # a count and the ID-set hash, never a title. Nothing by default, so a `Deps` made by hand needs no database.
    audit: Callable[[AuditEntry], None] = lambda entry: None
    _runtimes: dict[str, TurnRuntime] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def begin(self, conversation_id: str, runtime: TurnRuntime) -> None:
        with self._lock:
            self._runtimes[conversation_id] = runtime

    def end(self, conversation_id: str) -> None:
        with self._lock:
            self._runtimes.pop(conversation_id, None)

    def runtime(self, state: dict[str, Any]) -> TurnRuntime:
        with self._lock:
            runtime = self._runtimes.get(state.get("conversation_id", ""))
        if runtime is None:
            raise AgentError(ErrorKind.INTERNAL, detail="no active run for this conversation")
        return runtime
