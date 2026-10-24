"""The Saved Reports store and tools (plan 08, F20, F21, F25): owner enforcement, selectors, soft delete, undo and purge.

These tests call the tools directly, with a tool context, so they are about what the store does. The delete flow through the
graph (preview, confirmation levels, the hash) is in test_delete_flow.py.
"""
import pytest

from src.reports import idempotency, messages
from src.tools.base import LastAnswer, ToolContext
from tests.guard_support import ALICE, BOB
from tests.reports_support import CURRENT, EVERYTHING, ID_PATTERN, NEWEST, text_match


def ctx(user=ALICE, conversation="conv-test0001", turn="t-1", last=None, key=None):
    return ToolContext(user, conversation, turn, "00000000", last, key)


def tool(env, name):
    return env.registry.get(f"saved_reports.{name}")


def find(env, selector, user=ALICE, conversation="conv-test0001"):
    return tool(env, "find").run({"selector": selector}, ctx(user, conversation))


def delete(env, ids, user=ALICE, key="delete-key", turn="t-2"):
    return tool(env, "delete").run({"ids": ids}, ctx(user, turn=turn, key=key))


def report_ids(env, user=ALICE):
    return [row["report_id"] for row in env.rows("SELECT report_id FROM saved_reports WHERE owner_id = ? ORDER BY rowid", user.user_id)]


# save and list (F20) -----------------------------------------------------------------------------------------------------------

def test_a_saved_report_has_an_owner_a_conversation_a_title_and_the_text(reports):
    saved = tool(reports, "save").run({}, ctx(last=LastAnswer("Revenue Q1", "Brand A sold 10 items.", "Brand A sold 10 items."), key="k1"))
    assert saved.message == 'Saved "Revenue Q1" on 2026-10-01.' and saved.count == 1
    [row] = reports.rows("SELECT * FROM saved_reports")
    assert ID_PATTERN.fullmatch(row["report_id"])
    assert (row["owner_id"], row["conversation_id"], row["turn_id"], row["title"], row["body"]) == (
        "alice", "conv-test0001", "t-1", "Revenue Q1", "Brand A sold 10 items.")
    assert row["created_at"] == "2026-10-01T12:00:00+00:00" and row["deleted_at"] is None
    assert row["idempotency_key"] == idempotency.save_key("conv-test0001", "t-1", "Brand A sold 10 items.", "k1")  # the request's key and the text


def test_list_shows_the_owners_reports_oldest_first_with_dates(reports):
    reports.seed("Revenue Q1")
    reports.clock.advance(days=1)
    reports.seed("Revenue Q2")
    listed = tool(reports, "list").run({}, ctx())
    assert listed.message == "You have 2 saved reports:\n1. Revenue Q1 (2026-10-01)\n2. Revenue Q2 (2026-10-02)" and listed.count == 2


def test_an_empty_list_and_an_empty_save_say_so_and_store_nothing(reports):
    assert tool(reports, "list").run({}, ctx()).message == messages.NO_REPORTS
    for last in (None, LastAnswer("T", "   ")):
        assert tool(reports, "save").run({}, ctx(last=last, key=f"k-{last}")).message == messages.NOTHING_TO_SAVE
    assert reports.rows("SELECT * FROM saved_reports") == [] and reports.audit() == []


def test_a_long_list_is_cut_but_the_count_is_the_whole_count(reports):
    for number in range(messages.MAX_LISTED + 3):
        reports.seed(f"Report {number}")
    listed = tool(reports, "list").run({}, ctx())
    assert listed.count == messages.MAX_LISTED + 3 and listed.message.endswith("... and 3 more.")
    assert listed.message.startswith(f"You have {messages.MAX_LISTED + 3} saved reports:")


def test_a_title_is_one_line_and_cut(reports):
    tool(reports, "save").run({}, ctx(last=LastAnswer("Revenue\n  by   brand " + "x" * 300, "body"), key="k"))
    [row] = reports.rows("SELECT title FROM saved_reports")
    assert row["title"].startswith("Revenue by brand xxx") and len(row["title"]) == 120 and "\n" not in row["title"]


def test_an_answer_with_personal_data_is_not_saved_and_is_not_echoed(reports):
    """A20: a saved report holds no PII. The Output Guard's scan runs again on the text, and the refusal does not repeat it."""
    for number, (title, body) in enumerate((("Customers", "Write to jane.roe@example.test about this."), ("Contact jane.roe@example.test", "body"))):
        result = tool(reports, "save").run({}, ctx(last=LastAnswer(title, body), key=f"k-{number}"))
        assert result.message == messages.NOT_SAVED_PERSONAL and "jane.roe" not in result.message and result.count == 0
    assert reports.rows("SELECT * FROM saved_reports") == []
    assert [(row["action"], row["outcome"]) for row in reports.audit()] == [("save", "blocked")] * 2
    assert "jane.roe" not in str(reports.audit())


# owner enforcement (F21) -------------------------------------------------------------------------------------------------------

def test_another_user_never_sees_finds_or_deletes_a_report(reports):
    reports.seed("Alice secret plan", "the plan")
    [alice_id] = report_ids(reports)
    assert tool(reports, "list").run({}, ctx(BOB)).message == messages.NO_REPORTS  # sees
    for selector in (text_match("secret"), text_match("plan"), EVERYTHING, NEWEST, CURRENT):  # finds: by text, by "all", by "last"
        found = find(reports, selector, BOB)
        assert found.targets == () and found.message == messages.NO_MATCH, selector
    assert delete(reports, [alice_id], BOB).message == messages.NO_MATCH  # deletes: handed the real ID
    assert reports.titles() == ["Alice secret plan"] and reports.rows("SELECT * FROM delete_batches") == []
    assert reports.rows("SELECT deleted_at FROM saved_reports")[0]["deleted_at"] is None


def test_an_id_list_with_another_users_report_among_the_owners_deletes_only_the_owners(reports):
    reports.seed("Alice one")
    reports.seed("Bob one", user=BOB, conversation="conv-bob")
    ids = report_ids(reports, ALICE) + report_ids(reports, BOB)
    assert delete(reports, ids, ALICE).count == 1
    assert reports.titles(ALICE) == [] and reports.titles(BOB) == ["Bob one"]


def test_undo_restores_only_the_owners_own_delete(reports):
    reports.seed("Alice one")
    delete(reports, report_ids(reports))
    assert tool(reports, "undo_delete").run({}, ctx(BOB, key="undo-bob")).message == messages.NO_DELETE_TO_UNDO
    assert reports.titles() == []
    assert tool(reports, "undo_delete").run({}, ctx(key="undo-alice")).message == "Restored 1 report." and reports.titles() == ["Alice one"]


# selectors (F22) ---------------------------------------------------------------------------------------------------------------

def test_the_conversation_selector_matches_only_the_current_conversation(reports):
    reports.seed("Here", conversation="conv-test0001")
    reports.seed("Elsewhere", conversation="conv-other")
    assert [t.label for t in find(reports, CURRENT, conversation="conv-test0001").targets] == ["Here"]
    assert [t.label for t in find(reports, CURRENT, conversation="conv-other").targets] == ["Elsewhere"]
    assert find(reports, CURRENT, conversation="conv-nobody").targets == ()


def test_all_finds_every_own_report_and_last_the_newest_one(reports):
    for title in ("A", "B", "C"):
        reports.seed(title)  # all three in the same second: the order is the order they were saved in
    assert [t.label for t in find(reports, EVERYTHING).targets] == ["A", "B", "C"]
    assert [t.label for t in find(reports, NEWEST).targets] == ["C"]


def test_a_found_target_carries_the_id_a_title_and_a_date(reports):
    reports.seed("Revenue Q1")
    [target] = find(reports, EVERYTHING).targets
    assert ID_PATTERN.fullmatch(target.ref) and target.label == "Revenue Q1" and target.when == "2026-10-01"
    assert target.ref not in repr(target)  # the ID is not in the repr: it never lands in a log by accident


def test_text_match_looks_in_the_title_and_the_body_in_any_case(reports):
    reports.seed("Weekly numbers", "Client X ordered more than last month.")
    reports.seed("Other", "nothing to see")
    for words in ("weekly", "CLIENT x", "ordered MORE"):
        assert [t.label for t in find(reports, text_match(words)).targets][:1] != ["Other"], words
        assert len(find(reports, text_match(words)).targets) == 1, words
    assert find(reports, text_match("client y")).targets == ()


def test_text_match_folds_case_in_every_alphabet(reports):
    reports.seed("Приходи за октомври", "Продажбите на Нике растат.")
    for words in ("ПРИХОДИ", "нике", "ПРОДАЖБИТЕ"):
        assert len(find(reports, text_match(words)).targets) == 1, words


@pytest.mark.parametrize("words, expected", [
    ("100%", ["Margin 100%"]), ("snake_case", ["snake_case"]), ("a\\b", ["a\\b path"]), ("%", ["Margin 100%"]), ("_", ["snake_case"]),
    ("\\", ["a\\b path"]),
])
def test_text_match_treats_percent_underscore_and_backslash_as_plain_characters(reports, words, expected):
    for title in ("Margin 100%", "Margin 1000", "snake_case", "snakeXcase", "a\\b path", "plain"):
        reports.seed(title)
    assert [t.label for t in find(reports, text_match(words)).targets] == expected


def test_text_match_is_parameterized_so_quotes_are_just_text(reports):
    reports.seed("Client's \"report\"")
    assert len(find(reports, text_match("client's")).targets) == 1
    assert find(reports, text_match("x' OR '1'='1")).targets == ()
    assert len(reports.rows("SELECT * FROM saved_reports")) == 1


def test_text_match_does_not_look_at_the_scope_line_or_the_notes(reports):
    """The saved text is the whole answer, with its scope line. A search for a brand must not match every report of a user who
    can see that brand: it looks at the title and the body of the answer."""
    full = "*Scope: your brands: Brand A, Brand B*\n\nRevenue was strong this month.\n\n**Notes**\n- 3 groups were hidden"
    reports.seed("Revenue", full, searchable="Revenue was strong this month.")
    assert find(reports, text_match("Brand B")).targets == ()
    assert find(reports, text_match("hidden")).targets == ()
    assert len(find(reports, text_match("strong")).targets) == 1
    assert reports.rows("SELECT body FROM saved_reports")[0]["body"] == full  # the report itself is kept whole


def test_a_search_with_no_words_finds_nothing_rather_than_everything(reports):
    reports.seed("Revenue")
    assert find(reports, {"kind": "text_match", "text": "   "}).targets == ()
    assert reports.store.find("alice", "conv-test0001", "text_match", "") == []
    assert find(reports, {"kind": "regex", "text": ".*"}).targets == ()  # an unknown kind finds nothing
    assert find(reports, None).targets == ()


# soft delete and undo (F25) -----------------------------------------------------------------------------------------------------

def test_a_deleted_report_leaves_the_list_and_the_text_stays_for_the_undo(reports):
    reports.seed("Revenue Q1", "the text")
    deleted = delete(reports, report_ids(reports))
    assert deleted.message == "Deleted 1 report. Say 'undo' within 30 days to bring it back." and deleted.count == 1
    assert reports.titles() == [] and find(reports, EVERYTHING).targets == ()
    assert tool(reports, "list").run({}, ctx()).message == messages.NO_REPORTS
    [row] = reports.rows("SELECT * FROM saved_reports")
    assert row["body"] == "the text" and row["deleted_at"] == "2026-10-01T12:00:00+00:00" and row["purge_after"] == "2026-10-31T12:00:00+00:00"
    [batch] = reports.rows("SELECT * FROM delete_batches")
    assert (batch["owner_id"], batch["count"], batch["undone_at"]) == ("alice", 1, None) and row["delete_batch_id"] == batch["batch_id"]


def test_undo_brings_the_reports_back_and_a_second_undo_does_nothing(reports):
    reports.seed("A")
    reports.seed("B")
    delete(reports, report_ids(reports))
    assert tool(reports, "undo_delete").run({}, ctx(key="u1")).message == "Restored 2 reports."
    assert reports.titles() == ["A", "B"]
    assert [r["deleted_at"] for r in reports.rows("SELECT deleted_at FROM saved_reports")] == [None, None]
    assert tool(reports, "undo_delete").run({}, ctx(key="u2")).message == messages.ALREADY_UNDONE
    assert reports.titles() == ["A", "B"]


def test_undo_walks_back_one_delete_at_a_time_newest_first(reports):
    reports.seed("A")
    reports.seed("B")
    first, second = report_ids(reports)
    delete(reports, [first], key="d1")
    reports.clock.advance(hours=1)
    delete(reports, [second], key="d2")
    assert tool(reports, "undo_delete").run({}, ctx(key="u1")).message == "Restored 1 report." and reports.titles() == ["B"]
    assert tool(reports, "undo_delete").run({}, ctx(key="u2")).message == "Restored 1 report." and reports.titles() == ["A", "B"]
    assert tool(reports, "undo_delete").run({}, ctx(key="u3")).message == messages.ALREADY_UNDONE


def test_there_is_nothing_to_undo_before_any_delete(reports):
    reports.seed("A")
    assert tool(reports, "undo_delete").run({}, ctx(key="u")).message == messages.NO_DELETE_TO_UNDO


def test_undo_is_refused_after_the_window_and_allowed_on_its_last_day(reports):
    reports.seed("A")
    delete(reports, report_ids(reports))
    reports.clock.advance(days=30)  # the last moment of the window
    assert tool(reports, "undo_delete").run({}, ctx(key="u1")).message == "Restored 1 report."
    delete(reports, report_ids(reports), key="d2")
    reports.clock.advance(days=30, seconds=1)
    assert tool(reports, "undo_delete").run({}, ctx(key="u2")).message == messages.UNDO_EXPIRED and reports.titles() == []


def test_the_window_comes_from_the_delete_undo_days_setting(project, fake_bigquery):
    from tests.reports_support import ReportsEnv

    env = ReportsEnv(project, fake_bigquery, config="delete:\n  undo_days: 2\n")
    try:
        env.seed("A")
        assert delete(env, report_ids(env)).message == "Deleted 1 report. Say 'undo' within 2 days to bring it back."
        env.clock.advance(days=3)
        assert tool(env, "undo_delete").run({}, ctx(key="u")).message == messages.UNDO_EXPIRED
    finally:
        env.close()


def test_purge_removes_reports_past_the_window_and_only_those(reports):
    for title in ("Old", "Recent", "Kept"):
        reports.seed(title)
    old, recent, _ = report_ids(reports)
    delete(reports, [old], key="d1")
    reports.clock.advance(days=20)
    delete(reports, [recent], key="d2")
    reports.clock.advance(days=11)  # Old was deleted 31 days ago, Recent 11
    assert reports.store.purge_expired() == 1
    assert [row["title"] for row in reports.rows("SELECT title FROM saved_reports ORDER BY rowid")] == ["Recent", "Kept"]
    assert tool(reports, "undo_delete").run({}, ctx(key="u")).message == "Restored 1 report." and reports.titles() == ["Kept", "Recent"]


def test_purge_writes_one_audit_row_with_the_count_and_nothing_when_nothing_is_due(reports):
    reports.seed("A")
    reports.seed("B")
    delete(reports, report_ids(reports))
    assert reports.store.purge_expired() == 0 and reports.audit("purge") == []
    reports.clock.advance(days=31)
    assert reports.store.purge_expired() == 2 and reports.store.purge_expired() == 0
    [row] = reports.audit("purge")
    assert (row["count"], row["outcome"], row["user_hash"]) == (2, "ok", "system") and reports.rows("SELECT * FROM saved_reports") == []


def test_purge_never_removes_a_report_that_was_not_deleted(reports):
    reports.seed("Live")
    reports.clock.advance(days=400)
    assert reports.store.purge_expired() == 0 and reports.titles() == ["Live"]


def test_a_deleted_report_cannot_be_deleted_again(reports):
    reports.seed("A")
    ids = report_ids(reports)
    assert delete(reports, ids, key="d1").count == 1
    assert delete(reports, ids, key="d2").message == messages.NO_MATCH
    assert len(reports.rows("SELECT * FROM delete_batches")) == 1


def test_an_empty_id_list_deletes_nothing(reports):
    reports.seed("A")
    assert delete(reports, []).message == messages.NO_MATCH and reports.titles() == ["A"]


def test_a_report_needs_a_conversation_that_exists(reports):
    """The foreign key: a report always points to the conversation it was saved in (S8). The tool raises, and the graph turns that
    into TOOL_FAILED (test_delete_flow.py)."""
    with pytest.raises(Exception, match="FOREIGN KEY"):
        tool(reports, "save").run({}, ctx(conversation="conv-never-created", last=LastAnswer("T", "body"), key="k"))
    assert reports.rows("SELECT * FROM saved_reports") == []
    assert [(row["action"], row["outcome"]) for row in reports.audit()] == [("save", "failed")]


# an undo with nothing left to restore changes nothing and says so every time (review finding) -----------------------------------------------------------

def test_an_undo_whose_reports_are_gone_is_refused_every_time_and_the_delete_stays_open(reports):
    reports.seed("A")
    delete(reports, report_ids(reports))
    reports.execute("DELETE FROM saved_reports")  # the rows are gone while the window is still open (undo_days was raised, say)
    undo = tool(reports, "undo_delete")
    assert undo.run({}, ctx(key="u1")).message == messages.UNDO_EXPIRED
    assert undo.run({}, ctx(key="u1")).message == messages.UNDO_EXPIRED  # the same request again: not "Restored 1 report."
    assert undo.run({}, ctx(key="u2")).message == messages.UNDO_EXPIRED  # and another: not "already undone"
    assert reports.rows("SELECT undone_at, undo_key FROM delete_batches") == [{"undone_at": None, "undo_key": None}]
    assert [row["outcome"] for row in reports.audit("undo")] == ["expired", "expired"]  # one per key


def test_a_delete_can_be_undone_until_the_last_moment_of_its_window_and_is_not_purged_before_it(reports):
    reports.seed("A")
    delete(reports, report_ids(reports))
    reports.clock.advance(days=30)  # exactly undo_days later
    assert reports.store.purge_expired() == 0  # the same test as the undo's: not yet over
    assert tool(reports, "undo_delete").run({}, ctx(key="u")).message == "Restored 1 report." and reports.titles() == ["A"]
    delete(reports, report_ids(reports), key="d2")
    reports.clock.advance(days=30, seconds=1)
    assert reports.store.purge_expired() == 1  # one second later it is over, for the purge and the undo alike


def test_the_purge_takes_the_delete_batches_of_the_window_with_the_reports(reports):
    for title in ("Old", "Recent"):
        reports.seed(title)
    old, recent = report_ids(reports)
    delete(reports, [old], key="d1")
    reports.clock.advance(days=20)
    delete(reports, [recent], key="d2")
    reports.clock.advance(days=11)  # Old is 31 days past, Recent 11
    assert reports.store.purge_expired() == 1
    assert len(reports.rows("SELECT * FROM delete_batches")) == 1  # the batch that can still be undone is the one left
    assert tool(reports, "undo_delete").run({}, ctx(key="u")).message == "Restored 1 report." and reports.titles() == ["Recent"]
    reports.clock.advance(days=40)
    delete(reports, report_ids(reports), key="d3")
    reports.clock.advance(days=31)
    reports.store.purge_expired()
    assert reports.rows("SELECT * FROM delete_batches") == []
    assert tool(reports, "undo_delete").run({}, ctx(key="u2")).message == messages.NO_DELETE_TO_UNDO  # nothing left of the history


def test_undo_reads_the_newest_open_delete_through_the_index(reports):
    plan = reports.rows(
        "EXPLAIN QUERY PLAN SELECT * FROM delete_batches WHERE owner_id = ? AND undone_at IS NULL ORDER BY created_at DESC, rowid DESC LIMIT 1", "alice"
    )
    assert any("idx_batches_owner" in row["detail"] for row in plan), plan


def test_a_request_with_no_effect_finds_its_audit_row_through_the_index(reports):
    plan = reports.rows("EXPLAIN QUERY PLAN SELECT 1 FROM audit_log WHERE action = ? AND idempotency_key = ? AND outcome = ?", "undo", "k", "none")
    assert any("idx_audit_key" in row["detail"] for row in plan), plan
