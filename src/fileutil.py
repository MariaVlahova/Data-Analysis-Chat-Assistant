"""Small file helpers."""
from __future__ import annotations

import os
import secrets
from pathlib import Path


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """`atomic_write_text` for a file that must be kept byte for byte (a JSON-lines file with a line that is not valid UTF-8).
    Same promises: no half files, a temp file of its own, nothing left behind when the write or the replace fails."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        temp.write_bytes(data)
        os.replace(temp, path)
    except BaseException:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass  # the original error is the one to report
        raise


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
