"""Show a stored turn (`/trace`) and the metrics totals (`/metrics`) with Rich (F43).

Everything user-derived goes through `Text`, never markup, so a stored string like "[/oops]" cannot
crash the screen. Functions take a Rich `Console`, so the chat and the plain `trace` and `metrics`
commands share them.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from src.access.users import UserContext
from src.config import Settings
from src.observability import alerts
from src.observability.exchange import load_exchange
from src.observability.metrics import MetricsSummary, read_runs, summarize
from src.observability.tracing import exchange_file, trace_file, valid_trace_id

SUMMARY_ITEMS = 4  # scalar attributes shown on a span line
SUMMARY_VALUE_CHARS = 60
QUESTION_CHARS = 200
# Shown elsewhere in the line, or too long for it.
_NOT_IN_SUMMARY = frozenset({
    "model", "tokens_in", "tokens_out", "error_type", "error_detail", "user_hash", "conversation_id",
    "turn_id", "question", "persona_version", "prompt_id", "prompt_hash", "prompt_chars", "prompt_version",
    "prompt_text", "chaos",
})


# reading ---------------------------------------------------------------------------------------

def load_trace(traces_dir: Path, trace_id: str) -> list[dict[str, Any]] | None:
    """The spans of one turn in file order, or None for an unknown, invalid or empty trace."""
    path = trace_file(traces_dir, trace_id)  # the only gate between a typed ID and a file path
    if path is None:
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    spans = []
    # "\n" only: str.splitlines() also splits on U+2028, U+2029 and U+0085, which JSON allows unescaped in
    # a string, and that would cut a span (the root, with the owner's user_hash) in two.
    for line in data.decode("utf-8", errors="replace").split("\n"):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("span_id"):
            spans.append(record)
    return spans or None


def last_trace_id(metrics_file: Path, user_hash: str | None = None) -> str | None:
    """The trace ID of the newest turn in `runs.jsonl`, for one user when `user_hash` is given."""
    try:
        rows, _ = read_runs(Path(metrics_file))
    except OSError:
        return None  # an unreadable history is the same as none for "the last turn"
    for row in reversed(rows):
        if (user_hash is None or row.get("user_hash") == user_hash) and valid_trace_id(row.get("trace_id")):
            return row["trace_id"]
    return None


def _can_view(spans: list[dict[str, Any]], user: UserContext | None) -> bool:
    """A user sees their own turns. The CEO (all brands) sees every turn. No user means the operator."""
    if user is None or user.scope_mode == "all":
        return True
    root = next((span for span in spans if span.get("parent_id") is None), spans[0])
    return (root.get("attrs") or {}).get("user_hash") == user.user_hash


# the /trace tree -------------------------------------------------------------------------------

def _fmt_ms(ms: Any) -> str:
    if not isinstance(ms, (int, float)):
        return "? ms"
    return f"{ms:.0f} ms" if ms < 1000 else f"{ms / 1000:.2f} s"


def _short(value: Any, limit: int) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _summary(attrs: dict[str, Any]) -> str:
    if attrs.get("summary"):
        return _short(attrs["summary"], 120)
    shown = []
    for key, value in attrs.items():
        if key in _NOT_IN_SUMMARY or value is None or isinstance(value, (dict, list)):
            continue
        shown.append(f"{key}={_short(value, SUMMARY_VALUE_CHARS)}")
        if len(shown) == SUMMARY_ITEMS:
            break
    return ", ".join(shown)


def _span_label(span: dict[str, Any]) -> Text:
    attrs = span.get("attrs") or {}
    failed = span.get("status") == "error"
    label = Text()
    label.append(str(span.get("name", "?")), style="bold red" if failed else "bold")
    if span.get("parent_id") is None and attrs.get("turn_id"):
        label.append(f" {attrs['turn_id']}", style="bold")
    label.append(f"  {_fmt_ms(span.get('duration_ms'))}", style="cyan")
    if attrs.get("model"):
        label.append(f"  {attrs['model']}", style="magenta")
    if attrs.get("tokens_in") is not None or attrs.get("tokens_out") is not None:
        label.append(f"  tokens {attrs.get('tokens_in', 0)} in, {attrs.get('tokens_out', 0)} out", style="magenta")
    if failed:
        label.append(f"  ERROR {span.get('error_kind') or ''}".rstrip(), style="bold red")
    summary = _summary(attrs)
    if summary:
        label.append(f"  {summary}", style="dim")
    return label


_GUARD_STYLE = {"pass": "green", "rewrite": "yellow", "block": "bold red"}


def _event_label(item: dict[str, Any]) -> Text:
    attrs = item.get("attrs") or {}
    if item.get("name") == "guard.decision":
        outcome = str(attrs.get("outcome", "?"))
        label = Text(f"guard {attrs.get('guard', '?')}: ", style="dim")
        label.append(outcome, style=_GUARD_STYLE.get(outcome, ""))
        if attrs.get("rule"):
            label.append(f"  rule {attrs['rule']}", style="dim")
        return label
    label = Text(str(item.get("name", "event")), style="dim")
    summary = _summary(attrs)
    if summary:
        label.append(f"  {summary}", style="dim")
    return label


def _error_label(span: dict[str, Any]) -> Text | None:
    attrs = span.get("attrs") or {}
    if span.get("status") != "error" or not (attrs.get("error_type") or attrs.get("error_detail")):
        return None  # a parent of the failing step: its own line is already red
    label = Text(f"{span.get('error_kind') or 'error'}", style="bold red")
    if attrs.get("error_type"):
        label.append(f" ({attrs['error_type']})", style="red")
    if attrs.get("error_detail"):
        label.append(f": {attrs['error_detail']}", style="red")
    if attrs.get("rule"):
        label.append(f"  rule {attrs['rule']}", style="red")
    return label


def build_tree(spans: list[dict[str, Any]]) -> Tree:
    """The turn as a tree: steps, model calls, queries, guard decisions inline, errors in red."""
    ids = {span["span_id"] for span in spans}
    root = next((span for span in spans if span.get("parent_id") is None), spans[0])
    children: dict[str, list[tuple[str, int, str, dict[str, Any]]]] = {}

    def add(parent_id: str, ts: Any, order: int, kind: str, item: dict[str, Any]) -> None:
        children.setdefault(parent_id, []).append((str(ts or ""), order, kind, item))

    order = 0
    for span in spans:
        order += 1
        parent = span.get("parent_id")
        if span is not root:
            add(parent if parent in ids else root["span_id"], span.get("start_ts"), order, "span", span)  # orphan: under the root
        for item in span.get("events") or []:
            order += 1
            add(span["span_id"], item.get("ts"), order, "event", item)

    def fill(node: Tree, span: dict[str, Any]) -> None:
        error = _error_label(span)
        if error is not None:
            node.add(error)
        for _, _, kind, item in sorted(children.get(span["span_id"], []), key=lambda entry: entry[:2]):
            if kind == "event":
                node.add(_event_label(item))
            else:
                fill(node.add(_span_label(item)), item)

    tree = Tree(Text.assemble(("Trace ", "bold"), (str(root.get("trace_id", "?")), "bold cyan"), "  ", _span_label(root)))
    question = (root.get("attrs") or {}).get("question")
    if question:
        tree.add(Text(f"question: {_short(question, QUESTION_CHARS)}", style="dim"))
    fill(tree, root)
    return tree


def show_trace(
    console: Console,
    settings: Settings,
    trace_id: str | None = None,
    *,
    raw: bool = False,
    user: UserContext | None = None,
    default_id: str | None = None,
) -> bool:
    """Print one turn. Returns False when there was nothing to show (and a clear message was printed).

    With `user` set, only that user's turns are visible (the CEO sees all): the same message as for an
    unknown ID, so an ID cannot be probed. No `user` is the operator command.
    """
    wanted = trace_id or default_id or last_trace_id(settings.metrics_path, user.user_hash if user else None)
    if wanted is None:
        console.print(Text("No turns recorded yet. Ask a question first, then look at its trace."))
        return False
    spans = load_trace(settings.traces_path, wanted)
    if spans is None or not _can_view(spans, user):
        console.print(Text(
            f"No trace found for '{_short(wanted, 40)}'. A trace ID has 8 characters and is shown with every error. "
            f"Traces are kept {settings.retention.trace_days} days."
        ))
        return False
    if raw:
        for span in spans:
            console.print(Text(json.dumps(span, ensure_ascii=False)), soft_wrap=True)
    else:
        console.print(build_tree(spans))
    return True


# the exchange of a turn and the turns of a conversation (plan 12, B2 and B3, R7c) ---------------------------------

NO_EXCHANGE = (
    "No message exchange was stored for this turn. The chat keeps it only when it was started with --debug-prompts "
    "(python -m src.cli.main chat --user NAME --debug-prompts)."
)


def _block(console: Console, label: str, text: Any) -> None:
    """A labelled block of stored text, indented, as plain text: a stored string like "[/oops]" cannot crash the screen."""
    console.print(Text(f"    {label}", style="bold dim"))
    for line in str(text).split("\n"):
        console.print(Text("      " + line, style="dim"), soft_wrap=True)


def show_exchange(
    console: Console, settings: Settings, trace_id: str | None = None, *, user: UserContext | None = None, default_id: str | None = None,
) -> bool:
    """Print what was sent to the model and what came back, attempt by attempt, then the answer the user saw. Returns False when there was
    nothing to show. The rules of `show_trace` apply: with `user` set, only that user's turns (the CEO sees all), the same message for a turn
    that is not theirs as for one that does not exist. The contents hold no result row (a line names each result instead), and the
    Composer's draft is not there: the answer is, if the Output Guard passed it."""
    wanted = trace_id or default_id or last_trace_id(settings.metrics_path, user.user_hash if user else None)
    if wanted is None:
        console.print(Text("No turns recorded yet. Ask a question first, then look at its exchange."))
        return False
    spans = load_trace(settings.traces_path, wanted)
    if spans is None or not _can_view(spans, user):  # the trace carries the owner: without it nothing is shown
        console.print(Text(
            f"No trace found for '{_short(wanted, 40)}'. A trace ID has 8 characters and is shown with every error. "
            f"Traces are kept {settings.retention.trace_days} days."
        ))
        return False
    lines = load_exchange(exchange_file(settings.traces_path, wanted))
    if lines is None:
        console.print(Text(NO_EXCHANGE))
        return False
    console.print(Text.assemble(("Exchange ", "bold"), (str(wanted), "bold cyan"), f"  {len(lines)} line(s), in the order they happened"))
    number = 0
    for line in lines:
        if line.get("kind") == "answer":
            console.print(Text("answer  (the text the user saw, after the Output Guard)", style="bold green"))
            _block(console, "text:", line.get("reply") or "")
            continue
        number += 1
        failed = bool(line.get("error_kind"))
        head = Text(f"{number}. {line.get('step', '?')}", style="bold red" if failed else "bold")
        head.append(f"  {line.get('model', '?')}  attempt {line.get('attempt', '?')}  span {line.get('span_id', '?')}", style="magenta")
        head.append(f"  tokens {line.get('tokens_in', 0)} in, {line.get('tokens_out', 0)} out", style="magenta")
        if failed:
            head.append(f"  ERROR {line['error_kind']}", style="bold red")
        console.print(head)
        _block(console, "system:", line.get("system") or "")
        _block(console, "sent:", line.get("contents") or "")
        _block(console, "reply:", line.get("reply") if line.get("reply") is not None else "(none: the attempt failed before a reply)")
    return True


def show_conversation(
    console: Console, settings: Settings, conversation_id: str, *, user: UserContext | None = None, default_id: str | None = None,
) -> bool:
    """The turns of one conversation, from `runs.jsonl`, with the question of each (masked) from its trace. A reader goes from here to a turn
    (`trace ID`) and from the turn to its exchange (`trace ID --exchange`). With `user` set, a brand manager sees their own turns only."""
    wanted = conversation_id or default_id
    try:
        rows, _ = read_runs(settings.metrics_path)
    except OSError as exc:
        console.print(Text(f"The metrics file could not be read ({type(exc).__name__}). Close any program that has it open."))
        return False
    scoped_hash = user.user_hash if user is not None and user.scope_mode != "all" else None
    mine = [
        row for row in rows
        if row.get("conversation_id") == wanted and (scoped_hash is None or row.get("user_hash") == scoped_hash) and valid_trace_id(row.get("trace_id"))
    ]
    if not mine:
        console.print(Text(f"No turns found for conversation '{_short(wanted or '', 40)}'."))
        return False
    table = Table(title=Text(f"Conversation {_short(wanted, 40)}: {len(mine)} turn(s)"), show_lines=False)
    for column in ("turn", "trace ID", "time (UTC)", "intent", "outcome", "question"):
        table.add_column(column)
    for row in mine:
        spans = load_trace(settings.traces_path, row["trace_id"])
        root = next((span for span in spans if span.get("parent_id") is None), None) if spans else None
        question = (root.get("attrs") or {}).get("question") if root else None
        table.add_row(
            Text(str(row.get("turn_id", "?"))), Text(str(row["trace_id"]), style="cyan"), Text(str(row.get("ts", "?"))[:19].replace("T", " ")),
            Text(str(row.get("intent") or "-")), Text(str(row.get("outcome") or "?")),
            Text(_short(question, QUESTION_CHARS) if question else "(trace deleted)", style="dim"),
        )
    console.print(table)
    return True


# the metrics summary ---------------------------------------------------------------------------

def fmt_bytes(count: int) -> str:
    size = float(count)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.2f} TiB"


def _pct(rate: float) -> str:
    return f"{rate * 100:.1f}%"


def render_summary(console: Console, summary: MetricsSummary) -> None:
    alert = alerts.summary_alert(summary)
    if alert:
        console.print(Text(alert, style="bold red"))
    window = f"{summary.turns} turn(s)"
    if summary.turns != summary.total_rows:
        window = f"last {summary.turns} of {summary.total_rows} turns"
    console.print(Text(f"Metrics: {window}, {'your turns only' if summary.user_scoped else 'all users'}", style="bold"))

    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold")
    grid.add_column()

    def row(label: str, value: str, style: str = "") -> None:
        grid.add_row(Text(label), Text(value, style=style))

    outcomes = ", ".join(f"{name} {count}" for name, count in sorted(summary.outcomes.items()))
    row("Outcomes", outcomes or "none")
    row("Success rate", _pct(summary.success_rate))
    row("Refusal rate", _pct(summary.refusal_rate))
    row("Failure rate", _pct(summary.failure_rate), "red" if summary.failure_rate else "")
    row("Guard blocks", ", ".join(f"{guard} {count}" for guard, count in summary.guard_blocks.items()))
    row("PII at output", str(summary.pii_at_output_total), "bold red" if summary.pii_at_output_total else "green")
    if summary.latency_p50_ms is not None:
        row("Latency", f"p50 {_fmt_ms(summary.latency_p50_ms)}, p95 {_fmt_ms(summary.latency_p95_ms)}")
    row("Model calls per turn", f"{summary.avg_llm_calls:.2f}")
    row("Tokens", f"{summary.tokens_in} in, {summary.tokens_out} out")
    row("Top errors", ", ".join(f"{kind} x{count}" for kind, count in summary.top_errors) or "none")
    if summary.cost_usd is not None:
        note = f" ({summary.unpriced_turns} turn(s) without a price left out)" if summary.unpriced_turns else ""
        row("Estimated cost", f"${summary.cost_usd:.4f}{note}")
    else:
        row("Estimated cost", "not set (fill llm.pricing_usd_per_1m in config.yaml)" if summary.unpriced_turns else "n/a")
    row(
        f"BigQuery {summary.month}",
        f"{fmt_bytes(summary.bytes_billed_month)} billed by all users ({_pct(summary.free_tier_fraction)} of the 1 TiB free tier)",
        "bold yellow" if summary.free_tier_warning else "",
    )
    console.print(grid)
    if summary.free_tier_warning:
        console.print(Text(
            f"WARNING: {_pct(summary.free_tier_fraction)} of this month's free BigQuery tier is used (warning at 80%).",
            style="bold yellow",
        ))
    if summary.skipped_lines:
        console.print(Text(f"{summary.skipped_lines} unreadable line(s) in the metrics file were skipped.", style="dim"))


def show_metrics(
    console: Console, settings: Settings, last: int | None = None, *, user: UserContext | None = None
) -> bool:
    """Print the totals. With `user`, a brand manager sees their own turns only (the CEO sees all), the same
    rule as `/trace`: another user's PII-incident trace IDs and error mix are not theirs to read. No `user`
    is the operator command."""
    try:
        rows, skipped = read_runs(settings.metrics_path)
    except OSError as exc:
        console.print(Text(f"The metrics file could not be read ({type(exc).__name__}). Close any program that has it open."))
        return False
    scoped_hash = user.user_hash if user is not None and user.scope_mode != "all" else None
    if scoped_hash is not None:
        mine = [row for row in rows if row.get("user_hash") == scoped_hash]
        if not mine:
            console.print(Text("No turns of yours recorded yet."))
            return False
    elif not rows:
        console.print(Text("No turns recorded yet."))
        return False
    render_summary(console, summarize(rows, last=last, skipped_lines=skipped, user_hash=scoped_hash))
    return True
