"""Reference queries (`src/data_access/reference.py`): the right answer from the data, outside the guards, but never outside the runner's limits."""
import ast
from pathlib import Path

import pytest

from src.config import load_settings
from src.data_access.reference import ReferenceQueries
from src.errors import AgentError, ErrorKind

SRC = Path(__file__).resolve().parent.parent / "src"
DATASET = "bigquery-public-data.thelook_ecommerce"


@pytest.fixture
def reference(local_settings):
    return ReferenceQueries(local_settings)


# qualifying the tables ---------------------------------------------------------------------------------------------------

def test_a_bare_table_name_is_spelled_with_the_dataset(reference):
    sql = reference.qualify("SELECT COUNT(*) FROM order_items oi JOIN products p ON p.id = oi.product_id")
    assert "`bigquery-public-data`.thelook_ecommerce.order_items" in sql and "`bigquery-public-data`.thelook_ecommerce.products" in sql


def test_a_table_that_already_has_a_dataset_a_with_clause_and_a_foreign_name_are_left_alone(reference):
    assert reference.qualify(f"SELECT 1 FROM thelook_ecommerce.orders") == "SELECT 1 FROM thelook_ecommerce.orders"
    with_clause = reference.qualify("WITH orders AS (SELECT 1 AS order_id) SELECT * FROM orders")
    assert "thelook_ecommerce" not in with_clause
    assert "thelook_ecommerce" not in reference.qualify("SELECT * FROM some_other_table")


def test_a_string_that_looks_like_a_table_is_not_changed(reference):
    assert reference.qualify("SELECT 'order_items' AS t") == "SELECT 'order_items' AS t"


def test_sql_that_cannot_be_read_is_a_repairable_syntax_error(reference):
    with pytest.raises(AgentError) as caught:
        reference.qualify("SELECT FROM WHERE (")
    assert caught.value.kind is ErrorKind.SQL_SYNTAX


# running -------------------------------------------------------------------------------------------------------------------

def test_a_number_a_list_and_a_frame(reference, local_db):
    assert reference.number("SELECT COUNT(*) FROM users") == local_db.rows["users"]
    states = reference.strings("SELECT DISTINCT state FROM users ORDER BY state")
    assert "California" in states and all(isinstance(state, str) for state in states)
    assert list(reference.frame("SELECT status, COUNT(*) AS n FROM order_items GROUP BY status").columns) == ["status", "n"]


def test_a_query_with_no_row_has_no_number_and_an_empty_list(reference):
    with pytest.raises(ValueError, match="no number"):
        reference.number("SELECT id FROM users WHERE id < 0")
    assert reference.strings("SELECT state FROM users WHERE id < 0") == []


def test_only_a_select_runs(reference):
    for sql in ("DELETE FROM orders WHERE TRUE", "DROP TABLE users", "INSERT INTO orders (order_id) VALUES (1)"):
        with pytest.raises(AgentError) as caught:
            reference.number(sql)
        assert caught.value.kind is ErrorKind.GUARD_VIOLATION and "is not allowed" in caught.value.detail


def test_it_does_not_apply_the_brand_filter_or_the_allow_list_which_is_the_point(reference):
    """It reads every customer's e-mail address: the guards would refuse that, and the test needs the truth the guards protect."""
    assert reference.number("SELECT COUNT(*) FROM users WHERE email LIKE '%@example.com'") > 1000


def test_the_byte_cap_of_the_runner_still_applies(local_settings):
    tiny = local_settings.model_copy(update={"bigquery": local_settings.bigquery.model_copy(update={"max_bytes_billed": 1000})})
    with pytest.raises(AgentError) as caught:
        ReferenceQueries(tiny).number("SELECT COUNT(*) FROM order_items")
    assert caught.value.kind is ErrorKind.QUERY_TOO_EXPENSIVE


# who may use it --------------------------------------------------------------------------------------------------------------

def reference_routes(source: str) -> list[str]:
    """Every way the source reaches the reference queries, found in the syntax tree: `import src.data_access.reference`, a `from` import of the
    module or of the class, however it is spelled."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names if alias.name.endswith("data_access.reference")]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.endswith("data_access.reference"):
                found.append(module)
            found += [f"{module}.{alias.name}" for alias in node.names if alias.name in ("reference", "ReferenceQueries") and "data_access" in module]
    return found


@pytest.mark.parametrize("source", [
    "from src.data_access.reference import ReferenceQueries",
    "from src.data_access.reference import ReferenceQueries as R",
    "import src.data_access.reference",
    "import src.data_access.reference as ref",
    "from src.data_access import reference",
    "from src.data_access import safe_runner, reference",
])
def test_the_scan_finds_every_spelling_of_the_import(source):
    assert reference_routes(source)


@pytest.mark.parametrize("source", [
    "from src.data_access.safe_runner import SafeQueryRunner", "from src.scenarios.truth import Truth", "message = 'src.data_access.reference'",
    "from src.data_access import catalog",
])
def test_the_scan_leaves_other_code_alone(source):
    assert reference_routes(source) == []


def test_only_the_scenario_runner_reads_the_data_outside_the_guards():
    """Like `SystemQuery`, reference queries have no brand filter and no allow-list. Nothing in the agent, the guards or the tools may import them."""
    offenders = {}
    for path in SRC.rglob("*.py"):
        relative = path.relative_to(SRC)
        if relative.parts[0] in ("scenarios", "data_access"):
            continue
        if routes := reference_routes(path.read_text(encoding="utf-8")):
            offenders[str(relative)] = routes
    assert offenders == {}


def test_the_runner_does_import_it():
    assert reference_routes((SRC / "scenarios" / "truth.py").read_text(encoding="utf-8"))
