"""Smoke tests for evalkit (plan 09's scoring framework), and the rule that keeps it generic.

Not a full suite; enough to catch a broken import or a broken wiring between
schema/session/context/registry/graded/runner before you build on top of it.

The boundary: `src/` never imports `evalkit` (it is test tooling, not product code) and `evalkit/` never imports `src`
(it stays reusable). The project's own metrics and adapters live in `eval/`.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from evalkit.config import EvaluationConfig
from evalkit.context import ValidationContext
from evalkit.judge import RubricVerdict
from evalkit.registry import MetricRegistry, result_helpers
from evalkit.rubrics import load_rubrics
from evalkit.runner import EvaluationRunner, compare
from evalkit.schema import Focus, Layer, MetricResult, Verdict
from evalkit.session import EvalEvent, EvalSession
from evalkit.statistics import bootstrap_ci, cohens_kappa
from evalkit.testing import scripted_judge

PIPELINE = "chat"


def _session(session_id: str, answer: str) -> EvalSession:
    return EvalSession(
        session_id=session_id,
        pipeline=PIPELINE,
        events=[
            EvalEvent(type="user", author="user", content="hello"),
            EvalEvent(type="model", author="agent", content=answer),
        ],
        state={"final_answer": answer},
    )


def test_metric_result_normalization() -> None:
    r = MetricResult.from_bool("m", Layer.STATIC, "s", True, "ok", focus=Focus.CORRECTNESS)
    assert r.score == 1.0
    assert r.verdict is Verdict.PASS
    assert r.decided

    na = MetricResult.not_applicable("m", Layer.STATIC, "s", "no data", focus=Focus.CORRECTNESS)
    assert na.score is None
    assert not na.decided

    with pytest.raises(ValueError):
        MetricResult.not_exercised("m", Layer.STATIC, "s", "precondition", "", focus=Focus.CORRECTNESS)


def test_registry_pipeline_scoping() -> None:
    registry = MetricRegistry(Layer.STATIC)
    ok, na, _ = result_helpers("has_answer", Layer.STATIC, "generation", Focus.CORRECTNESS)

    @registry.register("has_answer", stage="generation", pipelines=("chat",), focus=Focus.CORRECTNESS)
    def check(ctx: ValidationContext):
        answer = str(ctx.state.get("final_answer") or "")
        return ok(bool(answer.strip()), "has answer" if answer.strip() else "empty")

    ctx_chat = ValidationContext(session=_session("s1", "hi"))
    ctx_other = ValidationContext(session=EvalSession(session_id="s2", pipeline="ingestion"))

    results = registry.run(ctx_chat)
    assert results["has_answer"].verdict is Verdict.PASS
    # A session on a pipeline the metric was not registered for scores nothing.
    assert registry.run(ctx_other) == {}


def test_session_turns_split_on_user_events() -> None:
    session = EvalSession(
        session_id="multi",
        pipeline=PIPELINE,
        events=[
            EvalEvent(type="user", author="user", content="q1"),
            EvalEvent(type="model", author="agent", content="a1"),
            EvalEvent(type="user", author="user", content="q2"),
            EvalEvent(type="model", author="agent", content="a2"),
        ],
    )
    turns = session.turns
    assert len(turns) == 2
    assert turns[0].prompt == "q1" and turns[0].answer == "a1"
    assert turns[1].prompt == "q2" and turns[1].answer == "a2"


def test_load_rubrics_and_judge_scoring(tmp_path: Path) -> None:
    csv_path = tmp_path / "rubrics.csv"
    csv_path.write_text(
        "registry,key,name,description,passing_criteria,failing_criteria,applicability_hint,"
        "category,score_interpretation,stage,pipelines,threshold\n"
        'JUDGE_RUBRICS,tone,Tone,"Is the tone appropriate?",Warm and clear,Cold or curt,'
        'Always,correctness,"1-4",generation,chat,0.5\n',
        encoding="utf-8",
    )
    rubrics = load_rubrics(csv_path)
    assert set(rubrics) == {"tone"}
    assert rubrics["tone"].stage == "generation"
    assert rubrics["tone"].threshold == 0.5

    judge = scripted_judge(lambda prompt: RubricVerdict(score=4, reason="fine"))
    runner = EvaluationRunner(
        config=EvaluationConfig(layers=frozenset({Layer.JUDGE}), gate_judge_on_static=False),
        judge=judge,
        rubrics=rubrics,
    )
    report = runner.run([_session("s1", "hello there")], run_id="test")
    summary = report.metric("tone")
    assert summary is not None
    assert summary.n_scored == 1
    assert summary.mean == pytest.approx(1.0)  # (4-1)/3 = 1.0


def test_load_rubrics_from_xlsx(tmp_path: Path) -> None:
    """The same rubric, written as a workbook instead of a CSV, parses to the same `Rubric`."""
    from openpyxl import Workbook

    from evalkit.rubrics import EXPECTED_COLUMNS

    xlsx_path = tmp_path / "rubrics.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(EXPECTED_COLUMNS)
    ws.append(
        ["JUDGE_RUBRICS", "tone", "Tone", "Is the tone appropriate?", "Warm and clear", "Cold or curt",
         "Always", "correctness", "1-4", "generation", "chat", 0.5]
    )
    ws.append([None] * len(EXPECTED_COLUMNS))  # a trailing blank row, as Excel often leaves one
    wb.save(xlsx_path)

    rubrics = load_rubrics(xlsx_path)
    assert set(rubrics) == {"tone"}
    assert rubrics["tone"].stage == "generation"
    assert rubrics["tone"].threshold == 0.5


def test_rubrics_xlsx_shipped_with_evalkit_loads() -> None:
    """evalkit/rubrics.xlsx (the project's own judge rubrics) is well-formed and loads cleanly."""
    path = Path(__file__).resolve().parent.parent / "evalkit" / "rubrics.xlsx"
    rubrics = load_rubrics(path)
    assert {"tool_use_quality", "final_response_quality", "groundedness", "safety",
            "answer_completeness", "hitl_appropriateness"} <= set(rubrics)
    for rubric in rubrics.values():
        assert rubric.as_prompt_block()  # every row renders to a judge/human-ready prompt block


def test_gate_and_floor() -> None:
    registry = MetricRegistry(Layer.STATIC)

    @registry.register("acl_ok", stage="retrieval", pipelines=(PIPELINE,), focus=Focus.SECURITY)
    def acl(ctx: ValidationContext):
        ok = ctx.ref("breach") is not True
        return MetricResult.from_bool(
            "acl_ok", Layer.STATIC, "retrieval", ok, "clean" if ok else "breach detected", focus=Focus.SECURITY
        )

    config = EvaluationConfig(
        layers=frozenset({Layer.STATIC}),
        gate_floors={"acl_ok": 1.0},
    )
    runner = EvaluationRunner(config=config, static_metrics=registry)
    report = runner.run(
        [_session("s1", "x"), _session("s2", "y")],
        references={"s1": {"breach": False}, "s2": {"breach": True}},
        run_id="gate-test",
    )
    assert report.gate["verdict"] == "FAIL"
    assert any(b["metric"] == "acl_ok" for b in report.gate["floor_breaches"])


def test_compare_detects_regression() -> None:
    registry = MetricRegistry(Layer.STATIC)

    @registry.register("m", stage="s", pipelines=(PIPELINE,), focus=Focus.CORRECTNESS)
    def m(ctx: ValidationContext):
        ok = bool(ctx.ref("ok"))
        return MetricResult.from_bool("m", Layer.STATIC, "s", ok, "ok" if ok else "fail", focus=Focus.CORRECTNESS)

    config = EvaluationConfig(layers=frozenset({Layer.STATIC}))
    runner = EvaluationRunner(config=config, static_metrics=registry)

    baseline = runner.run(
        [_session(f"b{i}", "x") for i in range(5)],
        references={f"b{i}": {"ok": True} for i in range(5)},
        run_id="baseline",
    )
    candidate = runner.run(
        [_session(f"c{i}", "x") for i in range(5)],
        references={f"c{i}": {"ok": i != 0} for i in range(5)},  # one flips to fail
        run_id="candidate",
    )
    result = compare(baseline, candidate, min_delta=0.05)
    assert result["metrics"]["m"]["regression"] is True


def test_graded_registry_skipped_as_proxy_when_every_static_metric_passes() -> None:
    static = MetricRegistry(Layer.STATIC)

    @static.register("acl_ok", stage="retrieval", pipelines=(PIPELINE,), focus=Focus.SECURITY)
    def acl(ctx: ValidationContext):
        return MetricResult.from_bool("acl_ok", Layer.STATIC, "retrieval", True, "clean", focus=Focus.SECURITY)

    graded = MetricRegistry(Layer.STANDARD)

    @graded.register("expensive", stage="generation", pipelines=(PIPELINE,), focus=Focus.CORRECTNESS)
    def expensive(ctx: ValidationContext):
        raise AssertionError("must not be called when the deterministic layers already passed")

    config = EvaluationConfig(layers=frozenset({Layer.STATIC, Layer.STANDARD}))
    runner = EvaluationRunner(config=config, static_metrics=static, graded_metrics=graded)
    report = runner.run([_session("s1", "hi")], run_id="proxy-test")

    summary = report.metric("expensive")
    assert summary is not None
    assert summary.n_pass_by_proxy == 1
    assert summary.n_scored == 0


def test_graded_registry_runs_when_a_static_metric_fails() -> None:
    static = MetricRegistry(Layer.STATIC)

    @static.register("acl_ok", stage="retrieval", pipelines=(PIPELINE,), focus=Focus.SECURITY)
    def acl(ctx: ValidationContext):
        return MetricResult.from_bool("acl_ok", Layer.STATIC, "retrieval", False, "breach", focus=Focus.SECURITY)

    graded = MetricRegistry(Layer.STANDARD)

    @graded.register("checked", stage="generation", pipelines=(PIPELINE,), focus=Focus.CORRECTNESS)
    def checked(ctx: ValidationContext):
        return MetricResult.from_bool(
            "checked", Layer.STANDARD, "generation", True, "ran for real", focus=Focus.CORRECTNESS
        )

    config = EvaluationConfig(layers=frozenset({Layer.STATIC, Layer.STANDARD}))
    runner = EvaluationRunner(config=config, static_metrics=static, graded_metrics=graded)
    report = runner.run([_session("s1", "hi")], run_id="run-test")

    summary = report.metric("checked")
    assert summary is not None
    assert summary.n_scored == 1
    assert summary.n_pass_by_proxy == 0


def test_graded_registry_runs_with_no_rubrics_and_no_judge_client() -> None:
    """A project running only a graded_metrics registry has no evalkit
    JudgeClient of its own -- the old unconditional ``assert self.judge is
    not None`` would be the first thing it hit. It must not be.
    """
    graded = MetricRegistry(Layer.STANDARD)

    @graded.register("checked", stage="generation", pipelines=(PIPELINE,), focus=Focus.CORRECTNESS)
    def checked(ctx: ValidationContext):
        return MetricResult.from_bool(
            "checked", Layer.STANDARD, "generation", True, "ran for real", focus=Focus.CORRECTNESS
        )

    config = EvaluationConfig(layers=frozenset({Layer.STANDARD}), gate_judge_on_static=False)
    runner = EvaluationRunner(config=config, graded_metrics=graded)
    report = runner.run([_session("s1", "hi")], run_id="no-judge-test")

    summary = report.metric("checked")
    assert summary is not None
    assert summary.n_scored == 1


def test_graded_registry_rejects_a_free_layer() -> None:
    with pytest.raises(ValueError):
        EvaluationRunner(graded_metrics=MetricRegistry(Layer.STATIC))


def test_statistics_helpers() -> None:
    ci = bootstrap_ci([1.0, 1.0, 1.0, 0.0], resamples=200)
    assert ci is not None
    assert 0.0 <= ci.low <= ci.high <= 1.0

    kappa = cohens_kappa(["a", "b", "a", "a"], ["a", "b", "b", "a"])
    assert kappa is not None


# the boundary ------------------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent


def imported_packages(path: Path) -> set[str]:
    """The top-level package of every import in the file, from the syntax tree."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


def test_the_scan_sees_both_trees() -> None:
    assert len(list((ROOT / "src").rglob("*.py"))) > 10 and len(list((ROOT / "evalkit").glob("*.py"))) >= 10


def test_only_the_live_evaluation_package_under_src_imports_evalkit() -> None:
    """The chat's `/eval` runs the judge with the same rubrics and scorer as the offline suites, so one package of the product reaches `evalkit`.
    The rest of `src/` stays free of it (`tests/test_eval_boundaries.py` also checks that this package uses no model SDK and no data access)."""
    live = ROOT / "src" / "evaluation"
    offenders = [
        str(path.relative_to(ROOT)) for path in (ROOT / "src").rglob("*.py") if live not in path.parents and "evalkit" in imported_packages(path)
    ]
    assert offenders == []


def test_evalkit_imports_nothing_from_this_project() -> None:
    offenders = [
        str(path.relative_to(ROOT)) for path in (ROOT / "evalkit").glob("*.py")
        if imported_packages(path) & {"src", "eval", "tests"}
    ]
    assert offenders == []
