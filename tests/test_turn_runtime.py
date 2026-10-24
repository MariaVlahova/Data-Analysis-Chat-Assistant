"""`TurnRuntime.scratch`: text the Output Guard has not passed, and the one list of what may be held there (plan 07)."""
import pytest

from src.agent.boundary import _drop_unchecked_text
from src.agent.deps import DRAFT, FINAL_TEXT, NOTES, TITLE, UNCHECKED_KEYS, TurnRuntime
from src.errors import AgentError, ErrorKind
from src.llm.budget import TurnBudget
from src.llm.fake import FakeClock
from tests.agent_support import make_rig


def runtime():
    return TurnRuntime(TurnBudget(8, 60, 10**9, FakeClock()))


def test_every_entry_of_the_scratch_space_is_in_the_one_list():
    assert set(UNCHECKED_KEYS) == {DRAFT, FINAL_TEXT, TITLE, NOTES}


def test_only_the_named_entries_can_be_held():
    held = runtime()
    for key in UNCHECKED_KEYS:
        held.hold(key, "text")
    with pytest.raises(AgentError) as caught:
        held.hold("answer_copy", "text")  # a new entry that the drop below would not know about
    assert caught.value.kind is ErrorKind.INTERNAL and "answer_copy" not in held.scratch


def test_dropping_removes_every_entry_the_title_and_the_notes_too():
    held = runtime()
    for key in UNCHECKED_KEYS:
        held.hold(key, "text")
    held.drop_unchecked()
    assert held.scratch == {}


def test_the_error_boundary_drops_all_of_it_when_a_node_fails(project, fake_bigquery):
    rig = make_rig(project, fake_bigquery=fake_bigquery)
    held = runtime()
    rig.agent.deps.begin("conv-x", held)
    for key in UNCHECKED_KEYS:
        held.hold(key, "an unchecked title")
    _drop_unchecked_text(rig.agent.deps, {"conversation_id": "conv-x"})
    assert held.scratch == {}
    rig.agent.deps.end("conv-x")
