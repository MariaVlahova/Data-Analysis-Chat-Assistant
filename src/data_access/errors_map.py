"""BigQuery and transport exceptions become typed `AgentError`s in one place (plan 03, decision 3.11, S6).

The SQL loop (plan 07) never inspects a raw message: it reads `AgentError.kind`, `.retryable` and `.repairable`.

Text rules (S6). A BigQuery message can quote SQL literals that no pattern masks. So:
- `SQL_SYNTAX` and `SQL_UNKNOWN_COLUMN` keep BigQuery's message in `detail`, because the repair step needs the
  position and the unknown name. It is normalized first, every quoted token is blanked (`"Maria"` becomes `"…"`,
  since a quoted token can be a string literal), then `AgentError` masks it and cuts it to 300 characters. The
  model's own SQL is the only other place the text appears, and the trace stores that masked too.
- Every other kind gets a fixed hint written here plus the HTTP code and BigQuery's reason slug, never the message.
"""
from __future__ import annotations

import http.client
import re
import ssl
from typing import Literal

import requests
from google.api_core import exceptions as api_exc
from google.auth import exceptions as auth_exc

from src.errors import AgentError, ErrorKind
from src.textutil import squash

# Where the call happens, because the same failure means different things:
#   "metadata": a table-metadata call on the configured dataset. A 404 means bigquery.dataset is wrong.
#   "dry_run":  parsing a query. A timeout means BigQuery does not answer, a 404 on a table is a query mistake.
#   "query":    the real job. A timeout means the question is too slow to run (F33).
Phase = Literal["metadata", "dry_run", "query"]

SETUP_MESSAGE = (
    "I can't use the data warehouse because of a setup problem on our side. "
    "Please tell an administrator and share the trace ID."
)

_COST_TEXT = re.compile(r"bytes billed|maximumbytesbilled", re.I)
_UNKNOWN_COLUMN_TEXT = re.compile(r"unrecognized name|not found inside|no matching signature|unknown column", re.I)
_TIMEOUT_TEXT = re.compile(r"timed out|timeout|deadline exceeded", re.I)

# BigQuery job error reasons (a fixed vocabulary). They decide the kind before the HTTP code does, because BigQuery
# reports several job failures that are not the query's fault with a 400: `timeout`, `jobBackendError`,
# `tableUnavailable`. Read as syntax errors they would send the model on a pointless repair.
#   too expensive: the query asks for too much, which no rewrite of the same question fixes
#   timeout:       the job ran past its time limit
#   transient:     a failure on BigQuery's side that goes away by itself
_TOO_EXPENSIVE_REASONS = frozenset({"bytesBilledLimitExceeded", "billingTierLimitExceeded", "resourcesExceeded", "responseTooLarge"})
_TIMEOUT_REASONS = frozenset({"timeout"})
_TRANSIENT_REASONS = frozenset({
    "rateLimitExceeded", "backendError", "internalError", "jobBackendError", "jobInternalError", "tableUnavailable",
    "resourceInUse",
})

_HINTS = {
    401: "authentication failed: run `gcloud auth application-default login`",
    403: "access denied: check GOOGLE_CLOUD_PROJECT, that the BigQuery API is enabled and that the account can run query jobs",
    404: "dataset or table not found: check bigquery.dataset in config.yaml",
}
_QUOTA_HINT = "quota exceeded: the free query tier for this month may be used up"
_CREDENTIALS_HINT = "no usable Google credentials: run `gcloud auth application-default login`"


def normalize_message(message: str) -> str:
    """The part of a BigQuery message the SQL repair step needs: no job IDs, locations or URLs, one line,
    and every quoted token blanked (it may be a string literal from the question)."""
    text = message.split("; reason:")[0]  # the client library repeats the API reason and message after this
    text = re.sub(r"\(?\bjob ?ID:?\s*\S+\)?", " ", text, flags=re.I)
    text = re.sub(r"\bLocation:\s*\S+", " ", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r'"[^"]*"', '"…"', text)
    text = re.sub(r"'[^']*'", "'…'", text)
    return squash(text)


def _reason(exc: api_exc.GoogleAPICallError) -> str | None:
    """BigQuery's reason slug (for example `rateLimitExceeded`), kept only if it looks like one."""
    for item in getattr(exc, "errors", None) or ():
        reason = item.get("reason") if isinstance(item, dict) else getattr(item, "reason", None)
        if isinstance(reason, str) and re.fullmatch(r"[A-Za-z]{1,40}", reason):
            return reason
    return None


def _unavailable(detail: str, *, retryable: bool) -> AgentError:
    if retryable:
        return AgentError(ErrorKind.BQ_UNAVAILABLE, detail=detail, retryable=True)
    return AgentError(ErrorKind.BQ_UNAVAILABLE, SETUP_MESSAGE, detail=detail, retryable=False)


def _too_expensive(detail: str) -> AgentError:
    return AgentError(ErrorKind.QUERY_TOO_EXPENSIVE, detail=detail)


def _timeout(detail: str) -> AgentError:
    return AgentError(ErrorKind.QUERY_TIMEOUT, detail=detail)


def setup_error(exc: BaseException) -> AgentError:
    """A failure to create the BigQuery client at all: no credentials, no project, a broken key file.

    Only the exception class goes in the detail. The usual cause is a missing project, so the hint names it.
    """
    return _unavailable(
        f"could not create the BigQuery client ({type(exc).__name__}): sign in with `gcloud auth application-default login` "
        "and set GOOGLE_CLOUD_PROJECT in .env",
        retryable=False,
    )


def _from_api_error(exc: api_exc.GoogleAPICallError, phase: Phase) -> AgentError:
    code = getattr(exc, "code", None) or 0
    reason = _reason(exc)
    message = normalize_message(str(getattr(exc, "message", "") or ""))
    where = f"HTTP {code}" + (f" {reason}" if reason else "")

    if reason in _TOO_EXPENSIVE_REASONS or _COST_TEXT.search(message):
        return _too_expensive(f"{where}: BigQuery refused the query as too expensive")
    if reason in _TIMEOUT_REASONS:
        if phase == "query":
            return _timeout(f"{where}: the job ran past its time limit")
        return _unavailable(f"{where}: BigQuery is not answering", retryable=True)
    if reason in _TRANSIENT_REASONS:
        return _unavailable(f"{where}: BigQuery reported a temporary failure", retryable=True)
    if code == 400:
        # BigQuery rejected the query text itself. The model can fix that, so it is repairable.
        if _UNKNOWN_COLUMN_TEXT.search(message):
            return AgentError(ErrorKind.SQL_UNKNOWN_COLUMN, detail=message)
        return AgentError(ErrorKind.SQL_SYNTAX, detail=message)
    if code == 404 and phase != "metadata":
        # Startup reads the configured dataset's tables in the "metadata" phase, so a dataset that is not there stops
        # it with the setup hint. A 404 on a query that gets this far is the model naming a table or dataset that
        # does not exist, which the repair step can fix.
        return AgentError(ErrorKind.SQL_UNKNOWN_COLUMN, detail=message)
    if code in (401, 403, 404):
        hint = _QUOTA_HINT if reason == "quotaExceeded" else _HINTS[code]
        return _unavailable(f"{where}: {hint}", retryable=False)
    if code == 429 or code >= 500:
        if phase == "query" and _TIMEOUT_TEXT.search(message):  # a job that BigQuery stopped for running too long
            return _timeout(f"{where}: the job ran past its time limit")
        return _unavailable(f"{where}: BigQuery is not answering", retryable=True)
    return AgentError(ErrorKind.INTERNAL, detail=f"{where}: unexpected BigQuery reply")


def classify(exc: BaseException, phase: Phase = "query") -> AgentError | None:
    """The `AgentError` for a BigQuery or transport failure, or None when the exception is not one of those.

    None means "not ours to classify": the caller re-raises the original, which the turn boundary reports as
    INTERNAL with the exception class and call sites only.
    """
    if isinstance(exc, AgentError):
        return exc
    if isinstance(exc, api_exc.GoogleAPICallError):
        return _from_api_error(exc, phase)
    if isinstance(exc, auth_exc.TransportError):  # before GoogleAuthError: a network failure during sign-in is transient
        return _unavailable("connection to BigQuery failed", retryable=True)
    if isinstance(exc, auth_exc.GoogleAuthError):  # no credentials, expired or revoked: signing in again is the fix
        return _unavailable(_CREDENTIALS_HINT, retryable=False)
    if isinstance(exc, requests.exceptions.ConnectTimeout):  # before Timeout: this one is a connection problem
        return _unavailable("connection to BigQuery timed out", retryable=True)
    if isinstance(exc, (TimeoutError, requests.exceptions.Timeout)):  # TimeoutError is also concurrent.futures'
        if phase == "query":
            return _timeout("the query did not finish within the time limit")
        return _unavailable("BigQuery did not answer in time", retryable=True)
    if isinstance(exc, (
        ConnectionError,  # includes ConnectionResetError and BrokenPipeError
        requests.exceptions.RequestException,  # ConnectionError, ChunkedEncodingError, SSLError, ContentDecodingError ...
        http.client.HTTPException,
        ssl.SSLError,
        api_exc.RetryError,
    )):
        return _unavailable("connection to BigQuery failed", retryable=True)
    return None


def job_may_still_run(exc: BaseException) -> bool:
    """True when the failure says nothing about the job itself: it timed out, or the connection or server failed.

    Then the job can still be running on BigQuery and billing, so the caller cancels it before giving up or
    starting a retry. A job that BigQuery reports as failed (a bad query, a quota) has already stopped.
    """
    err = classify(exc, "query")
    return err is not None and (err.kind == ErrorKind.QUERY_TIMEOUT or err.retryable)
