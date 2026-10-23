import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest
from langgraph.errors import GraphInterrupt, NodeInterrupt

from src.config import load_settings
from src.errors import AgentError, ErrorKind
from src.logging_setup import setup_logging, shutdown_logging
from src.observability import (
    Tracer,
    current_trace_id,
    event,
    guard_decision,
    prompt_attrs,
    purge_old_traces,
    span,
    turn_metrics,
)
from src.observability import tracing
from src.observability.metrics import read_runs

CANARY = "canary@example.test"


@pytest.fixture
def tracer(tmp_path):
    return Tracer(tmp_path / "traces", tmp_path / "runs.jsonl")


def run_turn(tracer, **kwargs):
    return tracer.start_turn("hash0001", "conv-1", "t-1", **kwargs)


def spans_of(tracer, trace_id) -> list[dict]:
    text = tracer.trace_path(trace_id).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


def by_name(spans, name) -> dict:
    return next(s for s in spans if s["name"] == name)


def rows(tracer) -> list[dict]:
    return read_runs(tracer.metrics_file)[0]


# ids and the file --------------------------------------------------------------------------------

def test_a_turn_writes_one_trace_file_and_one_metrics_row(tracer):
    with run_turn(tracer, question="how are sales?") as turn:
        assert current_trace_id() == turn.trace_id
    assert len(turn.trace_id) == 8 and int(turn.trace_id, 16) >= 0
    spans = spans_of(tracer, turn.trace_id)
    assert [s["name"] for s in spans] == ["turn"]
    assert spans[0]["parent_id"] is None and spans[0]["kind"] == "turn"
    assert spans[0]["attrs"]["user_hash"] == "hash0001"
    assert spans[0]["attrs"]["conversation_id"] == "conv-1" and spans[0]["attrs"]["turn_id"] == "t-1"
    (row,) = rows(tracer)
    assert row["trace_id"] == turn.trace_id and row["outcome"] == "answered" and row["success"] is True
    assert turn.row == row


def test_the_trace_id_the_user_was_given_is_the_one_stored(tracer):
    with run_turn(tracer, trace_id="ab12cd34") as turn:
        pass
    assert turn.trace_id == "ab12cd34" and tracer.trace_path("ab12cd34").is_file()


UNSAFE_IDS = [
    "../users", "a/b", "..\\x", "", "x" * 65, "has space", None, 7,
    "CON", "con", "AUX", "NUL", "PRN", "COM1", "LPT1",  # Windows device names: a read of CON.jsonl blocks
    "abcd1234\n", "abcd1234 ", "\nabcd1234",  # `$` would accept a trailing newline, `fullmatch` does not
    "abcd123", "abcd12345", "ABCDEFGH", "zzzzzzzz", "abcd-123", "abc_1234",  # not the 8 hex characters new_trace_id() makes
]


@pytest.mark.parametrize("bad", UNSAFE_IDS)
def test_an_unsafe_trace_id_is_replaced_never_used_as_a_path(tracer, bad):
    assert tracing.valid_trace_id(bad) is None
    with run_turn(tracer, trace_id=bad) as turn:
        pass
    assert tracing.valid_trace_id(turn.trace_id) and turn.trace_id != bad
    assert tracer.trace_path(bad if isinstance(bad, str) else "") is None
    assert [p.name for p in tracer.traces_dir.iterdir()] == [f"{turn.trace_id}.jsonl"]


def test_the_trace_id_gate_accepts_exactly_what_new_trace_id_makes():
    from src.errors import new_trace_id

    for _ in range(200):
        made = new_trace_id()
        assert tracing.valid_trace_id(made) == made
    assert tracing.valid_trace_id("ABCD1234") == "abcd1234"  # typed in capitals: same file


def test_there_is_one_place_that_turns_an_id_into_a_path(tracer, tmp_path):
    assert tracing.trace_file(tmp_path, "abcd1234") == tmp_path / "abcd1234.jsonl"
    assert tracer.trace_path("ABCD1234") == tracer.traces_dir / "abcd1234.jsonl"
    assert tracing.trace_file(tmp_path, "CON") is None


def test_span_ids_are_4_hex_characters_and_unique_inside_a_trace(tracer):
    with run_turn(tracer) as turn:
        for index in range(60):
            with span(f"step{index}"):
                pass
    ids = [s["span_id"] for s in spans_of(tracer, turn.trace_id)]
    assert len(ids) == 61 and len(set(ids)) == 61 and all(len(i) == 4 for i in ids)


def test_context_is_reset_after_the_turn(tracer):
    with run_turn(tracer):
        assert current_trace_id() is not None
    assert current_trace_id() is None


# nesting ------------------------------------------------------------------------------------------

def test_nested_spans_have_the_right_parents(tracer):
    with run_turn(tracer) as turn:
        with span("sql_loop", "step") as loop:
            with span("bq.dry_run", "query"):
                pass
            with span("bq.query", "query"):
                with span("inner", "tool"):
                    pass
        with span("composer", "step"):
            pass
    spans = spans_of(tracer, turn.trace_id)
    root = by_name(spans, "turn")
    ids = {s["name"]: s["span_id"] for s in spans}
    parent = {s["name"]: s["parent_id"] for s in spans}
    assert parent == {
        "turn": None, "sql_loop": root["span_id"], "bq.dry_run": ids["sql_loop"],
        "bq.query": ids["sql_loop"], "inner": ids["bq.query"], "composer": root["span_id"],
    }
    assert loop.span_id == ids["sql_loop"]
    assert all(s["trace_id"] == turn.trace_id for s in spans)


def test_spans_carry_timing_and_a_status(tracer):
    with run_turn(tracer) as turn:
        with span("slow", "step"):
            time.sleep(0.02)
    spans = spans_of(tracer, turn.trace_id)
    slow = by_name(spans, "slow")
    assert slow["status"] == "ok" and slow["error_kind"] is None
    assert slow["duration_ms"] >= 15 and slow["end_ts"] >= slow["start_ts"]
    assert by_name(spans, "turn")["duration_ms"] >= slow["duration_ms"]
    assert rows(tracer)[0]["latency_ms"] >= 15


def test_a_span_kind_must_be_known(tracer):
    with run_turn(tracer):
        with pytest.raises(ValueError, match="span kind"):
            with span("x", "nonsense"):
                pass
        with pytest.raises(ValueError):
            with span("x", "turn"):  # the root kind belongs to start_turn
                pass


def test_events_are_recorded_inside_the_current_span(tracer):
    with run_turn(tracer) as turn:
        with span("planner", "step"):
            event("numeric_check", number="12.5", subquestion="q1")
    planner = by_name(spans_of(tracer, turn.trace_id), "planner")
    assert planner["events"][0]["name"] == "numeric_check"
    assert planner["events"][0]["attrs"] == {"number": "12.5", "subquestion": "q1"}


def test_guard_decisions_are_events_and_a_block_counts(tracer):
    with run_turn(tracer) as turn:
        with span("sql_guard", "guard"):
            guard_decision("sql", "pass", "SQL-001")
            guard_decision("sql", "block", "SQL-007", table="users")
        guard_decision("output", "rewrite", None)
    spans = spans_of(tracer, turn.trace_id)
    decisions = [e["attrs"] for e in by_name(spans, "sql_guard")["events"]]
    assert decisions == [
        {"guard": "sql", "outcome": "pass", "rule": "SQL-001"},
        {"guard": "sql", "outcome": "block", "rule": "SQL-007", "table": "users"},
    ]
    assert by_name(spans, "turn")["events"][0]["attrs"]["outcome"] == "rewrite"
    assert rows(tracer)[0]["guard_blocks"] == {"input": 0, "sql": 1, "result": 0, "output": 0, "numeric": 0, "hitl": 0}


def test_guard_decision_rejects_unknown_names(tracer):
    with run_turn(tracer):
        with pytest.raises(ValueError, match="guard"):
            guard_decision("nonsense", "block", "X")
        with pytest.raises(ValueError, match="outcome"):
            guard_decision("sql", "maybe", "X")


def test_everything_is_a_quiet_no_op_outside_a_turn():
    assert current_trace_id() is None
    with span("orphan", "step", a=1) as s:
        s.set(b=2)
        event("anything")
        guard_decision("sql", "block", "X")
    turn_metrics().add_llm_call("m", tokens_in=5)  # a throwaway object, so callers never check
    assert prompt_attrs("t", 1, "text")["prompt_chars"] == 4


# errors -------------------------------------------------------------------------------------------

def test_an_agent_error_inside_a_span_records_status_kind_and_detail_and_the_turn_still_writes(tracer):
    with pytest.raises(AgentError):
        with run_turn(tracer, trace_id="aaaa1111"):
            with span("sql_loop", "step"):
                with span("bq.query", "query", sql="SELECT 1"):
                    raise AgentError(ErrorKind.BQ_UNAVAILABLE, detail="503 from the warehouse")
    spans = spans_of(tracer, "aaaa1111")
    failing = by_name(spans, "bq.query")
    assert failing["status"] == "error" and failing["error_kind"] == "BQ_UNAVAILABLE"
    assert failing["attrs"]["error_type"] == "AgentError"
    assert failing["attrs"]["error_detail"] == "503 from the warehouse"
    # parents are marked failed too, but the detail sits only on the step that failed
    for name in ("sql_loop", "turn"):
        assert by_name(spans, name)["status"] == "error" and by_name(spans, name)["error_kind"] == "BQ_UNAVAILABLE"
        assert "error_detail" not in by_name(spans, name)["attrs"]
    (row,) = rows(tracer)
    assert row["trace_id"] == "aaaa1111" and row["outcome"] == "failed" and row["success"] is False
    assert row["error_kinds"] == ["BQ_UNAVAILABLE"]  # one error through three spans counts once


def test_a_guard_rule_is_kept_with_the_error(tracer):
    with pytest.raises(AgentError):
        with run_turn(tracer, trace_id="aaaa1112"):
            with span("sql_guard", "guard"):
                raise AgentError(ErrorKind.GUARD_VIOLATION, detail="table not allowed", rule_id="SQL-003")
    assert by_name(spans_of(tracer, "aaaa1112"), "sql_guard")["attrs"]["rule"] == "SQL-003"
    assert rows(tracer)[0]["outcome"] == "refused"  # a refusal is not a failure


@pytest.mark.parametrize("kind, outcome", [
    (ErrorKind.OUT_OF_SCOPE, "refused"),
    (ErrorKind.INPUT_REJECTED, "refused"),
    (ErrorKind.GUARD_VIOLATION, "refused"),
    (ErrorKind.OUTPUT_BLOCKED, "failed"),
    (ErrorKind.LLM_UNAVAILABLE, "failed"),
])
def test_the_default_outcome_follows_the_error_kind(tracer, kind, outcome):
    with pytest.raises(AgentError):
        with run_turn(tracer):
            raise AgentError(kind)
    assert rows(tracer)[-1]["outcome"] == outcome


def test_an_outcome_set_by_the_agent_wins_over_the_default(tracer):
    with pytest.raises(AgentError):
        with run_turn(tracer) as turn:
            turn.metrics.set_outcome("partial")
            raise AgentError(ErrorKind.NUMERIC_UNGROUNDED)
    row = rows(tracer)[0]
    assert row["outcome"] == "partial" and row["success"] is True and row["error_kinds"] == ["NUMERIC_UNGROUNDED"]


def test_a_crash_stores_the_class_and_call_sites_never_the_exception_text(tracer):
    with pytest.raises(RuntimeError):
        with run_turn(tracer, trace_id="aaaa1113"):
            with span("composer", "step"):
                raise RuntimeError(f"password=hunter2 for {CANARY}")
    spans = spans_of(tracer, "aaaa1113")
    failing = by_name(spans, "composer")
    assert failing["error_kind"] == "INTERNAL" and failing["attrs"]["error_type"] == "RuntimeError"
    assert "RuntimeError at" in failing["attrs"]["error_detail"] and "test_tracing.py" in failing["attrs"]["error_detail"]
    text = tracer.trace_path("aaaa1113").read_text(encoding="utf-8") + tracer.metrics_file.read_text(encoding="utf-8")
    assert "hunter2" not in text and CANARY not in text
    assert rows(tracer)[0]["error_kinds"] == ["INTERNAL"]


def test_ctrl_c_still_writes_the_trace_and_the_row(tracer):
    with pytest.raises(KeyboardInterrupt):
        with run_turn(tracer, trace_id="aaaa1114"):
            with span("planner", "step"):
                raise KeyboardInterrupt
    assert by_name(spans_of(tracer, "aaaa1114"), "planner")["error_kind"] == "INTERRUPTED"
    row = rows(tracer)[0]
    assert row["outcome"] == "failed" and row["error_kinds"] == []  # INTERRUPTED is not an ErrorKind


def test_a_langgraph_pause_is_not_an_error(tracer):
    # interrupt() raises GraphInterrupt out of the node: the HITL gate is waiting for the user, nothing failed.
    with pytest.raises(GraphInterrupt):
        with run_turn(tracer, trace_id="aaaa2001") as turn:
            with span("hitl_gate", "hitl"):
                raise GraphInterrupt()
    gate = by_name(spans_of(tracer, "aaaa2001"), "hitl_gate")
    assert gate["status"] == "ok" and gate["error_kind"] is None and gate["attrs"]["paused"] == "GraphInterrupt"
    assert by_name(spans_of(tracer, "aaaa2001"), "turn")["status"] == "ok"
    row = turn.row
    assert row["outcome"] == "answered" and row["error_kinds"] == []


def test_every_langgraph_control_flow_exception_counts_as_a_pause(tracer):
    for index, exc in enumerate((NodeInterrupt("approve?"), GraphInterrupt())):
        with pytest.raises(type(exc)):
            with run_turn(tracer, trace_id=f"aaaa300{index}"):
                with span("hitl_gate", "hitl"):
                    raise exc
    assert [r["error_kinds"] for r in rows(tracer)] == [[], []]


def test_a_real_error_next_to_a_pause_is_still_an_error(tracer):
    with pytest.raises(RuntimeError):
        with run_turn(tracer, trace_id="aaaa2002"):
            with span("hitl_gate", "hitl"):
                raise RuntimeError("boom")
    assert by_name(spans_of(tracer, "aaaa2002"), "hitl_gate")["status"] == "error"
    assert rows(tracer)[0]["outcome"] == "failed"


def test_a_span_left_open_is_closed_as_unfinished_when_the_turn_ends(tracer):
    with run_turn(tracer, trace_id="aaaa1115") as turn:
        leaked = tracing._TRACE.get().new_span("leaked", "step", turn.root, {})
    assert leaked.end_ts is not None
    leaked_record = by_name(spans_of(tracer, "aaaa1115"), "leaked")
    assert leaked_record["status"] == "error" and leaked_record["error_kind"] == "UNFINISHED"


def test_a_runaway_loop_cannot_fill_the_disk(tracer, monkeypatch):
    monkeypatch.setattr(tracing, "MAX_SPANS", 5)
    monkeypatch.setattr(tracing, "MAX_EVENTS", 3)
    with run_turn(tracer) as turn:
        for index in range(12):
            with span(f"s{index}", "step") as current:
                for _ in range(10):
                    current.event("tick")
    spans = spans_of(tracer, turn.trace_id)
    assert len(spans) == 5
    assert by_name(spans, "turn")["attrs"]["spans_dropped"] == 8
    assert len(by_name(spans, "s0")["events"]) == 3 and by_name(spans, "s0")["attrs"]["events_dropped"] == 7


# the PII rule (F44, S6) ---------------------------------------------------------------------------

def test_a_canary_in_the_question_a_sql_literal_and_an_error_never_reaches_the_files(tracer, project):
    settings = load_settings(project / "config.yaml")
    setup_logging(settings)
    try:
        with pytest.raises(AgentError):
            with run_turn(tracer, trace_id="aaaa1116", question=f"what did {CANARY} buy?"):
                with span("bq.query", "query", sql=f"SELECT * FROM users WHERE email = '{CANARY}'", rows=0):
                    event("note", text=f"saw {CANARY}")
                    logging.getLogger("src.agent").error("query failed for %s", CANARY)
                    raise AgentError(ErrorKind.SQL_SYNTAX, detail=f"Syntax error near '{CANARY}'")
    finally:
        shutdown_logging()
    trace_text = tracer.trace_path("aaaa1116").read_text(encoding="utf-8")
    runs_text = tracer.metrics_file.read_text(encoding="utf-8")
    log_text = settings.log_path.read_text(encoding="utf-8")
    for name, text in (("trace", trace_text), ("runs", runs_text), ("log", log_text)):
        assert CANARY not in text, f"{name} holds the canary"
        assert "canary" not in text.lower(), f"{name} holds part of the canary"
    assert trace_text.count("[EMAIL_MASKED]") >= 4  # question, SQL, event, error detail
    assert "[EMAIL_MASKED]" in log_text


def test_attribute_values_are_masked_truncated_and_never_rows(tracer):
    frame = pd.DataFrame({"email": [CANARY], "revenue": [1.0]})
    with run_turn(tracer) as turn:
        with span(
            "bq.query", "query",
            frame=frame,
            rows_list=[{"email": CANARY}, {"email": "x"}],
            matrix=[[1, 2], [3, 4]],
            columns=["order_id", "revenue"],
            many=list(range(80)),
            nested={"a": {"b": {"c": {"d": 1}}}, CANARY: 1},
            long="x" * 5000,
            nan=float("nan"),
            number=7,
            flag=True,
            enum_value=ErrorKind.BQ_UNAVAILABLE,
            when=datetime(2026, 10, 22, tzinfo=timezone.utc),
            obj=object(),
        ):
            pass
    attrs = by_name(spans_of(tracer, turn.trace_id), "bq.query")["attrs"]
    assert attrs["frame"] == "<DataFrame>"
    assert attrs["rows_list"] == "<2 nested items withheld>" and attrs["matrix"] == "<2 nested items withheld>"
    assert attrs["columns"] == ["order_id", "revenue"]
    assert len(attrs["many"]) == 51 and attrs["many"][-1] == "...(+30 more)"
    assert attrs["nested"]["a"]["b"]["c"] == "<mapping>" and "[EMAIL_MASKED]" in attrs["nested"]
    assert len(attrs["long"]) == 2000 and attrs["long"].endswith("…")
    assert attrs["nan"] is None and attrs["number"] == 7 and attrs["flag"] is True
    assert attrs["enum_value"] == "BQ_UNAVAILABLE" and attrs["when"].startswith("2026-10-22")
    assert attrs["obj"] == "<object>"
    assert CANARY not in json.dumps(attrs)


def test_numbers_from_pandas_numpy_and_bigquery_keep_their_value(tracer):
    with run_turn(tracer) as turn:
        with span(
            "bq.query", "query",
            rows=np.int64(5), bytes=np.uint32(7), ratio=np.float32(0.5), cost=Decimal("1.5"), flag=np.bool_(True),
            missing=np.float64("nan"), big=Decimal("NaN"), total=pd.DataFrame({"x": [1, 2]})["x"].sum(),
            zero_d=np.array(3),
        ):
            pass
    attrs = by_name(spans_of(tracer, turn.trace_id), "bq.query")["attrs"]
    assert attrs["rows"] == 5 and attrs["bytes"] == 7 and attrs["ratio"] == 0.5 and attrs["cost"] == 1.5
    assert attrs["flag"] is True and attrs["missing"] is None and attrs["big"] is None
    assert attrs["total"] == 3 and attrs["zero_d"] == 3
    assert not any(isinstance(v, str) and v.startswith("<") for v in attrs.values())


def test_a_series_or_array_is_never_unpacked_into_cell_values(tracer):
    with run_turn(tracer) as turn:
        with span("x", "step", one=pd.Series([42]), arr=np.array([1, 2, 3]), frame=pd.DataFrame({"a": [1]})):
            pass
    attrs = by_name(spans_of(tracer, turn.trace_id), "x")["attrs"]
    assert attrs == {"one": "<Series>", "arr": "<ndarray>", "frame": "<DataFrame>"}


def test_a_pii_value_is_masked_before_it_is_cut_so_no_half_email_survives(tracer):
    padded = "x" * 1990 + CANARY  # the email straddles the 2000-character cut
    with run_turn(tracer) as turn:
        with span("s", "step", text=padded):
            pass
    text = by_name(spans_of(tracer, turn.trace_id), "s")["attrs"]["text"]
    assert "canary" not in text and "@" not in text and text.endswith("[EMAIL_MASKED]")


def test_prompts_store_only_an_id_a_version_a_hash_and_a_length_by_default(tracer):
    prompt = f"Question about {CANARY}. Rows: ..."
    with run_turn(tracer) as turn:
        with span("llm.call", "llm", **prompt_attrs("composer", "v3", prompt)):
            pass
    attrs = by_name(spans_of(tracer, turn.trace_id), "llm.call")["attrs"]
    assert attrs["prompt_id"] == "composer" and attrs["prompt_chars"] == len(prompt)
    assert attrs["prompt_version"] == f"composer@v3+{attrs['prompt_hash']}" and len(attrs["prompt_hash"]) == 8
    assert "prompt_text" not in attrs


def test_debug_prompts_stores_the_text_still_masked(tmp_path):
    tracer = Tracer(tmp_path / "t", tmp_path / "r.jsonl", debug_prompts=True)
    with run_turn(tracer) as turn:
        with span("llm.call", "llm", **prompt_attrs("composer", 1, f"Question about {CANARY}")):
            pass
    attrs = by_name(spans_of(tracer, turn.trace_id), "llm.call")["attrs"]
    assert attrs["prompt_text"] == "Question about [EMAIL_MASKED]"


def test_the_login_name_never_appears_only_the_user_hash(tracer):
    with run_turn(tracer, question="hello"):
        pass
    text = tracer.trace_path(rows(tracer)[0]["trace_id"]).read_text(encoding="utf-8") + tracer.metrics_file.read_text(encoding="utf-8")
    assert "hash0001" in text and "alice" not in text


# robustness ---------------------------------------------------------------------------------------

def test_a_trace_that_cannot_be_written_never_breaks_the_turn_and_the_row_is_still_written(tmp_path, caplog):
    blocker = tmp_path / "traces"
    blocker.write_text("a file where the traces folder should be", encoding="utf-8")
    tracer = Tracer(blocker, tmp_path / "runs.jsonl")
    with caplog.at_level(logging.ERROR, logger="src"):
        with run_turn(tracer, trace_id="bbbb0001") as turn:
            pass
    assert turn.row["trace_id"] == "bbbb0001"
    assert rows(tracer)[0]["trace_id"] == "bbbb0001"
    assert any("could not write trace file" in r.getMessage() for r in caplog.records)


def test_text_that_cannot_be_encoded_still_leaves_a_trace_and_a_row(tracer):
    # A lone surrogate (a bad paste, a bad decode) used to raise UnicodeEncodeError out of write_text and take
    # both the trace and the metrics row with it, leaving an empty .tmp file.
    with run_turn(tracer, trace_id="aaaa4001", question="hi \ud800 there") as turn:
        with span("llm.call", "llm", **prompt_attrs("composer", 1, "prompt \udc80 text"), note="x\ud83d"):
            pass
    assert "?" in by_name(spans_of(tracer, "aaaa4001"), "turn")["attrs"]["question"]
    assert rows(tracer)[0]["trace_id"] == "aaaa4001" and turn.row is not None
    assert sorted(p.name for p in tracer.traces_dir.iterdir()) == ["aaaa4001.jsonl"]  # no leftover .tmp


def test_a_trace_write_that_fails_in_any_way_never_costs_the_metrics_row(tracer, monkeypatch, caplog):
    def explode(self, ctx):
        raise ValueError("not an OSError")

    monkeypatch.setattr(Tracer, "_write_trace", explode)
    with caplog.at_level(logging.ERROR, logger="src"):
        with run_turn(tracer, trace_id="aaaa4002"):
            pass
    assert rows(tracer)[0]["trace_id"] == "aaaa4002"
    assert any("trace file (ValueError)" in r.getMessage() for r in caplog.records)


def test_a_failed_replace_removes_the_temp_file(tracer, monkeypatch):
    def locked(src, dst):
        raise PermissionError("a reader has the file open")

    monkeypatch.setattr(tracing.os, "replace", locked)
    with run_turn(tracer, trace_id="aaaa4003"):
        pass
    assert list(tracer.traces_dir.iterdir()) == []  # the masked trace must not sit there past the retention limit
    assert rows(tracer)[0]["trace_id"] == "aaaa4003"


def test_a_failure_while_writing_never_replaces_the_turns_own_error(tmp_path):
    blocker = tmp_path / "runs.jsonl"
    blocker.mkdir()  # appending to a folder fails
    tracer = Tracer(tmp_path / "traces", blocker)
    with pytest.raises(AgentError) as caught:
        with run_turn(tracer):
            raise AgentError(ErrorKind.TOOL_FAILED)
    assert caught.value.kind is ErrorKind.TOOL_FAILED


# model, query and prompt counters ----------------------------------------------------------------------

def test_other_plans_feed_the_turn_counters_through_turn_metrics(tracer):
    with run_turn(tracer, chaos=True, persona_version="default@v1+abcd1234") as turn:
        counters = turn_metrics()
        counters.set_intent("sales_by_brand", "simple")
        counters.add_llm_call("gemini-3.8-flash", tokens_in=100, tokens_out=40, prompt_version="planner@v1+aa")
        counters.add_llm_call("gemini-3.5-flash-lite", tokens_in=10, tokens_out=5, fallback_used=True)
        counters.add_query(bytes_billed=2048)
        counters.add_repair()
        counters.set_subquestions(2)
    row = turn.row
    assert (row["intent"], row["turn_kind"], row["chaos"]) == ("sales_by_brand", "simple", True)
    assert (row["llm_calls"], row["tokens_in"], row["tokens_out"], row["fallback_used"]) == (2, 110, 45, True)
    assert (row["bq_queries"], row["bytes_billed"], row["repairs"], row["subquestions"]) == (1, 2048, 1, 2)
    assert row["models"] == ["gemini-3.8-flash", "gemini-3.5-flash-lite"] and row["prompt_versions"] == ["planner@v1+aa"]
    assert row["persona_version"] == "default@v1+abcd1234"
    assert by_name(spans_of(tracer, turn.trace_id), "turn")["attrs"]["persona_version"] == "default@v1+abcd1234"


def test_the_tracer_reads_its_paths_and_prices_from_the_settings(project):
    (project / "config.yaml").write_text(
        "llm:\n  pricing_usd_per_1m:\n    gemini-3.8-flash: {in: 0.5, out: 2.0}\n", encoding="utf-8"
    )
    settings = load_settings(project / "config.yaml")
    tracer = Tracer.from_settings(settings, debug_prompts=True)
    assert tracer.traces_dir == project / "data" / "traces" and tracer.metrics_file == project / "data" / "runs.jsonl"
    assert tracer.pricing == {"gemini-3.8-flash": (0.5, 2.0)} and tracer.debug_prompts is True


# retention ----------------------------------------------------------------------------------------

def age(path, days):
    path.write_text("{}\n", encoding="utf-8")
    stamp = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
    os.utime(path, (stamp, stamp))


def test_the_purge_deletes_only_old_trace_files(tmp_path):
    folder = tmp_path / "traces"
    folder.mkdir()
    for name, days in {"01d00001.jsonl": 100, "01d00002.jsonl": 91, "0e000001.jsonl": 89, "fe500001.jsonl": 0}.items():
        age(folder / name, days)
    keep_other = folder / "notes.txt"  # not a trace file: never touched, however old
    keep_other.write_text("keep", encoding="utf-8")
    os.utime(keep_other, (0, 0))
    (folder / "abcd1234.jsonl").mkdir()  # a folder named like a trace is left alone
    assert purge_old_traces(folder, 90) == 2
    assert sorted(p.name for p in folder.iterdir()) == ["0e000001.jsonl", "abcd1234.jsonl", "fe500001.jsonl", "notes.txt"]


def test_the_purge_also_removes_old_temp_files_of_failed_writes(tmp_path):
    folder = tmp_path / "traces"
    folder.mkdir()
    age(folder / "01d00001.jsonl.tmp", 100)  # a write that failed halfway, long ago
    age(folder / "fe500001.jsonl.tmp", 1)  # maybe a write in progress: left alone
    assert purge_old_traces(folder, 90) == 1
    assert [p.name for p in folder.iterdir()] == ["fe500001.jsonl.tmp"]


def test_the_purge_never_touches_runs_jsonl_even_in_the_same_folder(tmp_path):
    # observability.traces_dir may point at the folder that holds the metrics file: its rows are kept for good.
    age(tmp_path / "runs.jsonl", 400)
    age(tmp_path / "01d00001.jsonl", 400)
    assert purge_old_traces(tmp_path, 90) == 1
    assert [p.name for p in tmp_path.iterdir()] == ["runs.jsonl"]


def test_ids_that_are_not_trace_ids_are_never_purged(tmp_path):
    for name in ("abcd1234.json", "abcd12345.jsonl", "ABCDEFGH.jsonl", "notes.jsonl", "abcd1234.jsonl.bak"):
        age(tmp_path / name, 400)
    before = sorted(p.name for p in tmp_path.iterdir())
    assert purge_old_traces(tmp_path, 90) == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_the_purge_of_a_missing_folder_does_nothing(tmp_path):
    assert purge_old_traces(tmp_path / "nope", 90) == 0
