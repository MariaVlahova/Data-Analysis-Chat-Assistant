"""Every sentence the Saved Reports tools say to the user, written once (plan 08).

The tools return these texts as their result and the graph passes them through the Output Guard like any answer, because a
title is stored text. Nothing here is written by a model.
"""
from __future__ import annotations

from typing import Sequence

MAX_LISTED = 50  # "list my reports" shows this many; the count is always the full one

NOTHING_TO_SAVE = "There is nothing to save yet. Ask a question first, then ask me to save the answer."
NOT_SAVED_PERSONAL = "I did not save this answer because it may contain personal data."
NO_REPORTS = "You have no saved reports."
NO_MATCH = "I found no reports matching that."
NO_DELETE_TO_UNDO = "There is no delete to undo."
ALREADY_UNDONE = "That delete was already undone."
UNDO_EXPIRED = "That delete can no longer be undone."


def _reports(count: int) -> str:
    return "report" if count == 1 else "reports"


def saved(title: str, date: str) -> str:
    return f"Saved \"{title}\" on {date}."


def listing(entries: Sequence[tuple[str, str]]) -> str:
    """"You have 2 saved reports:" and one line per report. `entries` are (title, date)."""
    lines = [f"You have {len(entries)} saved {_reports(len(entries))}:"]
    lines += [f"{number}. {title} ({date})" for number, (title, date) in enumerate(entries[:MAX_LISTED], 1)]
    if len(entries) > MAX_LISTED:
        lines.append(f"... and {len(entries) - MAX_LISTED} more.")
    return "\n".join(lines)


def deleted(count: int, undo_days: int) -> str:
    return f"Deleted {count} {_reports(count)}. Say 'undo' within {undo_days} days to bring {'it' if count == 1 else 'them'} back."


def restored(count: int) -> str:
    return f"Restored {count} {_reports(count)}."
