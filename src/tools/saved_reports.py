"""The Saved Reports tools (plan 08, F20 to F27): save, list, find, delete and undo, on the SQLite store.

They keep the interface plan 07 built (`ToolManifest`, `Tool.run`, `ToolContext`, `ToolResult`, `Target`), so the graph needed no
change to use them. The tools are thin on purpose:

- the owner is `ctx.user`, never an argument the model could have written (F21);
- `find` is the finder of `delete`: it returns targets whose `ref` is the real report ID, which the graph keeps out of its state;
- `delete` gets the IDs the graph just found and re-confirmed, and the store skips any that are not the owner's;
- every change is one store call, so the effect and its audit row commit together, and a failure leaves nothing half done;
- a retry with the same idempotency key returns the first result (`reports/idempotency.py`).

The words come from `reports/messages.py`. `save` scans the text again before it stores it (A20).
"""
from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence

from src.reports import idempotency, messages
from src.reports.store import Actor, ReportStore
from src.textutil import squash
from src.tools.base import Target, Tool, ToolContext, ToolManifest, ToolResult, clean_selector

ROLES = frozenset({"brand_manager", "ceo", "analyst", "admin"})  # any role in the prototype
TIMEOUT_S = 5.0
MAX_TITLE = 120
FALLBACK_TITLE = "Saved answer"

Scan = Callable[[str], Sequence[Any]]  # `OutputGuard.scan`: findings of personal data in a text


def _manifest(name: str, description: str, schema: Mapping[str, str], risk: str, *, idempotent: bool, reversible: bool,
              inverse: str | None, intents: tuple[str, ...] = (), finder: str | None = None) -> ToolManifest:
    return ToolManifest(
        name=name, description=description, input_schema=schema, risk_class=risk,  # type: ignore[arg-type]
        allowed_roles=ROLES, timeout_s=TIMEOUT_S, cost_hint="free", idempotent=idempotent, reversible=reversible,
        inverse=inverse, intents=intents, finder=finder, noun="report",
    )


SELECTOR_SCHEMA = {"selector": "conversation | text_match | all | last"}
SAVE_MANIFEST = _manifest(
    idempotency.SAVE_TOOL, "Save the last answer as a report.", {}, "write",
    idempotent=True, reversible=True, inverse=idempotency.DELETE_TOOL, intents=("save_report",),
)
LIST_MANIFEST = _manifest(
    "saved_reports.list", "List the user's saved reports.", {}, "read",
    idempotent=True, reversible=False, inverse=None, intents=("list_reports",),
)
FIND_MANIFEST = _manifest(
    "saved_reports.find", "Find the user's reports that match a selector.", SELECTOR_SCHEMA, "read",
    idempotent=True, reversible=False, inverse=None,
)
DELETE_MANIFEST = _manifest(
    idempotency.DELETE_TOOL, "Delete saved reports found by a selector (soft delete, can be undone).", SELECTOR_SCHEMA, "destructive",
    idempotent=True, reversible=True, inverse=idempotency.UNDO_TOOL, intents=("delete_reports",), finder=FIND_MANIFEST.name,
)
UNDO_MANIFEST = _manifest(
    idempotency.UNDO_TOOL, "Bring back the reports of the latest delete.", {}, "write",
    idempotent=True, reversible=True, inverse=idempotency.DELETE_TOOL, intents=("undo_delete",),
)


def actor(ctx: ToolContext) -> Actor:
    return Actor(ctx.user.user_id, ctx.user.user_hash, ctx.conversation_id, ctx.turn_id, ctx.trace_id)


class SavedReportsTools:
    """The five tools of plan 08's table, sharing one store. `tools()` gives them to a `ToolRegistry`."""

    def __init__(self, store: ReportStore, *, scan: Scan) -> None:
        self.store = store
        self._scan = scan

    def tools(self) -> list[Tool]:
        return [_Save(self), _List(self), _Find(self), _Delete(self), _Undo(self)]

    def effect(self, who: Actor, action: str, key: str, make: Callable[[], ToolResult]) -> ToolResult:
        """Run a change. If it raises, the store has rolled it back; say so in the audit log, then let the graph report it."""
        try:
            return make()
        except Exception:
            self.store.record(who, action, "failed", key=key, detail="rolled back")
            raise


class _Base:
    manifest: ToolManifest

    def __init__(self, owner: SavedReportsTools) -> None:
        self._o = owner


class _Save(_Base):
    manifest = SAVE_MANIFEST

    def run(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        answer = ctx.last_answer
        if answer is None or not answer.text.strip():
            return ToolResult(messages.NOTHING_TO_SAVE)
        who = actor(ctx)
        title = squash(answer.title, MAX_TITLE) or FALLBACK_TITLE
        # The graph's key for this request, with the hash of the text added: the same request with other text is another request.
        key = idempotency.save_key(ctx.conversation_id, ctx.turn_id, answer.text, ctx.idempotency_key)
        if self._o._scan(f"{title}\n{answer.text}"):  # A20: a saved report holds no personal data. It is not stored, and not echoed.
            self._o.store.record(who, "save", "blocked", key=key, detail="personal data in the text", once=True)
            return ToolResult(messages.NOT_SAVED_PERSONAL)

        def make() -> ToolResult:
            saved = self._o.store.save(who, title=title, body=answer.text, search_text=answer.searchable or answer.text, key=key)
            return ToolResult(messages.saved(saved.report.title, saved.report.date), count=1)

        return self._o.effect(who, "save", key, make)


class _List(_Base):
    manifest = LIST_MANIFEST

    def run(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        reports = self._o.store.list_reports(ctx.user.user_id)
        if not reports:
            return ToolResult(messages.NO_REPORTS)
        return ToolResult(messages.listing([(report.title, report.date) for report in reports]), count=len(reports))


class _Find(_Base):
    manifest = FIND_MANIFEST

    def run(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        selector = clean_selector(args.get("selector"))  # the model's selector, checked again in code
        found = self._o.store.find(ctx.user.user_id, ctx.conversation_id, selector["kind"], selector["text"]) if selector else []
        targets = tuple(Target(report.report_id, report.title, report.date) for report in found)
        return ToolResult("" if targets else messages.NO_MATCH, count=len(targets), targets=targets)


class _Delete(_Base):
    manifest = DELETE_MANIFEST

    def run(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        ids = [str(report_id) for report_id in args.get("ids") or ()]
        who = actor(ctx)
        # The graph supplies the key (conversation, the request turn, tool, ID-set hash). A caller with none gets the same recipe.
        key = ctx.idempotency_key or idempotency.delete_key(ctx.conversation_id, ctx.turn_id, self._o.store.id_set_hash(ids))

        def make() -> ToolResult:
            outcome = self._o.store.delete(who, ids, key=key)
            if outcome.count == 0:
                return ToolResult(messages.NO_MATCH)
            return ToolResult(messages.deleted(outcome.count, self._o.store.undo_days), count=outcome.count)

        return self._o.effect(who, "delete_executed", key, make)


class _Undo(_Base):
    manifest = UNDO_MANIFEST

    def run(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        who = actor(ctx)
        key = ctx.idempotency_key or idempotency.undo_key(ctx.conversation_id, ctx.turn_id)

        def make() -> ToolResult:
            outcome = self._o.store.undo(who, key=key)
            if outcome.status == "restored":
                return ToolResult(messages.restored(outcome.count), count=outcome.count)
            return ToolResult({
                "none": messages.NO_DELETE_TO_UNDO, "already": messages.ALREADY_UNDONE, "expired": messages.UNDO_EXPIRED,
            }[outcome.status])

        return self._o.effect(who, "undo", key, make)
