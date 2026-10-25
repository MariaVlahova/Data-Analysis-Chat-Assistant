"""Read a JSON-lines file the app wrote (`runs.jsonl`, a trace). One reader, so a report and a metric agree on what a damaged file means."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """The objects in the file, in order. A missing file is empty, and a line that is not a JSON object (a last line cut short by a crash) is skipped."""
    rows: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return rows
    for line in text.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows
