"""The log filter and the file handler that `src.logging_setup.setup_logging` attaches (F44, S6).

`bq_client.py` logs `f"... {str(e)}"`, and a BigQuery error can quote a filter value, so a log line
can hold data. The filter masks every record before any handler writes it. It sits on the handlers,
not on the logger: a logger filter would skip records from child loggers such as `src.cli.main`.
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from src import pii

LOG_MAX_BYTES = 2_000_000
LOG_BACKUPS = 4  # the live file plus four old ones: 5 files of 2 MB (impl)
WITHHELD = "[log line withheld: masking failed]"
_MASKED = "_opsfleet_masked"  # attribute set on a record once it has been masked

_plain_formatter = logging.Formatter()


class PIIMaskingFilter(logging.Filter):
    """Rewrite the message, the arguments, the traceback text and the stack text with `pii.mask`.

    Every handler has its own filter instance but they see the same record, so a record is marked once
    masked and the other handlers skip it (the four patterns and the traceback formatter run once per line).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, _MASKED, False):
            return True
        setattr(record, _MASKED, True)  # set first: a record that fails below is withheld, never retried raw
        try:
            record.msg = pii.mask(record.getMessage())  # formats the args first, then masks the result
            record.args = None
            if record.exc_info and record.exc_info[0] is not None:
                record.exc_text = pii.mask(_plain_formatter.formatException(record.exc_info))
            elif record.exc_text:
                record.exc_text = pii.mask(record.exc_text)
            if record.stack_info:
                record.stack_info = pii.mask(record.stack_info)
        except Exception:  # noqa: BLE001 - fail closed: a line we cannot mask is not written
            record.msg, record.args = WITHHELD, None
            record.exc_info = record.exc_text = record.stack_info = None
        return True  # the record is always kept, only its content changes


def make_file_handler(path: Path) -> RotatingFileHandler:
    return RotatingFileHandler(path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8")
