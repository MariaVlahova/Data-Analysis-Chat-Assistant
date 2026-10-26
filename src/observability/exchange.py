"""The message exchange of a turn (plan 12, B1 to B3, R7c): what was sent to the model, what came back, in order.

A trace says which step ran and how long it took. It does not say what was asked or answered, and a person who debugs a turn needs that
(the brief: "understanding what the message correspondence is and what went wrong"). With `--debug-prompts`, every model attempt adds one
line to `data/traces/<trace_id>.exchange.jsonl`, next to the trace. It is not part of the spans: a span attribute is cut at 2000
characters and never holds rows (plan 02).

What the file holds, and what it never holds:
- The contents sent are the prompt builder's *debug rendering*: the same text, with every guarded result replaced by one line that names it
  (`[rows of q1: 12 rows, columns brand, revenue; not stored]`). The gateway is given that text and never the contents it sent, so a row
  cannot get here by a mistake of a call site. A call that gave none is recorded as having none.
- The reply is stored for every step but the Composer. The Composer's draft follows the Output Guard's rule (plans 06 and 07): the file
  gets the answer only after `output_guard` has passed it (`record_answer`, the text the user saw), and nothing when the guard blocks it.
- Every text passes `pii.mask(..., extended=True)` and is cut at `MAX_TEXT` characters.
- The file is written only when `--debug-prompts` is on, and is removed with its trace by the retention purge.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from src import pii
from src.errors import crash_summary
from src.observability.tracing import current_trace_id, exchange_path

log = logging.getLogger(__name__)

MAX_TEXT = 20_000
DEBUG_CONTENTS = "debug_contents"  # the key of the prompt attributes that carries the safe rendering of the contents to the gateway
NOT_RECORDED = "[not recorded: this call gave no safe rendering of its contents]"
DRAFT_HELD = "[the draft is not stored: the answer follows, once the Output Guard has passed it]"
_LOCK = threading.Lock()


def _text(value: object) -> str | None:
    if value is None:
        return None
    masked = pii.mask(str(value), extended=True)  # mask first: cutting first could leave half an e-mail that no pattern matches
    return masked if len(masked) <= MAX_TEXT else masked[: MAX_TEXT - 1] + "…"


def _append(path: Path, record: dict[str, Any]) -> None:
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False, default=lambda obj: f"<{type(obj).__name__}>")
    with _LOCK, path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line + "\n")


def record_attempt(
    *, step: str, model: str, attempt: int, span_id: str, system: str, contents: str | None, reply: str | None,
    tokens_in: int = 0, tokens_out: int = 0, error_kind: str | None = None, hold_reply: bool = False,
) -> None:
    """One line for one outbound attempt. A no-op unless `--debug-prompts` is on. Never raises: this is a debugging aid, and a full disk must
    not fail a turn. `hold_reply` is for the Composer, whose draft is not written here."""
    try:
        path = exchange_path()
        if path is None:
            return
        _append(path, {
            "trace_id": current_trace_id(), "kind": "attempt", "step": step, "model": model, "attempt": attempt, "span_id": span_id,
            "system": _text(system), "contents": _text(contents) if contents is not None else NOT_RECORDED,
            "reply": DRAFT_HELD if hold_reply else _text(reply),
            "tokens_in": tokens_in, "tokens_out": tokens_out, "error_kind": error_kind,
        })
    except Exception as exc:  # noqa: BLE001 - see above
        log.warning("could not write the exchange file: %s", crash_summary(exc))


def record_answer(text: str) -> None:
    """The answer the user saw, after the Output Guard has passed it. The only place the Composer's words are written. A no-op unless
    `--debug-prompts` is on."""
    try:
        path = exchange_path()
        if path is None:
            return
        _append(path, {"trace_id": current_trace_id(), "kind": "answer", "step": "answer", "reply": _text(text)})
    except Exception as exc:  # noqa: BLE001
        log.warning("could not write the exchange file: %s", crash_summary(exc))


def load_exchange(path: Path | None) -> list[dict[str, Any]] | None:
    """The lines of an exchange file in order, or None when there is no file (or it holds no readable line)."""
    if path is None:
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    lines = []
    for line in data.decode("utf-8", errors="replace").split("\n"):  # "\n" only: U+2028 may sit unescaped inside a JSON string
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("kind") in ("attempt", "answer"):
            lines.append(record)
    return lines or None
