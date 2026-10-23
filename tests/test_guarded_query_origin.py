"""Only the SQL Guard makes a `GuardedQuery` (plan 05).

`SafeQueryRunner` accepts the type as proof that the checks ran (see `ACCEPTED_QUERY_TYPES`). If another module could
make one from model text, that proof would mean nothing. Tests may make one with `GuardedQuery.issue`; nothing in
`src/` may except the guard. The seal in `src/guards/query.py` is the second line: a query that was not sealed by the
guard, or was changed afterwards, is refused by the runner (`tests/test_guarded_query_seal.py`).
"""
import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
MAY_BUILD = SRC / "guards" / "sql_guard.py"


def _names_guarded_query(node: ast.AST) -> bool:
    return (isinstance(node, ast.Name) and node.id == "GuardedQuery") or (isinstance(node, ast.Attribute) and node.attr == "GuardedQuery")


def constructions(source: str) -> list[int]:
    """Lines where the source makes a `GuardedQuery`: `GuardedQuery(...)` or `GuardedQuery.issue(...)`, however the
    name was imported."""
    lines = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            func = node.func
            if _names_guarded_query(func) or (isinstance(func, ast.Attribute) and func.attr == "issue" and _names_guarded_query(func.value)):
                lines.append(node.lineno)
    return lines


def test_the_scanner_sees_every_spelling_of_a_construction():
    for source in (
        "GuardedQuery(sql='x')",
        "GuardedQuery.issue(sql='x')",
        "from src.guards.query import GuardedQuery\nGuardedQuery(sql='x')",
        "from src.guards import query\nquery.GuardedQuery(sql='x')",
        "from src.guards import query\nquery.GuardedQuery.issue(sql='x')",
        "import src.guards.query as q\nq.GuardedQuery(**fields)",
        "def f():\n    return GuardedQuery(*args)",
    ):
        assert constructions(source), source
    for source in ("isinstance(x, GuardedQuery)", "ACCEPTED = (SystemQuery, GuardedQuery)", "x: GuardedQuery = y", "message = 'GuardedQuery(x)'",
                   "gq.issue(x)", "other.issue(x)"):
        assert constructions(source) == [], source


def test_only_the_sql_guard_builds_a_guarded_query():
    files = list(SRC.rglob("*.py"))
    assert len(files) > 20 and MAY_BUILD in files
    offenders = {
        str(path.relative_to(SRC)): lines for path in files
        if path != MAY_BUILD and (lines := constructions(path.read_text(encoding="utf-8")))
    }
    assert offenders == {}


def test_the_sql_guard_builds_exactly_one_in_one_place():
    assert len(constructions(MAY_BUILD.read_text(encoding="utf-8"))) == 1


def test_the_guards_package_never_imports_the_bigquery_library():
    """`guards.query` must stay free of it: the runner turns `ArrayParam` into BigQuery's own type."""
    for path in (SRC / "guards").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "google.cloud" not in text and "bq_client" not in text, path.name


def test_importing_the_query_types_does_not_import_sqlglot():
    import subprocess
    import sys

    code = "import sys; import src.guards.query; sys.exit(1 if 'sqlglot' in sys.modules else 0)"
    assert subprocess.run([sys.executable, "-c", code], cwd=SRC.parent).returncode == 0
