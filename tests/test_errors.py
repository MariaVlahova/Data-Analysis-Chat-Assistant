import copy
import pickle

import pytest

from src.errors import AgentError, ConfigError, ErrorKind, crash_summary, default_message, friendly, new_trace_id


@pytest.mark.parametrize("kind", list(ErrorKind))
def test_every_kind_has_a_friendly_message(kind):
    message = default_message(kind)
    assert message.strip()
    assert message.endswith((".", "?"))
    assert "Traceback" not in message and "Exception" not in message
    assert AgentError(kind).user_message == message


@pytest.mark.parametrize("kind", list(ErrorKind))
def test_friendly_always_includes_the_trace_id(kind):
    text = friendly(AgentError(kind), "ab12cd34")
    assert text.endswith("Trace ID: ab12cd34")
    assert default_message(kind) in text


def test_friendly_uses_the_trace_id_on_the_error_when_none_is_passed():
    assert "Trace ID: feedbeef" in friendly(AgentError(ErrorKind.TOOL_FAILED, trace_id="feedbeef"))


def test_friendly_makes_up_a_trace_id_rather_than_omit_it():
    text = friendly(AgentError(ErrorKind.INTERNAL))
    assert "Trace ID: " in text
    assert len(text.rsplit("Trace ID: ", 1)[1]) == 8


def test_any_exception_becomes_internal_without_leaking_its_text():
    text = friendly(RuntimeError("password=hunter2 in /srv/secret/path"), "00000001")
    assert default_message(ErrorKind.INTERNAL) in text
    assert "hunter2" not in text and "/srv/secret" not in text
    assert "Trace ID: 00000001" in text


def test_from_exception_wraps_with_class_and_location_but_never_the_exception_text_and_keeps_agent_errors_as_they_are():
    wrapped = AgentError.from_exception(ValueError("password=hunter2 " + "x" * 1000), "11112222")
    assert wrapped.kind is ErrorKind.INTERNAL
    assert wrapped.trace_id == "11112222"
    assert wrapped.detail.startswith("ValueError at ") or wrapped.detail == "ValueError at unknown location"
    assert "hunter2" not in wrapped.detail and len(wrapped.detail) <= 300

    original = AgentError(ErrorKind.BQ_UNAVAILABLE)
    assert AgentError.from_exception(original, "33334444") is original
    assert original.trace_id == "33334444"


def test_crash_summary_names_the_class_and_call_sites_not_the_message():
    def inner():
        raise KeyError("customer-4711")

    def outer():
        inner()

    with pytest.raises(KeyError) as caught:
        outer()
    summary = crash_summary(caught.value)
    assert summary.startswith("KeyError at test_errors.py:")
    assert "in inner" in summary and "in outer" in summary
    assert "customer-4711" not in summary


def test_the_trace_id_already_on_the_error_wins_over_the_one_passed_in():
    err = AgentError(ErrorKind.TOOL_FAILED, trace_id="aaaa1111")
    text = friendly(err, "bbbb2222")
    assert text.endswith("Trace ID: aaaa1111")
    assert err.trace_id == "aaaa1111"


def test_a_made_up_trace_id_is_stored_on_the_error_so_it_can_be_found_again():
    err = AgentError(ErrorKind.INTERNAL)
    shown = friendly(err).rsplit("Trace ID: ", 1)[1]
    assert err.trace_id == shown


@pytest.mark.parametrize("clone", [copy.copy, copy.deepcopy, lambda e: pickle.loads(pickle.dumps(e))])
def test_agent_errors_survive_copy_deepcopy_and_pickle(clone):
    err = AgentError(
        ErrorKind.GUARD_VIOLATION, "Custom text.", detail="short detail", trace_id="cafe0001", rule_id="PII-001"
    )
    twin = clone(err)
    assert twin is not err
    assert (twin.kind, twin.user_message, twin.detail, twin.trace_id, twin.rule_id) == (
        ErrorKind.GUARD_VIOLATION, "Custom text.", "short detail", "cafe0001", "PII-001",
    )
    assert (twin.retryable, twin.repairable) == (err.retryable, err.repairable)
    assert str(twin) == str(err)


@pytest.mark.parametrize("clone", [copy.copy, copy.deepcopy, lambda e: pickle.loads(pickle.dumps(e))])
def test_config_errors_keep_their_fix_through_copy_and_pickle(clone):
    twin = clone(ConfigError("bad", fix="do this"))
    assert twin.message == "bad" and twin.fix == "do this"


def test_flags_follow_the_handling_table():
    assert AgentError(ErrorKind.SQL_SYNTAX).repairable
    assert AgentError(ErrorKind.SQL_UNKNOWN_COLUMN).repairable
    assert AgentError(ErrorKind.LLM_RATE_LIMITED).retryable
    assert AgentError(ErrorKind.BQ_UNAVAILABLE).retryable
    for no_retry in (ErrorKind.GUARD_VIOLATION, ErrorKind.NO_ACCESS, ErrorKind.RESULT_GUARD_FAILED, ErrorKind.OUTPUT_BLOCKED):
        err = AgentError(no_retry)
        assert not err.retryable and not err.repairable


def test_guard_violation_carries_a_rule_id_and_a_custom_message():
    err = AgentError(ErrorKind.GUARD_VIOLATION, "I can't show email addresses.", rule_id="PII-001")
    assert err.rule_id == "PII-001"
    assert "I can't show email addresses." in friendly(err, "aaaa0000")


def test_the_detail_is_masked_before_it_is_cut_so_no_half_email_survives():
    # The email straddles the 300-character cut: cutting first would leave "john.doe@exampl", which no pattern matches.
    err = AgentError(ErrorKind.SQL_SYNTAX, detail="x" * 284 + " john.doe@example.com tail")
    assert "john" not in err.detail and "@" not in err.detail
    assert "[EMAIL_MASKED]" in err.detail and len(err.detail) <= 300  # whole token kept, the cut falls after it


def test_a_detail_with_pii_stays_masked_after_a_copy():
    err = AgentError(ErrorKind.TOOL_FAILED, detail="failed for canary@example.test")
    assert pickle.loads(pickle.dumps(err)).detail == err.detail == "failed for [EMAIL_MASKED]"


def test_trace_ids_are_short_and_different():
    ids = {new_trace_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(len(i) == 8 for i in ids)


def test_config_error_keeps_the_fix_and_is_not_an_agent_error():
    err = ConfigError("bad", fix="do this")
    assert err.message == "bad" and err.fix == "do this"
    assert not isinstance(err, AgentError)
