"""Error classification (plan 03, S6): each BigQuery signal maps to the right kind, and no message leaks."""
import http.client
import ssl

import pytest
import requests
from google.api_core import exceptions as api_exc
from google.auth import exceptions as auth_exc

from src.data_access.errors_map import SETUP_MESSAGE, classify, job_may_still_run, normalize_message, setup_error
from src.errors import AgentError, ErrorKind

CANARY = "canary@example.test"


def kind_of(exc, phase="query") -> AgentError:
    err = classify(exc, phase)
    assert err is not None, f"{exc!r} was not classified"
    return err


# the error table in the plan ---------------------------------------------------------------------

@pytest.mark.parametrize("message,kept", [
    ('Syntax error: Unexpected identifier "revenue" at [1:8]', 'Unexpected identifier "…" at [1:8]'),  # the position is what repair needs
    ("Syntax error: Expected end of input but got keyword FROM at [3:1]", "got keyword FROM at [3:1]"),
])
def test_syntax_errors_are_repairable(message, kept):
    err = kind_of(api_exc.BadRequest(message))
    assert err.kind == ErrorKind.SQL_SYNTAX and err.repairable and not err.retryable
    assert kept in err.detail  # the repair step needs the message


@pytest.mark.parametrize("message", [
    "Unrecognized name: sale_prise at [1:8]",
    "Name sale_prise not found inside oi at [1:8]",
    "No matching signature for function SUM for argument types: STRING",
])
def test_unknown_columns_are_repairable(message):
    err = kind_of(api_exc.BadRequest(message))
    assert err.kind == ErrorKind.SQL_UNKNOWN_COLUMN and err.repairable
    assert message.split(" at [")[0] in err.detail


def test_any_other_rejected_query_is_a_repairable_sql_error():
    err = kind_of(api_exc.BadRequest("Division by zero: 1 / 0"))
    assert err.kind == ErrorKind.SQL_SYNTAX and err.repairable


@pytest.mark.parametrize("exc", [
    api_exc.BadRequest("Query exceeded limit for bytes billed: 536870912. 1073741824 or higher required."),
    api_exc.BadRequest("Resources exceeded during query execution", errors=[{"reason": "resourcesExceeded"}]),
    api_exc.Forbidden("Response too large to return", errors=[{"reason": "responseTooLarge"}]),
])
def test_a_query_that_asks_for_too_much_is_too_expensive_and_not_repairable(exc):
    err = kind_of(exc)
    assert err.kind == ErrorKind.QUERY_TOO_EXPENSIVE and not err.repairable and not err.retryable


def test_a_timeout_while_running_the_query_is_a_query_timeout():
    for exc in (TimeoutError(), requests.exceptions.ReadTimeout("slow"), api_exc.InternalServerError("Job timed out after 35000 ms")):
        err = kind_of(exc)
        assert err.kind == ErrorKind.QUERY_TIMEOUT and not err.retryable and not err.repairable
        assert job_may_still_run(exc)


def test_a_timeout_in_a_dry_run_or_metadata_call_means_bigquery_does_not_answer():
    err = kind_of(TimeoutError(), phase="dry_run")
    assert err.kind == ErrorKind.BQ_UNAVAILABLE and err.retryable
    assert kind_of(requests.exceptions.ReadTimeout("slow"), phase="metadata").retryable


@pytest.mark.parametrize("exc,hint", [
    (api_exc.Forbidden("Access Denied: Project x: User does not have bigquery.jobs.create permission"), "access denied"),
    (api_exc.Forbidden("Quota exceeded: free tier", errors=[{"reason": "quotaExceeded"}]), "quota exceeded"),
    (api_exc.NotFound("Not found: Dataset bigquery-public-data:nope"), "bigquery.dataset"),
    (api_exc.Unauthorized("Invalid credentials"), "gcloud auth application-default login"),
    (auth_exc.DefaultCredentialsError("none"), "gcloud auth application-default login"),
    (auth_exc.RefreshError("expired"), "gcloud auth application-default login"),
])
def test_permission_and_missing_data_errors_are_unavailable_with_a_fix_hint_and_no_retry(exc, hint):
    err = kind_of(exc, phase="metadata")
    assert err.kind == ErrorKind.BQ_UNAVAILABLE and not err.retryable
    assert hint in err.detail
    assert err.user_message == SETUP_MESSAGE  # "try again in a minute" would be wrong for a setup problem


@pytest.mark.parametrize("exc", [
    api_exc.ServiceUnavailable("503"),
    api_exc.InternalServerError("500"),
    api_exc.BadGateway("502"),
    api_exc.GatewayTimeout("504"),
    api_exc.TooManyRequests("429"),
    api_exc.Forbidden("Exceeded rate limits", errors=[{"reason": "rateLimitExceeded"}]),
    requests.exceptions.ConnectionError("reset"),
    requests.exceptions.ConnectTimeout("no route"),
    ConnectionResetError(),
    auth_exc.TransportError("dns"),
])
def test_server_and_connection_failures_are_unavailable_and_retryable(exc):
    err = kind_of(exc)
    assert err.kind == ErrorKind.BQ_UNAVAILABLE and err.retryable


def test_things_that_are_not_bigquery_problems_are_not_classified():
    assert classify(ValueError("a bug in our code")) is None
    assert classify(KeyError("x")) is None


def test_an_agent_error_passes_through_unchanged():
    original = AgentError(ErrorKind.GUARD_VIOLATION, rule_id="NOT_SELECT")
    assert classify(original) is original


# S6: masking and cutting -------------------------------------------------------------------------

def test_a_quoted_string_literal_in_a_syntax_error_is_blanked():
    err = kind_of(api_exc.BadRequest(f"Syntax error: Unexpected string literal '{CANARY}' at [1:30]"))
    assert CANARY not in err.detail and CANARY not in str(err)
    assert "Unexpected string literal '…' at [1:30]" in err.detail


def test_a_name_in_a_quoted_identifier_is_blanked_so_a_customer_name_in_the_question_does_not_travel():
    err = kind_of(api_exc.BadRequest('Syntax error: Unexpected identifier "Maria" at [1:30]'))
    assert "Maria" not in err.detail and '"…"' in err.detail


def test_an_unquoted_canary_email_is_still_masked_in_the_agent_error():
    err = kind_of(api_exc.BadRequest(f"Unrecognized name: {CANARY} at [1:30]"))
    assert CANARY not in err.detail and CANARY not in str(err) and "[EMAIL_MASKED]" in err.detail


def test_an_error_message_is_cut_to_300_characters_after_masking():
    err = kind_of(api_exc.BadRequest("Syntax error: " + "x" * 2000))
    assert len(err.detail) == 300


@pytest.mark.parametrize("exc", [
    api_exc.Forbidden(f"Access Denied: {CANARY} cannot run jobs"),
    api_exc.NotFound(f"Not found: Table {CANARY}"),
    api_exc.NotFound(f"Not found: Dataset {CANARY}"),
    api_exc.ServiceUnavailable(f"backend said {CANARY}"),
    api_exc.TooManyRequests(f"{CANARY} is rate limited"),
])
def test_kinds_other_than_sql_errors_never_carry_the_raw_message(exc):
    err = kind_of(exc, phase="metadata")
    assert CANARY not in err.detail and "canary" not in err.detail
    assert err.detail.startswith("HTTP ")


def test_the_reason_slug_is_kept_only_when_it_looks_like_one():
    err = kind_of(api_exc.Forbidden("no", errors=[{"reason": f"see {CANARY}"}]))
    assert CANARY not in err.detail


def test_normalize_message_drops_job_ids_locations_urls_and_the_repeated_reason():
    raw = ("Syntax error: Unexpected identifier \"x\" at [1:8]; reason: invalidQuery, location: query, message: ...\n\n"
           "Location: US\nJob ID: 1b2c3d-4e5f (see https://console.cloud.google.com/bigquery?j=abc)")
    assert normalize_message(raw) == 'Syntax error: Unexpected identifier "…" at [1:8]'
    assert normalize_message("a (job ID: abc-123) b\nLocation: EU\n") == "a b"


def test_a_job_may_still_run_after_a_timeout_or_a_transient_failure_but_not_after_a_failed_job():
    assert job_may_still_run(TimeoutError()) and job_may_still_run(api_exc.ServiceUnavailable("503"))
    assert job_may_still_run(requests.exceptions.ConnectionError("reset"))
    assert not job_may_still_run(api_exc.BadRequest("Unrecognized name: x at [1:8]"))
    assert not job_may_still_run(api_exc.Forbidden("Access Denied"))
    assert not job_may_still_run(ValueError("a bug"))


# 404, the phases, and transport failures -----------------------------------------------------------

@pytest.mark.parametrize("message", [
    "Not found: Table my-project:ds.paymentz was not found in location US",
    "Not found: Dataset my-project:thelook_ecommerce was not found in location US",  # the model named a wrong dataset
])
def test_a_404_on_a_query_is_a_repairable_mistake_whether_the_table_or_the_dataset_is_missing(message):
    for phase in ("dry_run", "query"):
        err = kind_of(api_exc.NotFound(message), phase=phase)
        assert err.kind == ErrorKind.SQL_UNKNOWN_COLUMN and err.repairable and not err.retryable


def test_a_404_on_a_metadata_call_is_a_setup_problem_because_that_is_where_the_configured_dataset_is_proven():
    for message in ("Not found: Dataset bigquery-public-data:nope", "Not found: Table bigquery-public-data:thelook_ecommerce.orders"):
        err = kind_of(api_exc.NotFound(message), phase="metadata")
        assert err.kind == ErrorKind.BQ_UNAVAILABLE and not err.retryable and "bigquery.dataset" in err.detail


@pytest.mark.parametrize("exc", [
    requests.exceptions.ChunkedEncodingError("connection broken"),
    requests.exceptions.SSLError("handshake"),
    http.client.RemoteDisconnected("closed"),
    http.client.IncompleteRead(b"x"),
    ssl.SSLError("eof"),
    api_exc.RetryError("deadline", cause=None),
])
def test_other_transport_failures_are_retryable_unavailable(exc):
    err = kind_of(exc)
    assert err.kind == ErrorKind.BQ_UNAVAILABLE and err.retryable


def test_a_sign_in_that_fails_for_lack_of_network_is_transient_but_a_revoked_one_is_not():
    assert kind_of(auth_exc.TransportError("dns")).retryable
    revoked = kind_of(auth_exc.RefreshError("invalid_grant"))
    assert not revoked.retryable and "gcloud auth application-default login" in revoked.detail
    assert not kind_of(auth_exc.GoogleAuthError("other")).retryable


def test_a_client_that_cannot_be_created_says_to_sign_in_and_set_the_project_and_keeps_only_the_class():
    err = setup_error(OSError("Project was not passed and could not be determined from the environment. canary@example.test"))
    assert err.kind == ErrorKind.BQ_UNAVAILABLE and not err.retryable and err.user_message == SETUP_MESSAGE
    assert "OSError" in err.detail and "GOOGLE_CLOUD_PROJECT" in err.detail and "canary" not in err.detail


# job failures that BigQuery reports with a 400 but that are not the query's fault -------------------------------

@pytest.mark.parametrize("reason", ["jobBackendError", "jobInternalError", "tableUnavailable", "backendError", "resourceInUse"])
def test_a_400_for_a_failure_on_bigquerys_side_is_retryable_and_not_a_syntax_error(reason):
    err = kind_of(api_exc.BadRequest("Some message that says nothing about syntax", errors=[{"reason": reason}]))
    assert err.kind == ErrorKind.BQ_UNAVAILABLE and err.retryable and not err.repairable
    assert job_may_still_run(api_exc.BadRequest("x", errors=[{"reason": reason}]))


def test_a_400_job_timeout_is_a_query_timeout_while_running_and_a_connection_problem_elsewhere():
    exc = api_exc.BadRequest("Job timed out after 35 sec", errors=[{"reason": "timeout"}])
    err = kind_of(exc, "query")
    assert err.kind == ErrorKind.QUERY_TIMEOUT and not err.repairable and not err.retryable and job_may_still_run(exc)
    for phase in ("dry_run", "metadata"):
        assert kind_of(exc, phase).kind == ErrorKind.BQ_UNAVAILABLE and kind_of(exc, phase).retryable


def test_a_real_syntax_error_is_still_a_syntax_error_even_with_a_reason_slug():
    err = kind_of(api_exc.BadRequest("Syntax error: Unexpected keyword FROM at [1:8]", errors=[{"reason": "invalidQuery"}]))
    assert err.kind == ErrorKind.SQL_SYNTAX and err.repairable
