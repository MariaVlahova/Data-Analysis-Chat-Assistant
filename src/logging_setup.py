"""File logging for the app. The console shows no log lines unless `--debug`.

Handlers and the level go on the application logger `src`, never on the root logger, so other
libraries (httpx, google-auth, langgraph) stay out of `app.log` and its future PII filter.
Plan 02 adds the masking filter to this logger.
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from src.config import Settings

APP_LOGGER = "src"
_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_OWNED = "_opsfleet_handler"  # marks handlers this module added, so setup is repeatable


def setup_logging(settings: Settings, debug: bool = False) -> None:
    shutdown_logging()
    logger = logging.getLogger(APP_LOGGER)
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    logger.propagate = False  # nothing from `src` reaches the root logger or the last-resort stderr handler

    handlers: list[logging.Handler] = []
    try:
        settings.log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(settings.log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"))
    except OSError:
        pass  # logging must never stop the app
    if debug:
        handlers.append(logging.StreamHandler(sys.stderr))
    if not handlers:
        handlers.append(logging.NullHandler())  # a file we cannot open must not turn into console noise

    for handler in handlers:
        handler.setFormatter(logging.Formatter(_FORMAT))
        setattr(handler, _OWNED, True)
        logger.addHandler(handler)


def shutdown_logging() -> None:
    """Remove and close the handlers added by `setup_logging` (releases the log file) and reset the logger."""
    logger = logging.getLogger(APP_LOGGER)
    for handler in list(logger.handlers):
        if getattr(handler, _OWNED, False):
            logger.removeHandler(handler)
            handler.close()
    logger.setLevel(logging.NOTSET)
    logger.propagate = True
