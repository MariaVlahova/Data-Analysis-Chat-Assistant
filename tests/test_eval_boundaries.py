"""The lines the evaluation must not cross (plan 09). `eval/` is test tooling: nothing in the product imports it, and it reaches the data and the model only through
the doors the product has (the SQL Guard then the safe runner; the gateway through `open_session`), never around them.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVAL = ROOT / "eval"
SRC = ROOT / "src"


def imported(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def identifiers(path: Path) -> set[str]:
    """The names the code uses, not the words of its docstrings."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}


def test_the_scan_sees_both_trees():
    assert len(list(EVAL.glob("*.py"))) >= 9 and len(list(SRC.rglob("*.py"))) > 50


def test_nothing_under_src_imports_the_evaluation():
    offenders = [str(path.relative_to(ROOT)) for path in SRC.rglob("*.py") if any(name == "eval" or name.startswith("eval.") for name in imported(path))]
    assert offenders == []


def test_the_evaluation_does_not_import_the_bypass_the_scenario_runner_alone_may_use():
    for path in EVAL.glob("*.py"):
        assert "src.data_access.reference" not in imported(path), path.name
        assert "ReferenceQueries" not in identifiers(path), path.name


def test_the_evaluation_never_asks_the_scenario_truth_for_a_figure():
    """`Truth.number` and `Truth.strings` run SQL outside the guards. The harness hands out only `Macros`, and nothing in `eval/` calls either through a
    `Facts.truth` it was given."""
    for path in EVAL.glob("*.py"):
        attributes = {node.attr for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))) if isinstance(node, ast.Attribute)}
        assert not attributes & {"number", "strings", "_reference", "_truth"}, path.name


def test_the_harness_hands_out_the_macros_and_not_the_truth():
    from eval.harness import Harness
    from src.scenarios.runner import ScenarioRunner
    from src.scenarios.truth import Macros

    assert not hasattr(Harness, "truth") and not hasattr(ScenarioRunner, "truth")
    assert not hasattr(Macros, "number") and not hasattr(Macros, "strings")


def test_the_evaluation_runs_sql_only_through_the_guard_and_the_safe_runner():
    for path in EVAL.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "execute_query" not in text and "SystemQuery" not in text and ".issue(" not in text, path.name
        assert not any(name.startswith(("google.cloud", "duckdb")) for name in imported(path)), path.name


def test_the_evaluation_reaches_the_model_only_through_the_app_not_an_sdk():
    for path in EVAL.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "generate_content" not in text and "chat/completions" not in text, path.name
        assert not any(name.startswith(("google.genai", "openai")) for name in imported(path)), path.name


def test_evalkit_is_used_by_the_evaluation_metrics_and_scoring():
    users = {path.name for path in EVAL.glob("*.py") if any(name == "evalkit" or name.startswith("evalkit.") for name in imported(path))}
    assert {"metrics.py", "evaluate.py"} <= users


# the chat's /eval (src/evaluation) ----------------------------------------------------------------------------------------------------------------

EVALUATION = SRC / "evaluation"


def test_only_the_live_evaluation_package_of_the_product_imports_evalkit():
    """`evalkit` is the generic scoring framework. The product reaches it from one package, so the rest of `src/` stays free of it."""
    offenders = [
        str(path.relative_to(ROOT)) for path in SRC.rglob("*.py")
        if EVALUATION not in path.parents and any(name == "evalkit" or name.startswith("evalkit.") for name in imported(path))
    ]
    assert offenders == []
    assert any(any(name.startswith("evalkit") for name in imported(path)) for path in EVALUATION.glob("*.py"))  # and it does use it


def test_the_live_evaluation_reaches_the_model_only_through_the_gateway_and_never_the_data_or_the_scenario_bypass():
    for path in EVALUATION.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "generate_content" not in text and "chat/completions" not in text and "execute_query" not in text and "SystemQuery" not in text, path.name
        names = imported(path)
        assert not any(name.startswith(("google.genai", "google.cloud", "openai", "duckdb", "src.scenarios", "src.data_access")) for name in names), path.name
        assert not any(name == "eval" or name.startswith("eval.") for name in names), path.name  # `evalkit` is allowed, `eval` is test tooling
        assert "ReferenceQueries" not in identifiers(path), path.name


def test_the_judge_call_is_one_gateway_call_of_the_judge_step():
    tree = ast.parse((EVALUATION / "judging.py").read_text(encoding="utf-8"))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "generate"
        and node.args and isinstance(node.args[0], ast.Attribute) and node.args[0].attr == "JUDGE"
    ]
    assert len(calls) == 1
    assert not [path.name for path in EVALUATION.glob("*.py") if path.name != "judging.py" and ".generate(" in path.read_text(encoding="utf-8")]  # no other file calls a model


def test_the_chat_does_not_load_the_scoring_framework_until_eval_is_typed(tmp_path):
    import subprocess
    import sys

    script = (
        f"import sys; sys.path.insert(0, {str(ROOT)!r})\n"
        "import src.cli.main, src.cli.commands\n"
        "print('LOADED:', [name for name in ('evalkit', 'eval', 'openpyxl', 'src.evaluation.live', 'src.evaluation.judging') if name in sys.modules])\n"
    )
    result = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, text=True, cwd=tmp_path, timeout=120)
    assert result.returncode == 0, result.stderr
    assert "LOADED: []" in result.stdout
