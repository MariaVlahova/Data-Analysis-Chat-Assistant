"""What the graph keeps (plan 07, CLAUDE.md rules): plain state, no raw rows, no real customer IDs, no pseudonymizer, and an
answer the Output Guard held back stored nowhere. Canary values are planted and then searched for everywhere the graph writes."""
import copy
import json
import pickle
import re

import pandas as pd
import pytest

from src.errors import ErrorKind
from src.llm.routing import Step
from src.observability.tracing import exchange_file
from tests.agent_support import composer_reply, guard_reply, make_real_gateway_rig, make_rig, sql_reply

REAL_IDS = (987654321, 123456789)
RANKED_SQL = "SELECT oi.user_id, SUM(oi.sale_price) AS spend FROM order_items oi GROUP BY oi.user_id ORDER BY spend DESC LIMIT 10"
EMAIL = "jane.roe@example.test"


@pytest.fixture
def rig(project, fake_bigquery):
    fake_bigquery.add("spend", df=pd.DataFrame({"user_id": list(REAL_IDS), "spend": [500.0, 300.0]}))
    return make_rig(project, fake_bigquery=fake_bigquery)


def every_checkpoint(rig):
    return [json.dumps(s.values, default=str) for s in rig.agent.graph.get_state_history({"configurable": {"thread_id": rig.conversation_id}})]


def everything_on_disk(rig):
    blobs = [path.read_text(encoding="utf-8", errors="replace") for path in (rig.settings.data_path).rglob("*") if path.is_file() and path.suffix in (".jsonl", ".log")]
    return blobs


def ranked_turn(rig, compose=None):
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply(standalone="Who are my top customers?"))
    rig.gateway.queue(Step.SQL_WRITE, sql_reply(RANKED_SQL, "Top customers by their spend on your brands."))
    label = lambda call: composer_reply(compose or f"{re.search(r'Customer #\d+', call.contents).group(0)} spent 500.")  # noqa: E731 - the fake ID is random
    rig.gateway.queue(Step.COMPOSER, label)
    return rig.ask("Who are my top customers?")


def test_a_ranked_customer_list_shows_fake_ids_and_the_notices_and_never_a_real_id(rig):
    result = ranked_turn(rig)
    assert result.outcome == "answered", result
    assert re.search(r"Customer #\d{5,7} spent 500\.", result.text)
    assert "Customer numbers are labels for this session only." in result.text and "counts only purchases in your brands" in result.text
    assert not any(str(real) in result.text for real in REAL_IDS)


def test_no_real_customer_id_is_in_any_checkpoint_trace_log_prompt_or_row(rig):
    ranked_turn(rig)
    blobs = every_checkpoint(rig) + everything_on_disk(rig) + [json.dumps(rig.last_row)]
    blobs += [str(call.contents) + call.system for call in rig.gateway.calls]
    assert len(every_checkpoint(rig)) > 5  # the history of the whole turn was searched
    for real in REAL_IDS:
        assert not any(str(real) in blob for blob in blobs), real


def test_the_state_is_plain_data_that_serializes_with_no_helper(rig):
    ranked_turn(rig)
    values = rig.agent.graph.get_state({"configurable": {"thread_id": rig.conversation_id}}).values
    json.dumps(values)  # no default=: every value is a plain JSON type, so a checkpointer can save it
    assert pickle.loads(pickle.dumps(values)) == values and copy.deepcopy(values) == values
    for name, result in values["results"].items():
        guarded = result["guarded"]
        assert set(guarded) >= {"columns", "rows", "notices", "summary"} and all(isinstance(row, dict) for row in guarded["rows"])
    assert not any(isinstance(v, pd.DataFrame) for v in values.values())


def test_the_pseudonymizer_and_the_clients_are_not_in_state_and_cannot_be_checkpointed(rig):
    from src.guards.pseudonymizer import SessionPseudonymizer

    ranked_turn(rig)
    values = rig.agent.graph.get_state({"configurable": {"thread_id": rig.conversation_id}}).values
    assert "SessionPseudonymizer" not in json.dumps(values) and "pseudonym" not in json.dumps(values).lower()
    pseudonymizer = SessionPseudonymizer()
    with pytest.raises(TypeError, match="cannot be copied, pickled or stored"):
        pickle.dumps({"state": pseudonymizer})  # a write of this into a checkpoint would fail loudly
    with pytest.raises(TypeError):
        copy.deepcopy({"state": pseudonymizer})


def test_a_fake_id_is_the_same_for_the_same_customer_across_turns_of_the_session(rig):
    first = ranked_turn(rig)
    rig.client.add("spend", df=pd.DataFrame({"user_id": [REAL_IDS[0]], "spend": [500.0]}))
    second = ranked_turn(rig)
    labels = lambda text: set(re.findall(r"Customer #\d+", text))  # noqa: E731
    assert labels(second.text) <= labels(first.text)


# an answer the Output Guard holds back --------------------------------------------------------------------------------

def blocked_turn(rig):
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply(standalone="Revenue by brand"))
    rig.gateway.queue(Step.SQL_WRITE, sql_reply())
    rig.gateway.queue(Step.COMPOSER, composer_reply(f"Brand A earned 1,234.5. For details write to {EMAIL} today."))
    return rig.ask("revenue by brand")


@pytest.fixture
def blocking_rig(project, fake_bigquery):
    fake_bigquery.add("revenue", df=pd.DataFrame({"brand": ["Brand A"], "revenue": [1234.5]}))
    return make_rig(project, fake_bigquery=fake_bigquery)


def test_an_answer_with_personal_data_is_not_shown_and_the_turn_ends_honestly(blocking_rig):
    result = blocked_turn(blocking_rig)
    assert result.outcome == "failed" and result.error.kind is ErrorKind.OUTPUT_BLOCKED
    assert "held back this answer because it may contain personal data" in result.text
    row = blocking_rig.last_row
    assert row["pii_at_output"] >= 1 and row["guard_blocks"]["output"] == 1 and row["outcome"] == "failed"


def test_the_held_back_answer_is_stored_nowhere_not_even_masked(blocking_rig):
    result = blocked_turn(blocking_rig)
    searched = every_checkpoint(blocking_rig) + everything_on_disk(blocking_rig) + [json.dumps(blocking_rig.last_row), result.text, str(result.error)]
    spans = json.dumps(blocking_rig.spans())
    for fragment in ("jane.roe", "example.test", "write to", "Brand A earned"):
        assert not any(fragment in blob for blob in searched + [spans]), fragment  # nothing of the Composer's text survived
    assert "findings" in spans and "PII_AT_OUTPUT" in spans  # only the counts, the kinds and the places are kept


def test_the_state_never_held_the_answer_before_the_guard_passed_it(blocking_rig):
    blocked_turn(blocking_rig)
    for snapshot in blocking_rig.agent.graph.get_state_history({"configurable": {"thread_id": blocking_rig.conversation_id}}):
        assert snapshot.values.get("answer") in (None, "") or snapshot.values.get("outcome") == "failed"
        assert "earned" not in json.dumps(snapshot.values.get("answer") or "")
    final = blocking_rig.agent.graph.get_state({"configurable": {"thread_id": blocking_rig.conversation_id}}).values
    assert "contact" not in (final["answer"] or "").lower()  # the answer in state is the friendly refusal, never the draft


def test_a_blocked_turn_leaves_nothing_for_save_or_history(blocking_rig):
    blocked_turn(blocking_rig)
    values = blocking_rig.agent.graph.get_state({"configurable": {"thread_id": blocking_rig.conversation_id}}).values
    assert not values.get("history") and not values.get("last_answer")
    assert EMAIL not in json.dumps(values)


def test_a_fresh_turn_resets_every_per_turn_field_so_nothing_leaks_between_questions(project, fake_bigquery):
    fake_bigquery.add("revenue", df=pd.DataFrame({"brand": ["Brand A"], "revenue": [1234.5]}))
    rig = make_rig(project, fake_bigquery=fake_bigquery)
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply(standalone="Revenue by brand"))
    rig.gateway.queue(Step.SQL_WRITE, sql_reply())
    rig.gateway.queue(Step.COMPOSER, composer_reply("Brand A earned 1,234.5."))
    rig.ask("revenue by brand")
    rig.gateway.queue(Step.INPUT_GUARD, guard_reply("out_of_scope", in_scope=False))
    rig.ask("a poem")
    values = rig.agent.graph.get_state({"configurable": {"thread_id": rig.conversation_id}}).values
    assert values["results"] == {} and values["plan"] == {} and values["errors"][0]["kind"] == "OUT_OF_SCOPE" and len(values["errors"]) == 1
    assert values["outcome"] == "refused" and values["fatal"] is False and values["pending_action"] is None
    assert len(values["history"]) == 1 and "Brand A earned 1,234.5." in values["last_answer"]["text"]  # the refusal did not replace them


# the exchange file of --debug-prompts (plan 12, R7c) holds none of it either ----------------------------------------------------------------------

def test_no_real_customer_id_is_in_the_exchange_file(project, fake_bigquery):
    fake_bigquery.add("spend", df=pd.DataFrame({"user_id": list(REAL_IDS), "spend": [500.0, 300.0]}))
    rig, client = make_real_gateway_rig(
        project, fake_bigquery=fake_bigquery, debug_prompts=True, InputGuardOut=guard_reply(standalone="Who are my top customers?"),
        SqlOut=sql_reply(RANKED_SQL, "Top customers by their spend on your brands."), ComposerOut=composer_reply("The top customer spent 500."),
    )
    result = rig.ask("Who are my top customers?")
    path = exchange_file(rig.tracer.traces_dir, rig.last_trace_id)
    text = path.read_text(encoding="utf-8")
    assert result.outcome == "answered" and len(text.splitlines()) >= 4  # the three attempts and the answer
    assert re.search(r"Customer #\d{5,7}", text)  # the fake labels the user saw are there, in the answer line
    assert not any(str(real) in text for real in REAL_IDS)


def test_the_held_back_answer_is_in_no_line_of_the_exchange_file(project, fake_bigquery):
    fake_bigquery.add("revenue", df=pd.DataFrame({"brand": ["Brand A"], "revenue": [1234.5]}))
    rig, client = make_real_gateway_rig(
        project, fake_bigquery=fake_bigquery, debug_prompts=True,
        ComposerOut=composer_reply(f"Brand A earned 1,234.5. For details write to {EMAIL} today."),
    )
    result = rig.ask("revenue by brand")
    assert result.outcome == "failed" and result.error.kind is ErrorKind.OUTPUT_BLOCKED
    text = exchange_file(rig.tracer.traces_dir, rig.last_trace_id).read_text(encoding="utf-8")
    for fragment in ("jane.roe", "example.test", "write to", "Brand A earned"):
        assert fragment not in text, fragment
    assert '"kind":"answer"' not in text  # and no answer line: there was no answer
