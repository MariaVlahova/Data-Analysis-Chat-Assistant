"""S2: agent code never calls `BigQueryRunner.execute_query` (no dry run, byte cap, timeout, labels or row cap).

Every query goes through `SafeQueryRunner`. Only the data_access package may mention the method.
"""
import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
ALLOWED = SRC / "data_access"


def python_files_outside_data_access():
    return [path for path in SRC.rglob("*.py") if ALLOWED not in path.parents]


def test_the_scan_sees_the_source_tree():
    files = python_files_outside_data_access()
    assert len(files) > 10 and any(path.name == "main.py" for path in files)


def test_no_file_outside_data_access_mentions_execute_query():
    offenders = [
        str(path.relative_to(SRC)) for path in python_files_outside_data_access()
        if "execute_query" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_system_queries_skip_the_sql_guard_so_only_data_access_may_build_them():
    """`SystemQuery` is fixed SQL that bypasses the brand scope and the allow-list. If a later node built one from
    model text it would read every brand's rows, so nothing outside data_access/ may even name it."""
    offenders = [
        str(path.relative_to(SRC)) for path in python_files_outside_data_access()
        if "SystemQuery" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_the_unguarded_methods_are_gone_from_the_old_client():
    from src.data_access.bq_client import BigQueryRunner

    for name in ("execute_query", "get_table_schema"):
        assert not hasattr(BigQueryRunner, name), f"{name} bypasses the dry run, byte cap, timeout, labels and row cap"


def test_the_raw_client_is_private_to_the_safe_runner():
    from src.data_access.safe_runner import SafeQueryRunner

    assert not hasattr(SafeQueryRunner, "runner") and not hasattr(SafeQueryRunner, "client")


def bigquery_routes(source: str) -> list[str]:
    """Every way the source reaches the BigQuery client library or the old client, found in the syntax tree.

    A text search misses spellings such as `from google.cloud import storage, bigquery` or an import written over
    several lines. Strings that merely contain the name (a list of package names, a message) are not routes.
    """
    found: list[str] = []

    def dotted(node: ast.AST) -> str:
        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
        return ".".join(reversed(parts))

    def is_route(name: str) -> bool:
        return (
            name.startswith("google.cloud.bigquery")  # the library
            or name.endswith("data_access.bq_client")  # the module that creates the client
            or name.split(".")[-1] == "BigQueryRunner"  # the class, however it was reached
        )

    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names if is_route(alias.name)]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            found += [f"{base}.{alias.name}" for alias in node.names if is_route(f"{base}.{alias.name}") or is_route(base)]
        elif isinstance(node, ast.Call):  # importlib.import_module("google.cloud.bigquery") and __import__(...)
            name = dotted(node.func)
            if name.endswith("import_module") or name == "__import__":
                literal = node.args[0] if node.args else None
                if isinstance(literal, ast.Constant) and isinstance(literal.value, str) and is_route(literal.value):
                    found.append(literal.value)
        elif isinstance(node, (ast.Attribute, ast.Name)):  # `import google.cloud` then `google.cloud.bigquery.Client()`
            name = dotted(node) if isinstance(node, ast.Attribute) else node.id
            if is_route(name):
                found.append(name)
    return found


@pytest.mark.parametrize("source", [
    "import google.cloud.bigquery",
    "import google.cloud.bigquery as bq",
    "from google.cloud import bigquery",
    "from google.cloud import storage, bigquery",
    "from google.cloud import (\n    storage,\n    bigquery,\n)",
    "from google.cloud.bigquery import Client as C\nC()",
    "from google.cloud.bigquery.client import Client",
    "import importlib\nimportlib.import_module('google.cloud.bigquery')",
    "__import__('google.cloud.bigquery')",
    "import google.cloud\ngoogle.cloud.bigquery.Client()",
    "from src.data_access.bq_client import BigQueryRunner",
    "from src.data_access import bq_client\nbq_client.BigQueryRunner()",
])
def test_the_scanner_finds_every_spelling_of_a_route_to_the_client(source):
    assert bigquery_routes(source), source


@pytest.mark.parametrize("source", [
    "import google.auth",
    "from google.cloud import storage",
    "REQUIRED = [('google.cloud.bigquery', 'google-cloud-bigquery')]\nimportlib.import_module(REQUIRED[0][0])",
    "message = 'install google.cloud.bigquery first'",
    "from src.data_access.safe_runner import SafeQueryRunner",
])
def test_the_scanner_leaves_alone_code_that_does_not_reach_the_client(source):
    assert bigquery_routes(source) == [], source


def test_no_file_outside_data_access_reaches_the_bigquery_client():
    """The same bypass by another road: `bigquery.Client(...).query(sql)` skips every guard in SafeQueryRunner."""
    offenders = {
        str(path.relative_to(SRC)): routes for path in python_files_outside_data_access()
        if (routes := bigquery_routes(path.read_text(encoding="utf-8")))
    }
    assert offenders == {}
