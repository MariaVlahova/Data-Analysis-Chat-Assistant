"""Tests for check_ledger.py: each rule has a test that breaks the ledger in one place and expects that rule to fire.

Run: python -m pytest .claude/skills/verifying-code-against-requirements/test_check_ledger.py -p no:cacheprovider
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("check_ledger.py")
HEADER = "| ID | Src | Quote | Class | Look | Verdict | Evidence | Note |\n|---|---|---|---|---|---|---|---|\n"

JUNIT = """<testsuites><testsuite>
<testcase classname="tests.test_mask" name="test_masks"/>
<testcase classname="tests.test_mask" name="test_masks_more[a]"/>
<testcase classname="tests.test_mask" name="test_broken"><failure message="x"/></testcase>
<testcase classname="tests.test_mask" name="test_skipped"><skipped/></testcase>
</testsuite></testsuites>"""

GOOD = {
    "B1": ("brief", "The agent must mask names.", "PROTO", "MET", "src/mask.py:2; tests/test_mask.py::test_masks", ""),
    "B2": ("brief", "It must never delete without asking.", "PROTO", "NOT-MET",
           "search: grep confirm in src/ found nothing", "no confirmation step exists anywhere"),
    "Q1": ("qa", "Harry says: scope by brand.", "DESIGN", "DESIGN-OK", "docs/design.md:1", ""),
    "DM-3.1": ("decision", "Agent framework", "PROTO", "MET", "src/mask.py:1; tests/test_mask.py::test_masks_more", ""),
    "NFR1": ("decision", "Response time", "CTX", "EXCLUDED", "", "no target is given for it"),
}


def table(rows: dict) -> str:
    return HEADER + "".join(
        f"| {i} | {s} | {q} | {c} | | {v} | {e} | {n} |\n" for i, (s, q, c, v, e, n) in rows.items())


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "docs" / "requirements").mkdir(parents=True)
    (tmp_path / "docs" / "requirements" / "Original_Requirements.md").write_text(
        "The agent must mask names. It must never delete without asking.\n", encoding="utf-8")
    (tmp_path / "docs" / "requirements" / "client-QA.md").write_text("Harry says: scope by brand.\n", encoding="utf-8")
    (tmp_path / "docs" / "decision.md").write_text(
        "### 3.1 Agent framework [PROPOSED]\n\n| NFR1 | Response time | 30 s | [PROPOSED] | x |\n", encoding="utf-8")
    (tmp_path / "docs" / "design.md").write_text("Scope comes from the brand list.\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mask.py").write_text("def mask(x):\n    return '***'\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_mask.py").write_text("def test_masks():\n    pass\n", encoding="utf-8")
    (tmp_path / "junit.xml").write_text(JUNIT, encoding="utf-8")
    return tmp_path


def run(repo: Path, rows: dict | None = None, *args: str, stage: str = "final", junit: bool = True):
    ledger = repo / "ledger.md"
    ledger.write_text(table(GOOD if rows is None else rows), encoding="utf-8")
    command = [sys.executable, str(SCRIPT), str(ledger), "--root", str(repo), "--stage", stage, "--allow-untracked", *args]
    if junit:
        command += ["--junit", str(repo / "junit.xml")]
    done = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
    return done.returncode, done.stdout


def changed(**edits):
    rows = dict(GOOD)
    for row_id, fields in edits.items():
        row = list(rows[row_id.replace("_", "-").replace("DM-3-1", "DM-3.1")])
        for index, value in fields.items():
            row[index] = value
        rows[row_id.replace("_", "-").replace("DM-3-1", "DM-3.1")] = tuple(row)
    return rows


def test_a_complete_ledger_passes(repo):
    code, out = run(repo)
    assert code == 0, out
    assert "COVERAGE 100%" in out and "satisfied (MET, DESIGN-OK, OOS-OK) in brief: 1/2" in out


def test_a_rejected_row_does_not_count_as_satisfied(repo):
    code, out = run(repo, changed(B1={4: "src/mask.py:2"}))
    assert code == 1 and "in brief: 0/2" in out


def test_a_requirement_row_that_is_not_met_is_a_finding_not_a_failure(repo):
    code, out = run(repo)
    assert code == 0 and "NOT-MET" in out and "no confirmation step" in out


def test_a_sentence_under_no_row_fails(repo):
    rows = dict(GOOD)
    del rows["B2"]
    code, out = run(repo, rows, stage="extract")
    assert code == 1 and "source text under no row: 'it must never delete without asking'" in out


def test_a_paraphrased_quote_fails(repo):
    code, out = run(repo, changed(B1={1: "The agent has to mask names."}), stage="extract")
    assert code == 1 and "B1: quote not found" in out


def test_a_missing_decision_row_fails(repo):
    rows = dict(GOOD)
    del rows["NFR1"]
    code, out = run(repo, rows, stage="extract")
    assert code == 1 and "NFR1: decision.md item has no ledger row" in out


def test_no_decision_skips_the_decision_items_and_says_so(repo):
    rows = {k: v for k, v in GOOD.items() if v[0] != "decision"}
    code, out = run(repo, rows, stage="extract")
    assert code == 1 and "no ledger row" in out
    code, out = run(repo, rows, "--no-decision", stage="extract")
    assert code == 0 and "NOT CHECKED (--no-decision)" in out


def test_ellipsis_spans_the_words_between(repo):
    rows = dict(GOOD)
    rows["B1"] = ("brief", "The agent … without asking.", "PROTO", "NOT-MET", "search: nothing in src/ asks", "spans both sentences")
    del rows["B2"]
    code, out = run(repo, rows, stage="extract")
    assert code == 0, out


def test_extract_stage_ignores_missing_verdicts_but_final_does_not(repo):
    blank = {k: (s, q, c, "", "", n if c == "CTX" else "") for k, (s, q, c, _v, _e, n) in GOOD.items()}
    assert run(repo, blank, stage="extract")[0] == 0
    code, out = run(repo, blank)
    assert code == 1 and "verdict empty does not fit class PROTO" in out


@pytest.mark.parametrize("edit, message", [
    ({3: "PARTIAL", 5: ""}, "PARTIAL needs a note"),
    ({3: "DESIGN-OK"}, "verdict DESIGN-OK does not fit class PROTO"),
    ({3: "MET", 4: "src/mask.py:2"}, "needs a test that passed"),
    ({4: "tests/test_mask.py::test_masks_missing"}, "test missing in the pytest run"),
    ({4: "tests/test_mask.py::test_broken"}, "test failed in the pytest run"),
    ({4: "tests/test_mask.py::test_skipped"}, "test skipped in the pytest run"),
    ({4: "src/mask.py:99; tests/test_mask.py::test_masks"}, "line outside the file (2 lines)"),
    ({4: "src/nowhere.py:1; tests/test_mask.py::test_masks"}, "evidence is not a path:line"),
    ({4: "docs/design.md:1; tests/test_mask.py::test_masks"}, "MET needs a code or file reference"),
    ({4: ""}, "no evidence"),
    ({4: "search: ok"}, "search evidence says too little"),
    ({4: "search: grep confirm found nothing at all"}, "search evidence must name where it looked"),
])
def test_final_stage_rules(repo, edit, message):
    code, out = run(repo, changed(B1=edit))
    assert code == 1 and message in out, out


def test_a_test_citation_needs_a_junit_run(repo):
    code, out = run(repo, junit=False)
    assert code == 1 and "cites a test but no --junit run was given" in out


def test_design_oos_and_overclaim_evidence(repo):
    assert "DESIGN-OK needs a reference" in run(repo, changed(Q1={4: "src/mask.py:1"}))[1]
    oos = changed(Q1={2: "OOS", 3: "OOS-OK", 4: "docs/design.md:1"})
    assert "OOS-OK needs a search:" in run(repo, oos)[1]
    oos = changed(Q1={2: "OOS", 3: "OOS-OK", 4: "docs/design.md:1; search: no memory module under src/"})
    assert run(repo, oos)[0] == 0
    over = changed(B1={3: "OVERCLAIM", 4: "docs/design.md:1", 5: "design says built, code has no such thing"})
    assert "OVERCLAIM needs the document line" in run(repo, over)[1]


def test_ctx_row_needs_a_reason_and_only_the_excluded_verdict(repo):
    assert "CTX row (not a requirement) needs a note" in run(repo, changed(NFR1={5: ""}), stage="extract")[1]
    assert "does not fit class CTX" in run(repo, changed(NFR1={3: "MET"}))[1]


def test_duplicate_id_and_bad_src_fail(repo):
    text = table(GOOD) + "| B1 | brief | x | PROTO | | | | |\n"
    (repo / "dup.md").write_text(text, encoding="utf-8")
    done = subprocess.run([sys.executable, str(SCRIPT), str(repo / "dup.md"), "--root", str(repo), "--stage", "extract"],
                          capture_output=True, text=True, encoding="utf-8")
    assert "missing or duplicate ID" in done.stdout
    assert "Src must be one of" in run(repo, changed(B1={0: "wiki"}), stage="extract")[1]


def test_deliverable_evidence_must_be_tracked_by_git(repo):
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "src", "tests", "docs/decision.md"], cwd=repo, check=True)
    rows = changed(Q1={2: "DELIV", 3: "MET", 4: "docs/design.md:1"})
    ledger = repo / "ledger.md"
    ledger.write_text(table(rows), encoding="utf-8")
    base = [sys.executable, str(SCRIPT), str(ledger), "--root", str(repo), "--junit", str(repo / "junit.xml")]
    strict = subprocess.run(base, capture_output=True, text=True, encoding="utf-8")
    assert strict.returncode == 1 and "docs/design.md is not tracked by git" in strict.stdout
    relaxed = subprocess.run(base + ["--allow-untracked"], capture_output=True, text=True, encoding="utf-8")
    assert relaxed.returncode == 0, relaxed.stdout


def test_code_no_row_cites_is_listed_from_the_whole_tree_not_the_diff(repo):
    (repo / "src" / "memory").mkdir()
    (repo / "src" / "memory" / "profile.py").write_text("PROFILE = {}\n", encoding="utf-8")
    (repo / "src" / "memory" / "__init__.py").write_text("", encoding="utf-8")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "everything is committed: the diff against HEAD is empty"], cwd=repo, check=True)
    code, out = run(repo)
    assert code == 0, out
    assert "code that no row cites (src,eval,evalkit): 1 of 2 .py files" in out
    assert "src/memory (1): profile.py" in out and "mask.py" not in out.split("code that no row cites")[1]
    code, out = run(repo, None, "--strict-code")
    assert code == 1 and "1 code file(s) are cited by no row" in out


def test_citing_a_folder_covers_the_code_under_it(repo):
    (repo / "src" / "memory").mkdir()
    (repo / "src" / "memory" / "profile.py").write_text("PROFILE = {}\n", encoding="utf-8")
    code, out = run(repo, changed(B2={4: "search: grep confirm in src/ found nothing; src/memory"}), "--strict-code")
    assert code == 0 and "0 of 2 .py files" in out, out


def test_merge_fills_rows_by_id_and_never_touches_quotes(repo):
    blank = {k: (s, q, c, "", "", "") for k, (s, q, c, *_rest) in GOOD.items()}
    ledger = repo / "ledger.md"
    ledger.write_text(table(blank), encoding="utf-8")
    reply = repo / "reply.md"
    reply.write_text("| ID | Verdict | Evidence | Note |\n|---|---|---|---|\n"
                     "| B1 | met | src/mask.py:2; tests/test_mask.py::test_masks | masks it |\n", encoding="utf-8")
    done = subprocess.run([sys.executable, str(SCRIPT), str(ledger), "--merge", str(reply)],
                          capture_output=True, text=True, encoding="utf-8")
    assert done.returncode == 0 and "merged 1 row(s)" in done.stdout and "4 row(s) still without" in done.stdout
    text = ledger.read_text(encoding="utf-8")
    assert "| B1 | brief | The agent must mask names. | PROTO | | MET | src/mask.py:2; tests/test_mask.py::test_masks | masks it |" in text
    reply.write_text("| ID | Verdict | Evidence | Note |\n|---|---|---|---|\n| B9 | MET | x | y |\n", encoding="utf-8")
    before = ledger.read_text(encoding="utf-8")
    done = subprocess.run([sys.executable, str(SCRIPT), str(ledger), "--merge", str(reply)],
                          capture_output=True, text=True, encoding="utf-8")
    assert done.returncode == 1 and "unknown ID 'B9'" in done.stdout and ledger.read_text(encoding="utf-8") == before


def test_init_decision_lists_what_decision_md_expects(repo):
    done = subprocess.run([sys.executable, str(SCRIPT), "--init-decision", "--root", str(repo)],
                          capture_output=True, text=True, encoding="utf-8")
    assert "| DM-3.1 | decision | Agent framework | ? |" in done.stdout and "| NFR1 | decision | Response time | ? |" in done.stdout
