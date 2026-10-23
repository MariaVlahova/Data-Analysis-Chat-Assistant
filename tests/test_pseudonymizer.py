"""Fake customer IDs for one session (plan 06, F14, A3)."""
import copy
import pickle
import re
from decimal import Decimal

import numpy as np
import pytest

from src.guards import pseudonymizer as pseudonymizer_module
from src.guards.output_guard import OutputGuard
from src.guards.pseudonymizer import SessionPseudonymizer

LABEL = re.compile(r"Customer #\d{5,}")


def test_a_label_has_the_documented_shape():
    assert re.fullmatch(r"Customer #\d{5}", SessionPseudonymizer().fake_id(48213))


def test_the_same_real_id_gets_the_same_label_all_session():
    pseudonymizer = SessionPseudonymizer()
    first = pseudonymizer.fake_id(48213)
    for _ in range(3):
        assert pseudonymizer.fake_id(48213) == first


def test_every_spelling_of_one_id_is_one_customer():
    pseudonymizer = SessionPseudonymizer()
    labels = {pseudonymizer.fake_id(value) for value in (7, 7.0, np.int64(7), np.float64(7.0), Decimal("7"), "7")}
    assert len(labels) == 1 and pseudonymizer.size == 1


def test_different_ids_never_collide_across_10000_draws():
    pseudonymizer = SessionPseudonymizer()
    labels = [pseudonymizer.fake_id(real_id) for real_id in range(10_000)]
    assert len(set(labels)) == 10_000  # a 5-digit space has 90,000 labels, so a collision by chance is likely: the lookup prevents it
    assert all(LABEL.fullmatch(label) for label in labels)


def test_a_full_label_space_widens_instead_of_repeating():
    pseudonymizer = SessionPseudonymizer(seed=3)
    labels = [pseudonymizer.fake_id(real_id) for real_id in range(60_000)]  # more than half of the 90,000 five-digit labels
    assert len(set(labels)) == 60_000
    assert any(len(label) > len("Customer #12345") for label in labels)


def test_a_new_instance_gives_different_labels():
    one, other = SessionPseudonymizer(), SessionPseudonymizer()
    ids = range(50)
    assert [one.fake_id(i) for i in ids] != [other.fake_id(i) for i in ids]


def test_the_seed_replays_the_draws_for_a_test():
    assert SessionPseudonymizer(seed=5).fake_id(1) == SessionPseudonymizer(seed=5).fake_id(1)
    assert SessionPseudonymizer(seed=5).fake_id(1) != SessionPseudonymizer(seed=6).fake_id(1)


def test_the_label_does_not_depend_on_the_real_id():
    # Two sessions with the same random draws give the same labels to different customers: the label says nothing about the ID.
    assert SessionPseudonymizer(seed=9).fake_id(1) == SessionPseudonymizer(seed=9).fake_id(99999)


def test_the_object_does_not_keep_a_real_id_anywhere_it_can_be_read():
    pseudonymizer = SessionPseudonymizer()
    pseudonymizer.fake_id(48213)
    pseudonymizer.fake_id("canary-customer-77")
    dump = repr(pseudonymizer) + repr(vars(pseudonymizer))
    assert "48213" not in dump and "canary-customer-77" not in dump


@pytest.mark.parametrize("value", [None, True, float("nan"), float("inf"), b"7", [7], {"id": 7}, object(), Decimal("NaN")])
def test_a_value_that_cannot_be_a_customer_id_is_refused(value):
    with pytest.raises(TypeError):
        SessionPseudonymizer().fake_id(value)


@pytest.mark.parametrize("copier", [copy.copy, copy.deepcopy, pickle.dumps])
def test_it_cannot_be_copied_or_pickled_into_a_checkpoint(copier):
    pseudonymizer = SessionPseudonymizer()
    pseudonymizer.fake_id(48213)
    with pytest.raises(TypeError, match="closure"):
        copier(pseudonymizer)


def test_the_label_space_is_capped_and_then_fails_closed_not_wider(monkeypatch):
    # The Output Guard lets a label of MAX_DIGITS digits or fewer through and scans anything longer, so the pseudonymizer
    # must never issue a longer one. Past the cap it stops instead (the Result Guard turns that into RESULT_GUARD_FAILED).
    monkeypatch.setattr(pseudonymizer_module, "MAX_DIGITS", 5)
    pseudonymizer = SessionPseudonymizer(seed=1)
    labels = [pseudonymizer.fake_id(real_id) for real_id in range(45_000)]  # half of the 90,000 five-digit labels
    assert len(set(labels)) == 45_000 and all(len(label) == len("Customer #12345") for label in labels)
    with pytest.raises(RuntimeError, match="used up"):
        pseudonymizer.fake_id(45_000)
    assert pseudonymizer.fake_id(7) == labels[7]  # a customer who already has a label still gets it


def test_the_output_guard_exempts_exactly_the_widths_the_pseudonymizer_issues():
    assert (pseudonymizer_module.FIRST_DIGITS, pseudonymizer_module.MAX_DIGITS) == (5, 7)
    widest = SessionPseudonymizer()
    widest._digits = pseudonymizer_module.MAX_DIGITS  # a session that has grown to the widest label
    label = widest.fake_id(1)
    assert len(label) == len("Customer #") + pseudonymizer_module.MAX_DIGITS
    assert OutputGuard().scan(f"{label} spent $1,200") == []
