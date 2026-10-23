import json
import logging
import math
from datetime import datetime, timezone

import pytest

from src.config import load_settings
from src.errors import AgentError, ConfigError, ErrorKind
from src.observability import Tracer, alerts, turn_metrics
from src.observability.metrics import (
    FREE_TIER_BYTES_PER_MONTH,
    TurnMetrics,
    percentile,
    read_runs,
    summarize,
    write_run_row,
)

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
GIB = 1024 ** 3


def row(**overrides) -> dict:
    base = {
        "ts": "2026-10-10T09:00:00.000+00:00", "trace_id": "aaaa0000", "user_hash": "h", "outcome": "answered",
        "success": True, "error_kinds": [], "guard_blocks": {}, "pii_at_output": 0, "latency_ms": 1000.0,
        "llm_calls": 2, "tokens_in": 100, "tokens_out": 50, "bytes_billed": 0, "cost_usd": 0.01,
    }
    return {**base, **overrides}


# the row -----------------------------------------------------------------------------------------

def test_the_row_has_every_field_of_the_plan_and_only_those():
    metrics = TurnMetrics(trace_id="ab12cd34", user_hash="h1", conversation_id="conv-1", turn_id="t-3")
    metrics.set_outcome("answered")
    result = metrics.to_row(ts="2026-10-22T10:00:00.000+00:00", latency_ms=12.345)
    assert list(result) == [
        "ts", "trace_id", "user_hash", "conversation_id", "turn_id", "intent", "turn_kind", "outcome", "success",
        "error_kinds", "guard_blocks", "pii_at_output", "latency_ms", "llm_calls", "tokens_in", "tokens_out",
        "fallback_used", "bq_queries", "bytes_billed", "cost_usd", "repairs", "replans", "subquestions",
        "models", "prompt_versions", "persona_version", "chaos",
    ]
    assert result["latency_ms"] == 12.3 and result["persona_version"] == "none" and result["chaos"] is False
    assert result["guard_blocks"] == {"input": 0, "sql": 0, "result": 0, "output": 0, "numeric": 0, "hitl": 0}
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("outcome, success", [
    ("answered", True), ("partial", True), ("action_done", True), ("action_cancelled", True),
    ("refused", False), ("failed", False),
])
def test_only_a_refusal_or_a_failure_is_not_success(outcome, success):
    metrics = TurnMetrics()
    metrics.set_outcome(outcome)
    assert metrics.to_row(ts="t", latency_ms=1)["success"] is success


def test_an_unknown_outcome_or_guard_is_a_programming_error():
    metrics = TurnMetrics()
    with pytest.raises(ValueError, match="outcome"):
        metrics.set_outcome("great")
    with pytest.raises(ValueError, match="guard"):
        metrics.add_guard_block("firewall")


def test_cost_is_null_without_prices_and_never_invented():
    metrics = TurnMetrics()
    metrics.add_llm_call("gemini-3.8-flash", tokens_in=1_000_000, tokens_out=1_000_000)
    assert metrics.to_row(ts="t", latency_ms=1)["cost_usd"] is None
    assert metrics.to_row(ts="t", latency_ms=1, pricing={})["cost_usd"] is None


def test_cost_is_tokens_times_the_configured_price():
    metrics = TurnMetrics()
    metrics.add_llm_call("flash", tokens_in=2_000_000, tokens_out=500_000)
    metrics.add_llm_call("lite", tokens_in=1_000_000, tokens_out=0)
    pricing = {"flash": (0.5, 2.0), "lite": (0.1, 0.4)}
    assert metrics.estimated_cost(pricing) == pytest.approx(2 * 0.5 + 0.5 * 2.0 + 0.1)


def test_cost_is_null_when_any_model_used_has_no_price():
    metrics = TurnMetrics()
    metrics.add_llm_call("flash", tokens_in=10)
    metrics.add_llm_call("unlisted", tokens_in=10)
    assert metrics.estimated_cost({"flash": (0.5, 2.0)}) is None


def test_cost_is_null_whenever_no_prices_are_set_even_for_a_turn_with_no_model_call():
    # The plan: "null when prices are not set". Otherwise unpriced spend would show as $0.0000.
    assert TurnMetrics().estimated_cost(None) is None
    assert TurnMetrics().estimated_cost({}) is None
    assert TurnMetrics().to_row(ts="t", latency_ms=1)["cost_usd"] is None


def test_with_prices_set_a_turn_with_no_model_call_costs_zero():
    assert TurnMetrics().estimated_cost({"flash": (0.5, 2.0)}) == 0.0


def test_the_summary_says_prices_are_not_set_when_every_turn_has_a_null_cost():
    s = summarize([row(cost_usd=None, llm_calls=0), row(cost_usd=None, llm_calls=2)], now=NOW)
    assert s.cost_usd is None and s.unpriced_turns == 2


def test_a_zero_call_turn_does_not_hide_the_missing_price_of_a_turn_that_called_a_model():
    s = summarize([row(cost_usd=0.0, llm_calls=0), row(cost_usd=None, llm_calls=2)], now=NOW)
    assert s.cost_usd == 0.0 and s.unpriced_turns == 1  # shown as "$0.0000 (1 turn(s) without a price left out)"


def test_the_row_masks_any_string_that_slips_in():
    metrics = TurnMetrics(user_hash="h")
    metrics.set_intent("asked about canary@example.test")
    assert "canary" not in json.dumps(metrics.to_row(ts="t", latency_ms=1))


def test_counters_count_every_call_and_unique_models_and_versions():
    metrics = TurnMetrics()
    for _ in range(3):
        metrics.add_llm_call("flash", tokens_in=1, tokens_out=2, prompt_version="p@1")
    metrics.add_error("SQL_SYNTAX")
    metrics.add_error("SQL_SYNTAX")
    result = metrics.to_row(ts="t", latency_ms=1)
    assert result["llm_calls"] == 3 and result["models"] == ["flash"] and result["prompt_versions"] == ["p@1"]
    assert result["error_kinds"] == ["SQL_SYNTAX"]
    assert (result["tokens_in"], result["tokens_out"]) == (3, 6)


# the file ----------------------------------------------------------------------------------------

def test_rows_round_trip_one_json_line_each(tmp_path):
    path = tmp_path / "data" / "runs.jsonl"
    write_run_row(path, row(trace_id="aaaa0001"))
    write_run_row(path, row(trace_id="aaaa0002"))
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2
    rows, skipped = read_runs(path)
    assert [r["trace_id"] for r in rows] == ["aaaa0001", "aaaa0002"] and skipped == 0


def test_a_torn_or_edited_line_is_skipped_and_counted_not_fatal(tmp_path):
    path = tmp_path / "runs.jsonl"
    write_run_row(path, row(trace_id="aaaa0001"))
    with open(path, "a", encoding="utf-8") as handle:
        handle.write('{"trace_id": "torn\n[1, 2]\n\n')
    write_run_row(path, row(trace_id="aaaa0002"))
    rows, skipped = read_runs(path)
    assert [r["trace_id"] for r in rows] == ["aaaa0001", "aaaa0002"] and skipped == 2


def test_a_missing_file_is_an_empty_history(tmp_path):
    assert read_runs(tmp_path / "none.jsonl") == ([], 0)


def test_bad_bytes_skip_that_line_only_and_never_raise(tmp_path):
    path = tmp_path / "runs.jsonl"
    write_run_row(path, row(trace_id="aaaa0001"))
    with open(path, "ab") as handle:
        handle.write('{"trace_id": "caf'.encode("utf-8") + b"\xc3" + b"\n")  # a write cut in the middle of "é"
        handle.write(b"\xff\xfe\n")
    write_run_row(path, row(trace_id="aaaa0002"))
    rows, skipped = read_runs(path)
    assert [r["trace_id"] for r in rows] == ["aaaa0001", "aaaa0002"] and skipped == 2


def test_a_row_with_a_unicode_line_separator_is_not_cut_in_two(tmp_path):
    # JSON allows U+2028, U+2029 and U+0085 unescaped; str.splitlines() would split on them.
    path = tmp_path / "runs.jsonl"
    for index, char in enumerate((" ", " ", "\u0085", "\x0b", "\x0c", "\x1c")):
        write_run_row(path, row(trace_id=f"aaaa00{index}0", persona_version=f"a{char}b"))
    rows, skipped = read_runs(path)
    assert len(rows) == 6 and skipped == 0
    assert [r["persona_version"] for r in rows][0] == "a b"


def test_a_file_that_exists_but_cannot_be_opened_is_an_error_not_an_empty_history(tmp_path):
    (tmp_path / "runs.jsonl").mkdir()  # reading a folder fails with an OSError on every platform
    with pytest.raises(OSError):
        read_runs(tmp_path / "runs.jsonl")


def test_the_summary_can_count_one_users_turns_only_while_the_free_tier_stays_shared():
    rows = [
        row(trace_id="a1", user_hash="mine", bytes_billed=GIB, outcome="answered"),
        row(trace_id="a2", user_hash="theirs", bytes_billed=2 * GIB, outcome="failed", success=False,
            error_kinds=["BQ_UNAVAILABLE"], pii_at_output=1),
        row(trace_id="a3", user_hash="mine", bytes_billed=GIB, outcome="answered"),
    ]
    mine = summarize(rows, now=NOW, user_hash="mine")
    assert mine.user_scoped and mine.turns == 2 and mine.total_rows == 2
    assert mine.failure_rate == 0 and mine.top_errors == [] and mine.pii_at_output_total == 0
    assert mine.pii_alert_trace_ids == []  # another user's incident is not shown to me
    assert mine.bytes_billed_month == 4 * GIB  # the free tier is shared: one number, nobody identified
    everyone = summarize(rows, now=NOW)
    assert not everyone.user_scoped and everyone.turns == 3 and everyone.pii_alert_trace_ids == ["a2"]
    assert summarize(rows, now=NOW, user_hash="mine", last=1).turns == 1  # `last` counts that user's turns


def test_runtime_files_follow_the_data_dir_unless_a_path_is_given(project):
    config = project / "config.yaml"
    config.write_text("app:\n  data_dir: var/opsfleet\n", encoding="utf-8")
    settings = load_settings(config)
    assert settings.metrics_path == project / "var" / "opsfleet" / "runs.jsonl"
    assert settings.traces_path == project / "var" / "opsfleet" / "traces"
    assert settings.log_path.parent.parent == project / "var" / "opsfleet"  # the same folder as the log
    config.write_text("app:\n  data_dir: var/opsfleet\nobservability:\n  traces_dir: elsewhere/t\n", encoding="utf-8")
    moved = load_settings(config)
    assert moved.traces_path == project / "elsewhere" / "t" and moved.metrics_path == project / "var" / "opsfleet" / "runs.jsonl"


# totals ------------------------------------------------------------------------------------------

def test_a_fixture_gives_the_expected_rates_percentiles_and_month_to_date_bytes():
    rows = [
        row(trace_id="a1", latency_ms=100, bytes_billed=1 * GIB),
        row(trace_id="a2", latency_ms=200, bytes_billed=2 * GIB),
        row(trace_id="a3", latency_ms=300, outcome="refused", success=False, llm_calls=1,
            guard_blocks={"input": 1}, error_kinds=["OUT_OF_SCOPE"], cost_usd=0.0),
        row(trace_id="a4", latency_ms=400, outcome="failed", success=False, llm_calls=3,
            guard_blocks={"sql": 2, "output": 1}, error_kinds=["BQ_UNAVAILABLE", "SQL_SYNTAX"], cost_usd=None),
        row(trace_id="a5", latency_ms=5000, outcome="partial", llm_calls=4, error_kinds=["SQL_SYNTAX"],
            ts="2026-09-30T23:59:59.000+00:00", bytes_billed=500 * GIB),  # last month: not in this month's bytes
    ]
    s = summarize(rows, now=NOW)
    assert s.turns == 5 and s.total_rows == 5
    assert s.outcomes == {"answered": 2, "refused": 1, "failed": 1, "partial": 1}
    assert (s.success_rate, s.refusal_rate, s.failure_rate) == (3 / 5, 1 / 5, 1 / 5)
    assert s.guard_blocks == {"input": 1, "sql": 2, "result": 0, "output": 1, "numeric": 0, "hitl": 0}
    assert s.latency_p50_ms == 300 and s.latency_p95_ms == 5000  # nearest rank
    assert s.avg_llm_calls == (2 + 2 + 1 + 3 + 4) / 5
    assert s.top_errors == [("SQL_SYNTAX", 2), ("OUT_OF_SCOPE", 1), ("BQ_UNAVAILABLE", 1)]
    assert (s.tokens_in, s.tokens_out) == (500, 250)
    assert s.month == "2026-10" and s.bytes_billed_month == 3 * GIB
    assert s.free_tier_fraction == pytest.approx(3 * GIB / FREE_TIER_BYTES_PER_MONTH) and not s.free_tier_warning
    assert s.cost_usd == pytest.approx(0.01 * 3) and s.unpriced_turns == 1  # a4 called models without a price


def test_last_n_limits_the_rates_but_not_the_month_to_date_bytes():
    rows = [row(trace_id=f"a{i}", bytes_billed=GIB, latency_ms=i * 10) for i in range(1, 6)]
    rows[-1]["outcome"], rows[-1]["success"] = "failed", False
    s = summarize(rows, last=2, now=NOW)
    assert s.turns == 2 and s.total_rows == 5
    assert s.failure_rate == 0.5 and s.latency_p50_ms == 40
    assert s.bytes_billed_month == 5 * GIB  # the free tier is about the whole month


def test_last_must_be_positive():
    with pytest.raises(ValueError):
        summarize([row()], last=0)


def test_a_warning_at_80_percent_of_the_free_tier():
    threshold = math.ceil(0.8 * FREE_TIER_BYTES_PER_MONTH)  # the first whole byte count at 80%
    below = summarize([row(bytes_billed=threshold - 1)], now=NOW)
    at = summarize([row(bytes_billed=threshold)], now=NOW)
    over = summarize([row(bytes_billed=FREE_TIER_BYTES_PER_MONTH)], now=NOW)
    assert not below.free_tier_warning and at.free_tier_warning and over.free_tier_warning


def test_no_rows_is_an_empty_but_valid_summary():
    s = summarize([], now=NOW)
    assert s.turns == 0 and s.success_rate == 0 and s.latency_p50_ms is None and s.cost_usd is None
    assert s.top_errors == [] and s.bytes_billed_month == 0


def test_unreadable_values_in_a_row_count_as_zero_not_a_crash():
    s = summarize([{"outcome": "answered", "latency_ms": "slow", "guard_blocks": "oops", "tokens_in": None,
                    "bytes_billed": float("nan"), "error_kinds": "x", "ts": 5}], now=NOW)
    assert s.turns == 1 and s.bytes_billed_month == 0 and s.tokens_in == 0


@pytest.mark.parametrize("values, pct, expected", [
    ([], 50, None), ([7], 50, 7), ([7], 95, 7),
    ([1, 2, 3, 4], 50, 2), ([1, 2, 3, 4], 95, 4), ([4, 1, 3, 2], 25, 1),
    (list(range(1, 101)), 95, 95), (list(range(1, 101)), 50, 50),
])
def test_percentile_is_nearest_rank(values, pct, expected):
    assert percentile(values, pct) == expected


# the PII alert ---------------------------------------------------------------------------------------

def test_a_row_with_pii_at_output_triggers_the_alert_line():
    clean = summarize([row(trace_id="ok000001")], now=NOW)
    dirty = summarize([row(trace_id="ok000001"), row(trace_id="bad00001", pii_at_output=1)], now=NOW)
    assert alerts.summary_alert(clean) is None
    line = alerts.summary_alert(dirty)
    assert line.startswith("ALERT:") and "bad00001" in line and "ok000001" not in line
    assert dirty.pii_at_output_total == 1 and dirty.pii_alert_trace_ids == ["bad00001"]


def test_the_alert_lists_a_few_trace_ids_and_counts_the_rest():
    many = summarize([row(trace_id=f"bad{i:05d}", pii_at_output=1) for i in range(8)], now=NOW)
    line = alerts.summary_alert(many)
    assert "bad00000" in line and "and 3 more" in line and "bad00007" not in line


def test_the_after_turn_warning_appears_only_for_a_dirty_row():
    assert alerts.turn_alert(row(pii_at_output=0)) is None
    assert alerts.turn_alert(None) is None
    assert alerts.turn_alert({"pii_at_output": "x"}) is None
    warning = alerts.turn_alert(row(trace_id="bad00001", pii_at_output=2))
    assert "ALERT" in warning and "bad00001" in warning and "2 finding" in warning


def test_a_turn_with_pii_at_output_logs_critical_and_writes_the_row(tmp_path, caplog):
    tracer = Tracer(tmp_path / "traces", tmp_path / "runs.jsonl")
    with caplog.at_level(logging.CRITICAL, logger="src"):
        with pytest.raises(AgentError):
            with tracer.start_turn("h", "conv-1", "t-1", trace_id="bad00002"):
                turn_metrics().add_pii_at_output(1)  # the Output Guard saw PII in the draft
                raise AgentError(ErrorKind.OUTPUT_BLOCKED)
    critical = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert len(critical) == 1 and "pii_at_output=1" in critical[0].getMessage() and "bad00002" in critical[0].getMessage()
    (saved,) = read_runs(tmp_path / "runs.jsonl")[0]
    assert saved["pii_at_output"] == 1 and saved["outcome"] == "failed"
    assert alerts.summary_alert(summarize([saved], now=NOW)) is not None


# prices in the config --------------------------------------------------------------------------------

def test_prices_are_null_by_default(project):
    assert load_settings(project / "config.yaml").llm.pricing_usd_per_1m is None


def test_prices_use_the_yaml_keys_in_and_out(project):
    (project / "config.yaml").write_text("llm:\n  pricing_usd_per_1m:\n    m1: {in: 0.3, out: 2.5}\n", encoding="utf-8")
    price = load_settings(project / "config.yaml").llm.pricing_usd_per_1m["m1"]
    assert (price.in_, price.out) == (0.3, 2.5)


@pytest.mark.parametrize("body", ["m1: {in: -1, out: 2}", "m1: {in: 1}", "m1: {in: 1, out: 2, extra: 3}"])
def test_a_bad_price_stops_startup(project, body):
    (project / "config.yaml").write_text(f"llm:\n  pricing_usd_per_1m:\n    {body}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="pricing_usd_per_1m"):
        load_settings(project / "config.yaml")
