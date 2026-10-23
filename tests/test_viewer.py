import io
import json

import pytest
from rich.console import Console

from src.access.users import UserContext
from src.config import load_settings
from src.errors import AgentError, ErrorKind
from src.observability import Tracer, guard_decision, span, turn_metrics, viewer
from src.observability.metrics import read_runs, summarize

CANARY = "canary@example.test"


def user(user_hash="hash0001", role="brand_manager", all_brands=False) -> UserContext:
    return UserContext("u", "U", role, ("Brand A",), all_brands, user_hash)


def capture(fn, *args, **kwargs) -> str:
    buffer = io.StringIO()
    fn(Console(file=buffer, width=200, color_system=None, force_terminal=False), *args, **kwargs)
    return buffer.getvalue()


@pytest.fixture
def settings(project):
    return load_settings(project / "config.yaml")


@pytest.fixture
def tracer(settings):
    return Tracer.from_settings(settings)


def a_failed_turn(tracer, trace_id="f00dcafe", user_hash="hash0001", question="top brands?"):
    with pytest.raises(AgentError):
        with tracer.start_turn(user_hash, "conv-1", "t-1", trace_id=trace_id, question=question, chaos=True):
            with span("input_guard", "step"):
                guard_decision("input", "pass", "IN-001")
            with span("sql_loop", "step"):
                with span("llm.call", "llm", model="gemini-3.8-flash", tokens_in=120, tokens_out=45):
                    pass
                with span("bq.query", "query", sql="SELECT 1", rows=0):
                    guard_decision("sql", "block", "SQL-007")
                    raise AgentError(ErrorKind.BQ_UNAVAILABLE, detail="503 from the warehouse")
    return trace_id


# /trace ---------------------------------------------------------------------------------------------

def test_a_stored_turn_renders_as_a_tree_with_steps_guards_and_the_error(tracer, settings):
    trace_id = a_failed_turn(tracer)
    out = capture(viewer.show_trace, settings, trace_id)
    for expected in (
        f"Trace {trace_id}", "turn t-1", "question: top brands?",
        "input_guard", "guard input: pass", "sql_loop", "llm.call", "gemini-3.8-flash", "tokens 120 in, 45 out",
        "bq.query", "ERROR BQ_UNAVAILABLE", "guard sql: block", "rule SQL-007",
        "BQ_UNAVAILABLE (AgentError): 503 from the warehouse",
    ):
        assert expected in out, expected
    assert "Traceback" not in out
    # the failing step is the one carrying the reason, indented under its parents
    lines = out.splitlines()
    sql_loop = next(i for i, line in enumerate(lines) if "sql_loop" in line)
    bq = next(i for i, line in enumerate(lines) if "bq.query" in line)
    assert bq > sql_loop and lines[bq].index("bq.query") > lines[sql_loop].index("sql_loop")


def test_a_bare_trace_shows_the_last_turn(tracer, settings):
    a_failed_turn(tracer, "00000001")
    a_failed_turn(tracer, "00000002")
    out = capture(viewer.show_trace, settings)
    assert "Trace 00000002" in out and "Trace 00000001" not in out


def test_raw_prints_the_stored_spans_as_json(tracer, settings):
    trace_id = a_failed_turn(tracer)
    out = capture(viewer.show_trace, settings, trace_id, raw=True)
    records = [json.loads(line) for line in out.splitlines() if line.strip()]
    assert {r["name"] for r in records} == {"turn", "input_guard", "sql_loop", "llm.call", "bq.query"}
    assert all(r["trace_id"] == trace_id for r in records)


@pytest.mark.parametrize("wanted", [
    "deadbeef", "../users", "a/b", "", "x" * 70, "[bold]oops[/]",
    "CON", "aux", "COM1", "nul",  # Windows device names: opening CON.jsonl blocks forever
    "deadbeef\n", "dead",
])
def test_an_unknown_or_unsafe_id_prints_a_clear_message_not_a_stack_trace(settings, wanted):
    buffer = io.StringIO()
    console = Console(file=buffer, width=200, color_system=None)
    found = viewer.show_trace(console, settings, wanted or "   ")
    out = buffer.getvalue()
    assert found is False and "No trace found" in out and "Traceback" not in out and "90 days" in out


def test_an_id_is_never_turned_into_a_path_outside_the_traces_folder(tracer, settings, project):
    (project / "data").mkdir(exist_ok=True)
    (project / "data" / "secret.jsonl").write_text(json.dumps({"span_id": "x", "name": "leak"}) + "\n", encoding="utf-8")
    assert viewer.load_trace(settings.traces_path, "../secret") is None
    assert viewer.load_trace(settings.traces_path, "..\\secret") is None
    assert "leak" not in capture(viewer.show_trace, settings, "../secret")


def test_no_turns_yet_says_so(settings):
    out = capture(viewer.show_trace, settings)
    assert "No turns recorded yet" in out


def test_a_trace_id_typed_in_capitals_finds_the_same_turn(tracer, settings):
    a_failed_turn(tracer, "f00dcafe")
    assert "Trace f00dcafe" in capture(viewer.show_trace, settings, "F00DCAFE")


def test_a_question_with_a_unicode_line_separator_does_not_hide_the_owners_own_turn(tracer, settings):
    # The root span line holds the question. U+2028 is legal unescaped in JSON; splitlines() cut the line in
    # two, dropped the root, and the owner got "No trace found" for their own turn.
    question = "top brands last month please\u0085thanks"
    with tracer.start_turn("hash0001", "conv-1", "t-1", trace_id="0000a001", question=question):
        with span("echo", "step"):
            pass
    spans = viewer.load_trace(settings.traces_path, "0000a001")
    assert [s["name"] for s in spans] == ["turn", "echo"] and spans[0]["attrs"]["question"] == question
    assert "Trace 0000a001" in capture(viewer.show_trace, settings, "0000a001", user=user("hash0001"))
    assert viewer.last_trace_id(settings.metrics_path, "hash0001") == "0000a001"


def test_a_trace_file_with_bad_bytes_still_renders_the_readable_spans(tracer, settings):
    trace_id = a_failed_turn(tracer)
    path = tracer.trace_path(trace_id)
    path.write_bytes(path.read_bytes() + b'{"torn \xc3\n')
    out = capture(viewer.show_trace, settings, trace_id)
    assert "bq.query" in out and "Traceback" not in out


def test_an_unreadable_trace_file_is_just_not_found(tracer, settings):
    tracer.traces_dir.mkdir(parents=True)
    (tracer.traces_dir / "0000b001.jsonl").mkdir()  # reading a folder raises an OSError
    assert "No trace found" in capture(viewer.show_trace, settings, "0000b001")


def test_a_user_sees_only_their_own_turns_and_the_ceo_sees_all(tracer, settings):
    mine = a_failed_turn(tracer, "aaaa0001", user_hash="hash0001")
    theirs = a_failed_turn(tracer, "bbbb0001", user_hash="hash0002")
    me = user("hash0001")
    assert "Trace aaaa0001" in capture(viewer.show_trace, settings, mine, user=me)
    hidden = capture(viewer.show_trace, settings, theirs, user=me)
    assert "No trace found" in hidden and "bq.query" not in hidden  # same message as an unknown ID
    assert "Trace bbbb0001" in capture(viewer.show_trace, settings, theirs, user=user("hash0009", "ceo", True))
    assert "Trace bbbb0001" in capture(viewer.show_trace, settings, theirs)  # the operator command has no user
    # a bare /trace picks the newest turn of that user, not of everybody
    assert "Trace aaaa0001" in capture(viewer.show_trace, settings, None, user=me)


def test_the_session_default_wins_for_a_bare_trace(tracer, settings):
    a_failed_turn(tracer, "00000001")
    a_failed_turn(tracer, "00000002")
    assert "Trace 00000001" in capture(viewer.show_trace, settings, None, default_id="00000001")


def test_text_that_looks_like_markup_cannot_crash_the_screen(tracer, settings):
    with tracer.start_turn("hash0001", "conv-1", "t-1", trace_id="0000beef", question="[/oops] [bold]x"):
        with span("echo", "step", summary="[/broken] markup"):
            pass
    out = capture(viewer.show_trace, settings, "0000beef")
    assert "[/oops] [bold]x" in out and "[/broken] markup" in out


def test_a_damaged_trace_file_still_renders_what_is_readable(tracer, settings):
    trace_id = a_failed_turn(tracer)
    path = tracer.trace_path(trace_id)
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join([lines[0], "{torn", *lines[1:]]) + "\n", encoding="utf-8")
    assert "bq.query" in capture(viewer.show_trace, settings, trace_id)
    path.write_text("not json at all\n", encoding="utf-8")
    assert "No trace found" in capture(viewer.show_trace, settings, trace_id)


def test_a_span_whose_parent_is_missing_is_shown_under_the_root():
    spans = [
        {"trace_id": "t", "span_id": "0001", "parent_id": None, "name": "turn", "attrs": {}, "events": [], "status": "ok",
         "start_ts": "2026-10-22T10:00:00.000+00:00", "duration_ms": 5.0},
        {"trace_id": "t", "span_id": "0002", "parent_id": "gone", "name": "orphan", "attrs": {}, "events": [],
         "status": "ok", "start_ts": "2026-10-22T10:00:00.001+00:00", "duration_ms": 1.0},
    ]
    buffer = io.StringIO()
    Console(file=buffer, width=100, color_system=None).print(viewer.build_tree(spans))
    assert "orphan" in buffer.getvalue()


# the metrics summary ----------------------------------------------------------------------------------

def test_the_summary_says_when_there_is_nothing_yet(settings):
    assert "No turns recorded yet" in capture(viewer.show_metrics, settings)


def test_a_metrics_file_with_bad_bytes_still_gives_a_summary(tracer, settings):
    with tracer.start_turn("hash0001", "conv-1", "t-1"):
        pass
    with open(settings.metrics_path, "ab") as handle:
        handle.write(b'{"outcome": "caf\xc3\n')
    out = capture(viewer.show_metrics, settings)
    assert "Metrics: 1 turn(s)" in out and "1 unreadable line(s)" in out and "Traceback" not in out


def test_a_metrics_file_that_cannot_be_opened_gets_a_clear_message_not_a_traceback(settings):
    settings.metrics_path.mkdir(parents=True)  # a folder where the file should be: reading it raises an OSError
    out = capture(viewer.show_metrics, settings)
    assert "could not be read" in out and "Traceback" not in out
    assert viewer.last_trace_id(settings.metrics_path) is None


def test_a_brand_manager_sees_only_their_own_turns_and_incidents_but_the_ceo_sees_all(tracer, settings):
    with tracer.start_turn("hash0001", "conv-1", "t-1"):
        pass
    with tracer.start_turn("hash0002", "conv-2", "t-1", trace_id="bad00002"):
        turn_metrics().add_pii_at_output(1)  # someone else's incident
        turn_metrics().add_query(bytes_billed=2 * 1024 ** 3)
    me = capture(viewer.show_metrics, settings, None, user=user("hash0001"))
    assert "Metrics: 1 turn(s), your turns only" in me
    assert "ALERT" not in me and "bad00002" not in me  # not mine to read
    assert "2.0 GiB billed by all users" in me  # the free tier is one shared number
    ceo = capture(viewer.show_metrics, settings, None, user=user("hash0009", "ceo", True))
    assert "Metrics: 2 turn(s), all users" in ceo and "ALERT" in ceo and "bad00002" in ceo
    operator = capture(viewer.show_metrics, settings)
    assert "Metrics: 2 turn(s), all users" in operator
    assert "No turns of yours recorded yet" in capture(viewer.show_metrics, settings, None, user=user("hash0003"))


def test_the_summary_shows_totals_rates_errors_and_the_free_tier(tracer, settings):
    with tracer.start_turn("hash0001", "conv-1", "t-1"):
        turn_metrics().add_llm_call("gemini-3.8-flash", tokens_in=100, tokens_out=50)
        turn_metrics().add_query(bytes_billed=3 * 1024 ** 3)
    a_failed_turn(tracer)
    out = capture(viewer.show_metrics, settings)
    for expected in (
        "Metrics: 2 turn(s)", "Success rate", "50.0%", "Failure rate", "Guard blocks", "sql 1",
        "PII at output", "Latency", "p95", "BQ_UNAVAILABLE x1", "not set (fill llm.pricing_usd_per_1m",
        "of the 1 TiB free tier", "3.0 GiB billed by all users",
    ):
        assert expected in out, expected
    assert "ALERT" not in out and "WARNING" not in out


def test_the_summary_leads_with_the_alert_and_warns_near_the_free_tier_limit(tracer, settings):
    with tracer.start_turn("hash0001", "conv-1", "t-1", trace_id="bad00001"):
        turn_metrics().add_pii_at_output(2)
        turn_metrics().add_query(bytes_billed=int(0.9 * 1024 ** 4))
    out = capture(viewer.show_metrics, settings)
    assert out.splitlines()[0].startswith("ALERT:") and "bad00001" in out.splitlines()[0]
    assert "WARNING: 90.0% of this month's free BigQuery tier" in out


def test_last_n_is_reported_in_the_header(tracer, settings):
    for index in range(3):
        with tracer.start_turn("hash0001", "conv-1", f"t-{index}"):
            pass
    assert "last 2 of 3 turns" in capture(viewer.show_metrics, settings, 2)
    rows, _ = read_runs(settings.metrics_path)
    assert summarize(rows, last=2).turns == 2
