"""A `GuardedQuery` cannot be forged (plan 05, code review).

Being an instance of the class is no proof that the SQL Guard ran: `dataclasses.replace(gq, sql=...)` returns an
instance too. So the guard seals every field with a key that exists only in its process, and the runner refuses a
query whose seal no longer matches. See `src/guards/query.py`.
"""
import copy
import dataclasses
import pickle
import subprocess
import sys

import pandas as pd
import pytest

from src.data_access.safe_runner import SafeQueryRunner
from src.errors import AgentError
from src.guards.query import ArrayParam, GuardedQuery
from tests.fake_bigquery import FakeRunner
from tests.guard_support import ALICE, REPO_ROOT, make_guard, make_settings

SQL = "SELECT COUNT(*) AS n FROM orders"


@pytest.fixture(scope="module")
def gq() -> GuardedQuery:
    return make_guard().check(SQL, ALICE)


def runner(client) -> SafeQueryRunner:
    return SafeQueryRunner(make_settings(), FakeRunner(client))


def fields_of(query: GuardedQuery) -> dict:
    return {f.name: getattr(query, f.name) for f in dataclasses.fields(query) if f.name != "seal"}


def test_a_query_from_the_guard_is_authentic(gq):
    assert gq.authentic and len(gq.seal) == 64


def test_the_seal_is_not_printed(gq):
    assert gq.seal not in repr(gq)


@pytest.mark.parametrize("name,value", [
    ("sql", "SELECT * FROM `bigquery-public-data.thelook_ecommerce.users`"),
    ("params", ()),
    ("tables", frozenset({"users"})),
    ("id_columns", ("user_id",)),
    ("per_customer", True),
    ("demographic", True),
    ("k_column", "__k_users"),
    ("limit", 5),
    ("rewrites", ()),
    ("fingerprint", "0" * 64),
    ("scope_label", "all brands"),
])
def test_changing_any_field_of_a_query_breaks_the_seal(gq, name, value):
    """The runner reads `sql` and `params`; the Result Guard reads the tags. Changing a tag (say `demographic=False`)
    would weaken the Result Guard as surely as changing the SQL would weaken the SQL Guard."""
    assert getattr(gq, name) != value
    forged = dataclasses.replace(gq, **{name: value})
    assert isinstance(forged, GuardedQuery) and not forged.authentic


def test_a_changed_brand_list_breaks_the_seal(gq):
    forged = dataclasses.replace(gq, params=(ArrayParam("brands", "STRING", ("Some Other Brand",)),))
    assert not forged.authentic


def test_a_query_made_by_hand_is_not_authentic(gq):
    by_hand = GuardedQuery(**fields_of(gq))
    assert not by_hand.authentic
    assert not GuardedQuery(**fields_of(gq), seal="0" * 64).authentic


def test_writing_a_field_behind_the_dataclass_is_still_caught(gq):
    tampered = copy.copy(gq)
    object.__setattr__(tampered, "sql", "SELECT 1")
    assert not tampered.authentic and gq.authentic


def test_the_class_cannot_be_subclassed():
    with pytest.raises(TypeError):
        class Mine(GuardedQuery):  # a subclass could define `authentic` as always True
            pass


def test_copies_and_pickles_inside_the_process_stay_authentic(gq):
    """Plan 07 may keep a query in state, or hand it to another node. The same process can still run it."""
    assert copy.copy(gq).authentic and copy.deepcopy(gq).authentic
    assert pickle.loads(pickle.dumps(gq)).authentic
    assert dataclasses.replace(gq).authentic  # a replace that changes nothing changes nothing


def test_a_query_from_another_process_is_not_authentic(gq):
    """The key is made when the module loads and is written nowhere. A query that crossed a process boundary (a saved
    checkpoint, say) is therefore run through the guard again, not trusted."""
    code = (
        "import pickle, sys\n"
        "sys.path.insert(0, '.')\n"
        "from tests.guard_support import ALICE, make_guard\n"
        f"sys.stdout.buffer.write(pickle.dumps(make_guard().check({SQL!r}, ALICE)))\n"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, check=True)
    foreign = pickle.loads(result.stdout)
    assert fields_of(foreign) == fields_of(gq)  # the same query ...
    assert foreign.seal != gq.seal and not foreign.authentic  # ... but sealed with another key


def test_the_runner_runs_an_authentic_query(fake_bigquery, gq):
    fake_bigquery.add("UNNEST(@brands)", df=pd.DataFrame({"n": [3]}))
    assert int(runner(fake_bigquery).run(gq).df["n"].iloc[0]) == 3


@pytest.mark.parametrize("how", ["replace_sql", "replace_params", "by_hand", "tampered"])
def test_the_runner_refuses_a_forged_query_before_any_job_starts(fake_bigquery, gq, how):
    if how == "replace_sql":
        forged = dataclasses.replace(gq, sql="SELECT * FROM `bigquery-public-data.thelook_ecommerce.users`", params=())
    elif how == "replace_params":
        forged = dataclasses.replace(gq, params=())
    elif how == "by_hand":
        forged = GuardedQuery(**fields_of(gq))
    else:
        forged = copy.copy(gq)
        object.__setattr__(forged, "sql", "SELECT 1")
    safe = runner(fake_bigquery)
    for call in (safe.run, safe.dry_run):
        with pytest.raises(AgentError) as err:
            call(forged)
        assert err.value.rule_id == "UNGUARDED_QUERY"
    assert fake_bigquery.jobs == []  # not even a dry run
