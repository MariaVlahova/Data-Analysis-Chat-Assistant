import logging
from logging.handlers import RotatingFileHandler

import pytest

from src.config import load_settings
from src.logging_setup import APP_LOGGER, setup_logging, shutdown_logging
from src.observability.logging_setup import LOG_BACKUPS, LOG_MAX_BYTES, WITHHELD, PIIMaskingFilter

CANARY = "canary@example.test"


@pytest.fixture
def settings(project):
    return load_settings(project / "config.yaml")


@pytest.fixture
def logged(settings):
    """Set up the app logging, give the test the logger, and always shut it down."""
    setup_logging(settings)
    try:
        yield logging.getLogger("src.demo")
    finally:
        shutdown_logging()


def read_log(settings) -> str:
    shutdown_logging()  # flush and release the file
    return settings.log_path.read_text(encoding="utf-8")


def make_record(msg, args=None, exc_info=None) -> logging.LogRecord:
    return logging.LogRecord("src.demo", logging.ERROR, __file__, 1, msg, args, exc_info)


def test_a_log_line_with_an_email_is_masked_in_the_file(settings, logged):
    logged.error("BigQuery execution failed: no row for %s", CANARY)
    logged.error(f"f-string line with {CANARY} inside")
    text = read_log(settings)
    assert CANARY not in text
    assert text.count("[EMAIL_MASKED]") == 2
    assert "BigQuery execution failed" in text  # only the data is removed, not the line


def test_the_arguments_are_formatted_then_masked(settings, logged):
    logged.warning("user %s called %s", CANARY, "415-555-2671")
    text = read_log(settings)
    assert CANARY not in text and "415-555-2671" not in text
    assert "[EMAIL_MASKED]" in text and "[PHONE_MASKED]" in text


def test_a_traceback_is_masked_too(settings, logged):
    try:
        raise ValueError(f"bad value for {CANARY}")
    except ValueError:
        logged.exception("could not read")
    text = read_log(settings)
    assert "could not read" in text and "ValueError" in text
    assert CANARY not in text and "[EMAIL_MASKED]" in text


def test_records_from_child_loggers_are_masked(settings, project):
    # The filter is on the handlers: a logger filter would skip `src.cli.main` and every other child.
    setup_logging(settings)
    try:
        logging.getLogger("src.cli.main").error("child logger %s", CANARY)
        logging.getLogger("src.data_access.bq_client").error("deep child %s", CANARY)
    finally:
        text = read_log(settings)
    assert CANARY not in text and text.count("[EMAIL_MASKED]") == 2


def test_the_older_bq_client_logs_through_a_masked_logger():
    # S6: it used `logging.error(...)` on the root logger, which skips app.log and prints unmasked to stderr.
    from src.data_access import bq_client

    assert bq_client.log.name == "src.data_access.bq_client"


class _FailingClient:
    """A BigQuery client whose every call fails with an error that quotes SQL literals, as the real one can."""

    def query(self, sql):
        raise RuntimeError("Syntax error near WHERE users.id = 12345 AND name = 'Jane Roe'")

    def get_table(self, ref):
        raise RuntimeError("Not found: Table for customer 'Jane Roe' id 12345")


def test_bigquery_error_text_never_reaches_the_log_because_no_pattern_can_clean_it(settings):
    # Masking catches emails, phones and the like, not a bare ID or a name. So the error lines log the
    # exception class and call sites only, as plan 01 does everywhere else.
    from src.data_access.bq_client import BigQueryRunner

    runner = object.__new__(BigQueryRunner)  # no real client, no credentials
    runner.client, runner.dataset_id = _FailingClient(), "ds"
    setup_logging(settings)
    try:
        with pytest.raises(RuntimeError):
            runner.execute_query("SELECT 1")
        with pytest.raises(RuntimeError):
            runner.get_table_schema("users")
    finally:
        text = read_log(settings)
    assert "BigQuery execution failed: RuntimeError at" in text and "Failed to get schema for table users" in text
    for leak in ("12345", "Jane", "Roe", "Syntax error"):
        assert leak not in text


def test_a_record_is_masked_once_even_when_two_handlers_see_it(monkeypatch):
    from src.observability import logging_setup as module

    calls = []
    real_mask = module.pii.mask
    monkeypatch.setattr(module.pii, "mask", lambda text: calls.append(text) or real_mask(text))
    record = make_record("value %s", (CANARY,), exc_info=None)
    first, second = PIIMaskingFilter(), PIIMaskingFilter()  # one per handler
    first.filter(record)
    after_first = len(calls)
    second.filter(record)
    assert after_first >= 1 and len(calls) == after_first  # the second handler did no masking work
    assert record.getMessage() == "value [EMAIL_MASKED]"


def test_a_line_with_a_lone_surrogate_is_written_not_dropped_with_a_logging_error(settings, capsys):
    setup_logging(settings)
    try:
        logging.getLogger("src.demo").error("pasted text \ud800 and %s", CANARY)
    finally:
        text = read_log(settings)
    assert "pasted text ? and [EMAIL_MASKED]" in text
    assert "Logging error" not in capsys.readouterr().err  # the stdlib prints that when a handler cannot encode


def test_the_debug_console_handler_is_masked_as_well(settings, capsys):
    setup_logging(settings, debug=True)
    try:
        logging.getLogger("src.demo").error("on screen: %s", CANARY)
        handlers = [h for h in logging.getLogger(APP_LOGGER).handlers if getattr(h, "_opsfleet_handler", False)]
        assert len(handlers) == 2  # the file and the debug console, not the library NullHandler
        assert all(any(isinstance(f, PIIMaskingFilter) for f in h.filters) for h in handlers)
    finally:
        shutdown_logging()
    err = capsys.readouterr().err
    assert CANARY not in err and "[EMAIL_MASKED]" in err


def test_the_file_handler_rotates_5_files_of_2_mb(settings):
    setup_logging(settings)
    try:
        handler = next(h for h in logging.getLogger(APP_LOGGER).handlers if isinstance(h, RotatingFileHandler))
        assert handler.maxBytes == LOG_MAX_BYTES == 2_000_000
        assert handler.backupCount == LOG_BACKUPS == 4  # the live file plus 4 old ones
    finally:
        shutdown_logging()


def test_a_line_that_cannot_be_formatted_is_withheld_not_written_raw():
    record = make_record("two placeholders %s %s", ("only one argument",))
    assert PIIMaskingFilter().filter(record) is True
    assert record.getMessage() == WITHHELD
    assert "only one argument" not in record.getMessage()


def test_filtering_twice_changes_nothing():
    record = make_record("value %s", (CANARY,))
    log_filter = PIIMaskingFilter()
    log_filter.filter(record)
    first = record.getMessage()
    log_filter.filter(record)
    assert record.getMessage() == first == "value [EMAIL_MASKED]"
