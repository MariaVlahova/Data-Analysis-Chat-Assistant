"""Small file helpers."""
from __future__ import annotations

import os
import secrets
from pathlib import Path


def atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to `path` so that a reader never sees half a file and two writers never share a temp file.

    The temp file has a name of its own, and it is removed if the write or the replace fails (a full disk, a lock
    held by a virus scanner), so a failed write leaves nothing behind. UTF-8, "\\n" line ends.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        temp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(temp, path)
    except BaseException:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass  # the original error is the one to report
        raise
