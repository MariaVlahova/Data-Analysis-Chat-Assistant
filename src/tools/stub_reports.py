"""A stubbed Saved Reports tool (plan 07): in memory, same interface as plan 08's real tools.

Plan 08 replaces this with the SQLite store, the audit log and the idempotency proof, and keeps the `Tool` interface. What
this stub does prove is the shape: the owner comes from the context, never from an argument; the finder returns targets and
the graph keeps their IDs out of its state; a destructive tool takes the IDs the node just found; a repeat with the same
idempotency key has no second effect.

Nothing here survives the process.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from src.tools.base import Target, Tool, ToolContext, ToolManifest, ToolResult, clean_selector

_ROLES = frozenset({"brand_manager", "ceo", "analyst", "admin"})
_UTC_NOW = lambda: datetime.now(timezone.utc)  # noqa: E731 - a default, replaced in tests

NOTHING_TO_SAVE = "There is nothing to save yet. Ask a question first, then ask me to save the answer."
NO_MATCH = "I found no reports matching that."


@dataclass
class _Report:
    report_id: str
    owner: str
    conversation_id: str
    turn_id: str
    title: str
    body: str
    searchable: str  # what a text search looks in besides the title: the body without the scope line and the notes
    created_at: datetime
    deleted_at: datetime | None = None
    batch: str | None = None


@dataclass
class _Batch:
    batch_id: str
    owner: str
    created_at: datetime
    undone: bool = False


@dataclass
class _Store:
    reports: dict[str, _Report] = field(default_factory=dict)
    batches: dict[str, _Batch] = field(default_factory=dict)
    done: dict[str, ToolResult] = field(default_factory=dict)  # idempotency key -> the first result


def _manifest(name: str, description: str, schema: Mapping[str, str], risk: str, *, idempotent: bool, reversible: bool,
              inverse: str | None, intents: tuple[str, ...] = (), finder: str | None = None) -> ToolManifest:
    return ToolManifest(
        name=name, description=description, input_schema=schema, risk_class=risk,  # type: ignore[arg-type]
        allowed_roles=_ROLES, timeout_s=5.0, cost_hint="free", idempotent=idempotent, reversible=reversible,
        inverse=inverse, intents=intents, finder=finder, noun="report",
    )


class StubSavedReports:
    """The five tools of plan 08's table, sharing one in-memory store. `tools()` gives them to a `ToolRegistry`."""

    def __init__(self, *, undo_days: int = 30, now: Callable[[], datetime] = _UTC_NOW) -> None:
        self._store = _Store()
        self._undo = timedelta(days=undo_days)
        self._now = now

    def tools(self) -> list[Tool]:
        return [_Save(self), _List(self), _Find(self), _Delete(self), _Undo(self)]

    # shared pieces -----------------------------------------------------------------------------
    def _own(self, ctx: ToolContext, *, deleted: bool = False) -> list[_Report]:
        """Owner enforcement (F21): every read starts from the owner in the context."""
        mine = [r for r in self._store.reports.values() if r.owner == ctx.user.user_id and (r.deleted_at is not None) == deleted]
        return sorted(mine, key=lambda r: r.created_at)

    def _select(self, selector: Mapping[str, Any] | None, ctx: ToolContext) -> list[_Report]:
        picked = clean_selector(selector)
        if picked is None:
            return []
        reports = self._own(ctx)
        kind = picked["kind"]
        if kind == "conversation":
            return [r for r in reports if r.conversation_id == ctx.conversation_id]
        if kind == "text_match":
            needle = str(picked["text"]).casefold()
            # Not the scope line ("your brands: A, B") or the notes: they are on every answer, so a brand name would match every report.
            return [r for r in reports if needle in r.title.casefold() or needle in r.searchable.casefold()]
        if kind == "last":
            return reports[-1:]
        return reports  # all

    def _once(self, ctx: ToolContext, make: Callable[[], ToolResult]) -> ToolResult:
        """A repeat with the same idempotency key returns the first result and changes nothing."""
        key = ctx.idempotency_key
        if key is not None and key in self._store.done:
            return self._store.done[key]
        result = make()
        if key is not None:
            self._store.done[key] = result
        return result

    @staticmethod
    def _date(moment: datetime) -> str:
        return moment.date().isoformat()


class _Base:
    manifest: ToolManifest

    def __init__(self, owner: StubSavedReports) -> None:
        self._o = owner


class _Save(_Base):
    manifest = _manifest(
        "saved_reports.save", "Save the last answer as a report.", {}, "write",
        idempotent=True, reversible=True, inverse="saved_reports.delete", intents=("save_report",),
    )

    def run(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        answer = ctx.last_answer
        if answer is None or not answer.text.strip():
            return ToolResult(NOTHING_TO_SAVE)

        def make() -> ToolResult:
            report = _Report("r-" + secrets.token_hex(4), ctx.user.user_id, ctx.conversation_id, ctx.turn_id,
                             answer.title, answer.text, answer.searchable or answer.text, self._o._now())
            self._o._store.reports[report.report_id] = report
            return ToolResult(f"Saved \"{answer.title}\" on {self._o._date(report.created_at)}.", count=1)

        return self._o._once(ctx, make)


class _List(_Base):
    manifest = _manifest(
        "saved_reports.list", "List the user's saved reports.", {}, "read",
        idempotent=True, reversible=False, inverse=None, intents=("list_reports",),
    )

    def run(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        reports = self._o._own(ctx)
        if not reports:
            return ToolResult("You have no saved reports.")
        lines = [f"You have {len(reports)} saved report{'s' if len(reports) != 1 else ''}:"]
        lines += [f"{n}. {r.title} ({self._o._date(r.created_at)})" for n, r in enumerate(reports, 1)]
        return ToolResult("\n".join(lines), count=len(reports))


class _Find(_Base):
    manifest = _manifest(
        "saved_reports.find", "Find the user's reports that match a selector.", {"selector": "conversation | text_match | all | last"},
        "read", idempotent=True, reversible=False, inverse=None,
    )

    def run(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        found = self._o._select(args.get("selector"), ctx)
        targets = tuple(Target(r.report_id, r.title, self._o._date(r.created_at)) for r in found)
        return ToolResult(NO_MATCH if not targets else "", count=len(targets), targets=targets)


class _Delete(_Base):
    manifest = _manifest(
        "saved_reports.delete", "Delete saved reports found by a selector (soft delete, can be undone).",
        {"selector": "conversation | text_match | all | last"}, "destructive",
        idempotent=True, reversible=True, inverse="saved_reports.undo_delete", intents=("delete_reports",),
        finder="saved_reports.find",
    )

    def run(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        ids = list(args.get("ids") or ())

        def make() -> ToolResult:
            mine = {r.report_id: r for r in self._o._own(ctx)}
            hit = [mine[i] for i in ids if i in mine]  # the owner check again, whatever the caller passed
            if not hit:
                return ToolResult(NO_MATCH)
            batch = _Batch("b-" + secrets.token_hex(4), ctx.user.user_id, self._o._now())
            self._o._store.batches[batch.batch_id] = batch
            for report in hit:
                report.deleted_at, report.batch = batch.created_at, batch.batch_id
            plural = "report" if len(hit) == 1 else "reports"
            return ToolResult(f"Deleted {len(hit)} {plural}. Say 'undo' within {self._o._undo.days} days to bring "
                              f"{'it' if len(hit) == 1 else 'them'} back.", count=len(hit))

        return self._o._once(ctx, make)


class _Undo(_Base):
    manifest = _manifest(
        "saved_reports.undo_delete", "Bring back the reports of the latest delete.", {}, "write",
        idempotent=True, reversible=True, inverse="saved_reports.delete", intents=("undo_delete",),
    )

    def run(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        mine = [b for b in self._o._store.batches.values() if b.owner == ctx.user.user_id]
        if not mine:
            return ToolResult("There is no delete to undo.")
        pending = [b for b in mine if not b.undone]
        if not pending:
            return ToolResult("That delete was already undone.")
        latest = pending[-1]  # the batch made last that is not undone: insertion order, so two deletes in the same instant are not a tie
        if self._o._now() - latest.created_at > self._o._undo:
            return ToolResult("That delete can no longer be undone.")
        restored = [r for r in self._o._store.reports.values() if r.batch == latest.batch_id]
        for report in restored:
            report.deleted_at, report.batch = None, None
        latest.undone = True
        plural = "report" if len(restored) == 1 else "reports"
        return ToolResult(f"Restored {len(restored)} {plural}.", count=len(restored))
