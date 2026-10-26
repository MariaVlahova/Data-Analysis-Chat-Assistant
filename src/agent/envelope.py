"""The answer envelope (plan 07, F6, F32, F33, Q35): the code renders what must be said, so it does not depend on the model.

Around the Composer's body, in this order:
  1. "How I read your question": the plan's assumptions and each sub-question's interpretation, with the resolved date range and
     a note when the data ends earlier than today (F6, A5).
  2. The scope line ("your brands: ..." or "all brands").
  3. The body.
  3b. For a simple turn with one result, the figures of that result as a table the code builds (plan 12, D1).
  4. Notes: every notice of the Result Guard ("3 groups with fewer than 10 customers were hidden", "Showing top 50 of 1,240
     rows"), the status of every sub-question that is not ok, and a "limited data" label when the answer is partial (Q35).

The Composer may leave any of these out. They are in the answer anyway.

Also here: the code-built answer for when the Composer cannot run (the model is down, the budget or time is used up, or the
numeric check left nothing). It shows the guarded figures as a table and says so: degraded, but still true and still guarded.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from src.agent.numeric_check import NUMERIC_NOTE, Ground, check_lines
from src.agent.state import ANSWERABLE, BLOCKED, EMPTY, FAILED, GAP_STATUSES, OK, SKIPPED, TOO_EXPENSIVE, TOO_SLOW, TurnState
from src.errors import ErrorKind, default_message
from src.textutil import squash

LIMITED_DATA_NOTE = "Limited data: this answer rests on only part of the information I tried to get."
DEGRADED_INTRO = "I could not write a full explanation this time, so here are the figures I found, straight from the data."
FALLBACK_ROWS = 10  # rows of a result shown in the code-built answer
MAX_TABLE_CELL = 60
READING_HEADING = "**How I read your question**"
SCOPE_PREFIX = "*Scope: "
NOTES_HEADING = "**Notes**"


def _unique(lines: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(line.strip() for line in lines if line and line.strip()))


def reading_lines(state: TurnState, ground: Ground | None = None) -> list[str]:
    """"How I read your question", as bullet texts. The lines are mostly the model's (the Planner's assumptions, the SQL writer's
    reading), so with a `ground` the ones that state a figure nothing backs are left out (`numeric_check.check_lines`)."""
    plan = state.get("plan", {})
    lines = list(plan.get("assumptions", []))
    results = state.get("results", {})
    for sub in plan.get("subquestions", []):
        interpretation = results.get(sub["id"], {}).get("interpretation")
        if interpretation:
            lines.append(interpretation)
    date_range = plan.get("date_range")
    if date_range and date_range.get("start") and date_range.get("end"):
        label = f" ({date_range['label']})" if date_range.get("label") else ""
        lines.append(f"Period: {date_range['start']} to {date_range['end']}{label}.")
    note = state.get("context", {}).get("freshness_note")
    if note:
        lines.append(note)
    return _unique(lines if ground is None else check_lines(lines, ground))


def failure_reason(kind: str | None) -> str:
    """The standard message of an error kind, for a result that stored no message of its own."""
    try:
        return default_message(ErrorKind(kind)) if kind else default_message(ErrorKind.INTERNAL)
    except ValueError:
        return default_message(ErrorKind.INTERNAL)


def _all_hidden(result: Mapping[str, Any]) -> bool:
    """The rows exist and the Result Guard hid them: every group was too small to show (S7)."""
    return bool((result.get("guarded") or {}).get("suppressed_groups"))


def gap_line(sub: Mapping[str, Any], result: Mapping[str, Any], *, widened: bool = False) -> str | None:
    """One sentence for a sub-question that has no data to show, in plain words. None for a result with data.

    `widened` is true when the loop ran a second, wider query because the first found nothing."""
    status = result.get("status", SKIPPED)
    text = sub.get("text", "")
    if status == OK:
        return None
    if status == EMPTY:
        if _all_hidden(result):  # not "no data": there is data, and it cannot be shown
            return f"I can't show \"{text}\": every group was too small to show without identifying customers."
        return f"No data was found for \"{text}\"" + (", even after widening the search." if widened else ".")
    if status == BLOCKED:
        return f"I did not run \"{text}\". {result.get('notice') or default_message(ErrorKind.GUARD_VIOLATION)}"
    if status == TOO_EXPENSIVE:
        return f"I did not get \"{text}\". {default_message(ErrorKind.QUERY_TOO_EXPENSIVE)}"
    if status == TOO_SLOW:
        return f"I did not get \"{text}\". {default_message(ErrorKind.QUERY_TIMEOUT)}"
    if status == FAILED:
        # The message the error carried: a permanent setup problem must not read as "try again in a minute".
        return f"I could not get \"{text}\". {result.get('error_message') or failure_reason(result.get('error_kind'))}"
    return f"I did not get to \"{text}\" within the limits for one question."


def notes_lines(state: TurnState, *, numeric_removed: bool = False, partial: bool = False, ground: Ground | None = None) -> list[str]:
    plan = state.get("plan", {})
    results = state.get("results", {})
    lines: list[str] = []
    for sub in plan.get("subquestions", []):
        result = results.get(sub["id"], {})
        guarded = result.get("guarded")
        if guarded:
            lines.extend(guarded.get("notices", []))
    lines.extend(plan.get("notices", []))
    attempts = state.get("attempts", {})
    for sub in plan.get("subquestions", []):
        result = results.get(sub["id"], {})
        widened = attempts.get(sub["id"], {}).get("broaden_ran", 0) > 0  # asked for is not done: the writer may have failed
        gap = gap_line(sub, result, widened=widened)
        if gap:
            lines.append(gap)
        elif widened and result.get("status") == OK:  # the code knows the question was widened: it does not leave that to the model
            lines.append(f"My first query for \"{sub.get('text', '')}\" found no data, so I widened the search. The figures are for the wider search.")
    missing = state.get("evidence", {}).get("missing") or []
    if ground is not None:
        missing = check_lines(missing, ground)  # the Evidence Check's phrases are model text too
    if missing and not state.get("evidence", {}).get("enough", True):
        lines.append("I could not get all the evidence I wanted: " + "; ".join(missing) + ".")
    if numeric_removed:
        lines.append(NUMERIC_NOTE)
    if partial:
        lines.append(LIMITED_DATA_NOTE)
    return _unique(lines)


def is_partial(state: TurnState) -> bool:
    """A gap the user should know about: a sub-question with no data because of a refusal or a failure, or evidence that was
    not enough. An honest "no data" is an answer, not a gap."""
    results = state.get("results", {})
    if any(result.get("status") in GAP_STATUSES or (result.get("status") == EMPTY and _all_hidden(result)) for result in results.values()):
        return True
    evidence = state.get("evidence", {})
    return bool(evidence) and not evidence.get("enough", True)


def render_answer(
    state: TurnState, body: str, *, title: str | None = None, numeric_removed: bool = False, degraded: bool = False,
    ground: Ground | None = None, table: str = "",
) -> tuple[str, list[str]]:
    """The final text and the notes in it. The text is not stored anywhere until the Output Guard has passed it. With a `ground`,
    the lines the model wrote around the body (the reading, the missing evidence) get the numeric check too. `table` is the code-built
    table of a simple turn (`result_table`): it comes from guarded rows, so the numeric check does not hold it, and the Output Guard
    checks it with the rest of the text."""
    partial = degraded or numeric_removed or is_partial(state)  # a figure left out is a gap too (Q35)
    reading = reading_lines(state, ground)
    notes = notes_lines(state, numeric_removed=numeric_removed, partial=partial, ground=ground)

    blocks: list[str] = []
    if reading:
        blocks.append(READING_HEADING + "\n" + "\n".join(f"- {line}" for line in reading))
    scope = state.get("scope_label")
    if scope:
        blocks.append(f"{SCOPE_PREFIX}{scope}*")
    if title and state.get("turn_kind") == "report":
        blocks.append(f"## {title}")
    blocks.append(body.strip())
    if table:
        blocks.append(table)
    if notes:
        blocks.append(NOTES_HEADING + "\n" + "\n".join(f"- {line}" for line in notes))
    return "\n\n".join(block for block in blocks if block), notes


def answer_body(text: str) -> str:
    """A rendered answer without its envelope: the reading and the scope line in front, the notes behind. The history keeps this,
    because it has little room and the follow-up question needs what the answer said, not the boilerplate around it."""
    blocks = text.split("\n\n")
    while len(blocks) > 1 and blocks[0].startswith((READING_HEADING, SCOPE_PREFIX)):
        blocks.pop(0)
    if len(blocks) > 1 and blocks[-1].startswith(NOTES_HEADING):
        blocks.pop()
    return "\n\n".join(blocks)


# the code-built answer ---------------------------------------------------------------------------------------------

def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return f"{value:,}" if abs(value) >= 10_000 else str(value)  # a year such as 2026 keeps no separator
    if isinstance(value, float):
        return f"{value:,.2f}" if abs(value) >= 10_000 else f"{value:.2f}".rstrip("0").rstrip(".")
    text = squash(value).replace("|", "/")
    return text if len(text) <= MAX_TABLE_CELL else text[: MAX_TABLE_CELL - 1] + "…"


def render_table(guarded: Mapping[str, Any], limit: int = FALLBACK_ROWS, *, more_note: bool = False) -> str:
    """A result as a markdown table of at most `limit` rows. The footer says how many rows the query had (the default), or with `more_note`
    how many of the guarded rows are left out of the table (the Result Guard's own notice already says when it capped the result)."""
    columns = list(guarded.get("columns", []))
    available = guarded.get("rows", [])
    rows = available[:limit]
    if not columns or not rows:
        return ""
    lines = ["| " + " | ".join(_cell(column) for column in columns) + " |", "|" + "---|" * len(columns)]
    lines += ["| " + " | ".join(_cell(row.get(column)) for column in columns) + " |" for row in rows]
    shown, total = len(rows), guarded.get("total_rows", len(rows))
    if more_note:
        if len(available) > shown:
            lines.append(f"\n_{len(available) - shown:,} more rows are not shown._")
    elif total > shown:
        lines.append(f"\n_Showing {shown:,} of {total:,} rows._")
    return "\n".join(lines)


def result_table(state: TurnState, limit: int) -> str:
    """The figures of a simple turn as a table, built by code from the guarded rows (plan 12, D1). Empty for any other turn and for a
    result that is not `ok`. Why and report turns keep their figures in the text: several tables would bury the answer."""
    if state.get("turn_kind") != "simple":
        return ""
    subquestions = state.get("plan", {}).get("subquestions", [])
    if len(subquestions) != 1:
        return ""
    result = state.get("results", {}).get(subquestions[0]["id"], {})
    if result.get("status") != OK or not result.get("guarded"):
        return ""
    return render_table(result["guarded"], limit, more_note=True)


def fallback_body(state: TurnState) -> str | None:
    """The guarded figures as tables, with no model involved. None when no sub-question has anything to show."""
    plan = state.get("plan", {})
    results = state.get("results", {})
    blocks = []
    for sub in plan.get("subquestions", []):
        result = results.get(sub["id"], {})
        if result.get("status") != OK or not result.get("guarded"):
            continue
        table = render_table(result["guarded"])
        if table:
            blocks.append(f"**{sub['text']}**\n\n{table}")
    if not blocks:
        return None
    return DEGRADED_INTRO + "\n\n" + "\n\n".join(blocks)


def has_answerable_result(state: TurnState) -> bool:
    return any(result.get("status") in ANSWERABLE for result in state.get("results", {}).values())
