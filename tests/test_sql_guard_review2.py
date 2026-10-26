"""Regressions for the second code review of the SQL Guard (plan 05): one group per finding.

Each was reproduced against the guard before it was fixed. The first review's regressions are in
`test_sql_guard_review.py`, the sealed query's in `test_guarded_query_seal.py`.
"""
import pytest
import sqlglot
from sqlglot import exp

from src.config import GuardsConfig
from src.errors import ConfigError, ErrorKind
from src.guards import sql_guard
from src.observability import Tracer
from src.observability.metrics import read_runs
from tests.guard_support import ALICE, CEO, REPO_ROOT, accepted, make_guard, refused, violation_rule


@pytest.fixture(scope="module")
def guard():
    return make_guard()


# 1. a call that names its project, its dataset or both -------------------------------------------------------------------

STORED_FUNCTIONS = [
    "SELECT `my-project`.mydataset.f(1) FROM products",
    "SELECT `my-project.mydataset.f`(1) FROM products",
    "SELECT my-project.mydataset.f(1) FROM products",
    "SELECT project.mydataset.f(1) FROM products",
    "SELECT `proj`.`ds`.`f`(1) FROM products",
    "SELECT `a b`.ds.f(1) FROM products",
    "SELECT `my-project`.`my dataset`.`my fn`(1) FROM products",
    "SELECT mydataset.f(1) FROM products",
    "SELECT `mydataset.f`(1) FROM products",
    "SELECT `my-dataset`.f(1) FROM products",
    "SELECT (`my-project`.mydataset.f(1)) FROM products",
    "SELECT COUNT(*) FROM products WHERE `my-project`.mydataset.f(name) > 0",
]


@pytest.mark.parametrize("sql", STORED_FUNCTIONS)
@pytest.mark.parametrize("user", [ALICE, CEO], ids=["brand manager", "ceo"])
def test_a_function_stored_in_a_dataset_is_refused_however_its_path_is_spelled(guard, sql, user):
    """A function in some project's dataset can read tables the allow-list does not cover. Only namespaces that are part
    of BigQuery itself may be called with a dot. The check reads the tree: a pattern over the generated text missed a
    project with a hyphen in its name, which is the usual kind."""
    assert violation_rule(guard, sql, user) == "DISALLOWED_FUNCTION"


@pytest.mark.parametrize("sql", [
    "SELECT SAFE.DIVIDE(SUM(sale_price), COUNT(*)) AS a FROM order_items",
    "SELECT NET.HOST('https://a.test/x') AS h FROM order_items",
    "SELECT NET.IP_FROM_STRING('1.2.3.4') AS ip FROM order_items",
    "SELECT KEYS.NEW_KEYSET('AEAD_AES_GCM_256') AS k FROM orders",
    "SELECT SAFE.PARSE_JSON('{}') AS j FROM orders",
    "SELECT COUNT(*) AS n, COALESCE(MAX(sale_price), 0) AS m FROM order_items",
])
def test_the_namespaces_that_belong_to_bigquery_still_work(guard, sql):
    """A typed function's `.name` is its first ARGUMENT in sqlglot (`Host('https://a.test/x').name`), which once made
    `NET.HOST('https://a.test/x')` look like a dotted path."""
    accepted(guard, sql, ALICE)


def test_the_deny_list_also_sees_a_name_behind_a_hyphenated_path(guard):
    assert violation_rule(guard, "SELECT `ML`.`PREDICT`(MODEL m, TABLE orders)") == "DISALLOWED_FUNCTION"
    assert violation_rule(guard, "SELECT `ml.generate_text`('x')") == "DISALLOWED_FUNCTION"


def test_a_quoted_function_name_is_not_changed_on_its_way_out(guard):
    """It used to come out upper-cased (`MY-PROJECT.MYDATASET.F`), which names a different function. Such a call is now
    refused, and an unknown plain name is still free to be upper-cased, because BigQuery does not distinguish case there."""
    assert violation_rule(guard, "SELECT `my-project.mydataset.f`(1) FROM products") == "DISALLOWED_FUNCTION"


# 2. SELECT DISTINCT -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "SELECT DISTINCT COUNT(*) AS n FROM orders GROUP BY gender",
    "SELECT DISTINCT state, COUNT(*) AS n FROM users GROUP BY state",
    "SELECT DISTINCT state FROM users GROUP BY state",
    "SELECT DISTINCT state FROM users GROUP BY state UNION ALL SELECT state FROM users GROUP BY state",
    "SELECT state FROM users GROUP BY state UNION ALL SELECT DISTINCT state FROM users GROUP BY state",
])
def test_the_customer_count_is_not_added_to_a_select_distinct(guard, sql):
    """It would change which rows are duplicates: two groups of the same size but with different numbers of distinct
    customers would no longer collapse into one row."""
    gq = accepted(guard, sql, ALICE)
    assert gq.demographic and gq.k_column is None and "__k_users" not in gq.sql


def test_a_plain_select_and_union_all_still_get_the_count(guard):
    assert accepted(guard, "SELECT gender, COUNT(*) AS n FROM orders GROUP BY gender", ALICE).k_column == "__k_users"
    gq = accepted(guard, "SELECT state FROM users GROUP BY state UNION ALL SELECT state FROM users GROUP BY state", ALICE)
    assert gq.k_column == "__k_users" and gq.sql.count("AS __k_users") == 2


# 4. a query sqlglot cannot write back is the model's to repair -----------------------------------------------------------------

def test_sqlglot_repairs_a_missing_argument_quietly_and_bigquery_is_left_to_refuse_it(guard):
    """`FORMAT_DATE(, x)` parses; sqlglot drops the empty argument and writes `FORMAT_DATE(x)`. The guard cannot know that is
    wrong, but the dry run can, and says so as a repairable syntax error. What matters here is that nothing crashes."""
    gq = accepted(guard, "SELECT FORMAT_DATE(, DATE(created_at)) AS ym FROM orders GROUP BY ym", ALICE)
    assert "FORMAT_DATE(DATE(created_at))" in gq.sql


@pytest.mark.parametrize("where,detail", [("_to_sql", "malformed"), ("generate_sql", "written back")])
def test_a_query_that_parses_but_cannot_be_written_is_a_repairable_syntax_error(monkeypatch, where, detail):
    def cannot_write(*args, **kwargs):
        raise AttributeError("a generator method met a malformed node")

    monkeypatch.setattr(sql_guard, where, cannot_write)
    err = refused(make_guard(), "SELECT COUNT(*) AS n FROM orders")
    assert err.kind == ErrorKind.SQL_SYNTAX and err.repairable and detail in err.detail


def test_a_failed_read_back_of_the_rewrite_is_a_repairable_syntax_error_too(monkeypatch):
    real = sqlglot
    calls = {"parse": 0}

    class Proxy:
        def __getattr__(self, name):
            return getattr(real, name)

        def parse(self, *args, **kwargs):
            calls["parse"] += 1
            if calls["parse"] == 2:  # the first reads the model's SQL; the second reads the rewrite back
                raise ValueError("the generated text does not parse")
            return real.parse(*args, **kwargs)

    monkeypatch.setattr(sql_guard, "sqlglot", Proxy())
    err = refused(make_guard(), "SELECT COUNT(*) AS n FROM orders")
    assert err.kind == ErrorKind.SQL_SYNTAX and err.repairable and "read back" in err.detail


def test_a_malformed_expression_is_not_counted_as_a_guard_block(tmp_path, monkeypatch):
    """It is the model's mistake, like any syntax error. Counted as a block it would inflate the alert built on blocks."""
    guard = make_guard()
    tracer = Tracer(tmp_path / "traces", tmp_path / "runs.jsonl")
    with tracer.start_turn("hash0001", "conv-1", "t-1"):
        with monkeypatch.context() as patch:
            patch.setattr(sql_guard, "generate_sql", lambda tree: (_ for _ in ()).throw(AttributeError("x")))
            refused(guard, "SELECT COUNT(*) AS n FROM orders")
        refused(guard, "SELECT email FROM users")
    assert read_runs(tracer.metrics_file)[0][0]["guard_blocks"]["sql"] == 1  # only the PII refusal


# 6. the generator the SQL goes through ------------------------------------------------------------------------------------------

def test_the_generator_is_sqlglots_own_list_with_one_helper_removed():
    from sqlglot.dialects.bigquery import BigQuery

    names = lambda fn: [getattr(item, "__name__", "") for item in sql_guard._transform_list(fn)]  # noqa: E731
    parent = names(BigQuery.Generator.TRANSFORMS[exp.Select])
    built = names(sql_guard._bigquery_dialect().Generator.TRANSFORMS[exp.Select])
    assert "_alias_ordered_group" in parent and "_alias_ordered_group" not in built
    assert built == [name for name in parent if name != "_alias_ordered_group"]  # nothing else dropped, nothing reordered


def test_a_sqlglot_whose_generator_looks_different_stops_the_guard_at_startup(monkeypatch):
    from sqlglot.dialects.bigquery import BigQuery

    monkeypatch.setitem(BigQuery.Generator.TRANSFORMS, exp.Select, lambda self, expression: "")  # no list of helpers to read
    sql_guard._bigquery_dialect.cache_clear()
    try:
        with pytest.raises(ConfigError) as err:
            make_guard()
        assert "requirements.txt" in err.value.fix
    finally:
        monkeypatch.undo()
        sql_guard._bigquery_dialect.cache_clear()
    make_guard()  # and it works again once sqlglot is as expected


def test_the_requirements_pin_the_sqlglot_the_generator_was_checked_against():
    import re

    line = next(l for l in (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines() if l.startswith("sqlglot"))
    low = re.search(r">=\s*(\d+)\.(\d+)", line)
    assert (int(low.group(1)), int(low.group(2))) >= (30, 24) and re.search(r"<\s*31", line), line
    assert sqlglot.__version__.split(".")[0] == "30"


# 7. a function name in the config must match whole -----------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["FARM_FINGERPRINT\n", "A\n", " A", "A ", "", "A.", ".A", "A..B", "A.*.B", "*", "A B", "A-B", "1A", "A\r\n"])
def test_a_function_name_with_stray_characters_is_refused_when_the_config_loads(name):
    """`$` also matches before a trailing newline, and a YAML block scalar (`- |`) ends in one. The name would then never
    match a function and the denial would silently do nothing."""
    with pytest.raises(ValueError):
        GuardsConfig(denied_functions=[name])


@pytest.mark.parametrize("name", ["FARM_FINGERPRINT", "farm_fingerprint", "ML.*", "a_b.c_d", "NS.SUB.FN", "X1"])
def test_a_proper_function_name_or_namespace_pattern_loads(name):
    assert GuardsConfig(denied_functions=[name]).denied_functions == [name]


def test_a_block_scalar_in_config_yaml_is_refused_with_the_file_and_key(tmp_path):
    from src.config import load_settings

    path = tmp_path / "config.yaml"
    path.write_text("guards:\n  denied_functions:\n    - |\n      FARM_FINGERPRINT\n", encoding="utf-8")
    with pytest.raises(ConfigError) as err:
        load_settings(path, load_env_file=False)
    assert "denied_functions" in err.value.message and "FARM_FINGERPRINT" in err.value.message
