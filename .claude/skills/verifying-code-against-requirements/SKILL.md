---
name: verifying-code-against-requirements
description: Use when asked whether the codebase does what the client brief (docs/requirements) and docs/decision.md expect, before a submission or milestone, or when a README, CLAUDE.md, plan or PRD says requirements are built and nobody has checked each one against code and tests
---

# Verifying Code Against Requirements

## Overview

Every sentence of the brief and the client Q&A is either a requirement that the code (or the design documents) meets, or text you can say why it is not a requirement. **100% coverage** means no word of those two files is left under no row, every item of `decision.md` has a row, and every verdict carries evidence you can open. It does not mean everything passes. The report says how much does, and a gap is a finding.

Docs, plan `Status:` lines, `CLAUDE.md` "Current state" and the PRD describe the code. They are claims, and the code and a passing test are the evidence.

**The scope is the whole working tree, not the branch.** A requirement is met or not by all the code that exists, including what no recent commit touched. `git diff`, `git status`, the branch name and the list of changed files never decide what to read or what to skip. The checker backs this up: it lists the `.py` files of `src`, `eval` and `evalkit` that no row cites, and the report must account for them.

## When to Use

- "Is the code what the brief and decision.md expect?" before a submission, a milestone or a merge
- A document says "built", "Full" or "covered" and nobody has checked each requirement
- Not for one plan's file and function names: use `verifying-plans-against-code`. Not for bug hunting: use `code-review`.

## Files here

| File | Use |
|---|---|
| `requirements-ledger.md` | Seed ledger: 221 rows in topical sections. The brief (92) and the client Q&A (24) are split into one falsifiable clause per row, with class and where to look; each client answer sits beside the requirement it clarifies. The 105 rows of `decision.md` (NFR, assumptions, risks, decisions, prototype stand-ins, answered questions) have first-guess classes. Open questions of `decision.md` are not expectations and have no row. |
| `check_ledger.py` | The coverage gate. Stdlib only. `--stage extract`, `--stage final --junit FILE`, `--merge FILE`, `--init-decision`, `--allow-untracked`, `--no-decision`, `--code-dirs`, `--strict-code`. Tests: `test_check_ledger.py`. |

## The Rule

**Every row gets a verdict with evidence the checker accepts. No sampling, and no stopping at the first serious finding.** Do not edit the sources, the script or its strictness to make it pass.

| Class | The sources ask | Verdicts that fit |
|---|---|---|
| `PROTO` | The prototype does it | MET, PARTIAL, NOT-MET, OVERCLAIM, LIVE-UNPROVEN, CONFLICT |
| `DESIGN` | The design documents explain it (building it is allowed) | MET, DESIGN-OK, PARTIAL, NOT-MET, OVERCLAIM, CONFLICT |
| `DELIV` | A file or document exists and is in git | MET, PARTIAL, NOT-MET, OVERCLAIM, CONFLICT |
| `OOS` | Excluded: not built, and the documents say so | OOS-OK, PARTIAL, OVERCLAIM, CONFLICT |
| `CTX` | Not a requirement (heading, greeting, time box) | EXCLUDED, with the reason in Note |

| Verdict | Means | Evidence the checker demands |
|---|---|---|
| MET | The code does it | `path:line` of code or config. On `PROTO` also a test: `path::test_name` that passed in the pytest run. On `DELIV` the file, tracked by git |
| DESIGN-OK | Explained in the documents, and they do not call it built | `docs/x.md:line` |
| OOS-OK | Nothing builds it and the documents say it is out | `search: <pattern> in <folders>` that found nothing, and the `docs` line |
| PARTIAL | Part of it is met | Refs, and a note saying which part is missing |
| NOT-MET | Nothing delivers it | Refs or `search:`, and a note |
| OVERCLAIM | A document says it is built or true and the code or tests say otherwise | The document line and the contradicting ref, test or `search:` |
| LIVE-UNPROVEN | Built and tested offline. Only a real BigQuery or Gemini run proves it, and none is recorded | Refs, and a note naming the missing run |
| CONFLICT | Two sources disagree. Quote both | Refs, and a note with both quotes. The owner decides |

Evidence tokens, separated by `;`: `path:line` or `path:a-b`, `path::test_name`, an existing path, `search: <pattern> in <folders>`. A `search:` names the patterns and every folder it covered: for an absence that means the whole tree (`src eval evalkit scenarios golden personas`, the root `*.yaml`), not the `Look` hints and not the changed files. The checker opens every path and line, and looks every test up in the junit file. A skipped test is not a pass.

## Procedure

Work from the repository root with the project's interpreter (`venv/Scripts/python.exe` on Windows, `venv/bin/python` elsewhere; `python -c "import pandas"` must work). Keep the working files outside the repo (the scratchpad). `SK=.claude/skills/verifying-code-against-requirements`, `W=<scratchpad>/requirements-check`.

0. **Pin the state.** `git rev-parse --short HEAD` and `git status --short`. The evidence is of this whole working tree, so the report names the commit and the dirty files. This is for the report only: do not use the dirty files to choose what to review. **No live calls:** no `pytest -m live`, `doctor --live`, `scenario run`, or `eval.run_*`. They need a Google sign-in, a Gemini key or the local model, and they spend quota. A requirement only a live run proves is LIVE-UNPROVEN. A run on the sample data does not prove BigQuery behaviour.
1. **Seed.** `cp $SK/requirements-ledger.md $W/ledger.md`.
2. **Extract.** `python $SK/check_ledger.py $W/ledger.md --stage extract` until it exits 0.
   - *Quote not found*: the source changed. Fix the row to the new words, never the source.
   - *Source text under no row*: add a row. One falsifiable clause per row. Split at "and", "or" and lists, each part quoting its own words. `…` in a quote spans from the words before it to the next place the words after it appear.
   - *decision.md item has no row*: `--init-decision` prints the rows. Add the missing ones.
   - Set a class for every `?`. Do not use `CTX` for anything that could be wrong: the report lists every CTX row for the owner to audit.
3. **Run the tests once.** `<python> -m pytest --junitxml=$W/junit.xml` (offline; `live` is deselected by `pytest.ini`). A test that fails here cannot be evidence. Record the failures.
4. **Dispatch in parallel**, one read-only agent per group of ledger sections (serial and in the same order if you have no agents). The sections already keep each client answer next to the requirement it clarifies. Give each agent its rows (ID, Quote, Class, Look), the two tables above and the junit path. Do not give a guessed verdict: an agent handed a hypothesis tends to return it confirmed. The Context section needs no agent: read each reason yourself, then set its verdict to `EXCLUDED`. If a reason does not hold, the row is a requirement: change its class and verify it.

   | Agent | Ledger sections (rows) |
   |---|---|
   | 1 | Task and architecture (11) and Deliverables and the prototype list (21) |
   | 2 | R1 Golden Bucket (11) |
   | 3 | R2 Safety and PII, with the client's scope answer (11) |
   | 4 | R3 destructive operations (6) and R5 resilience (5) |
   | 5 | R4 and R8 (7) and R6 and R7 (6) |
   | 6 | Dataset, capabilities and setup (23) |
   | 7, 8, 9 | decision.md (105): `DM-*` and `P8-*`; `NFR*`; `A1` to `A24`, `S1` to `S15` and the answered `Q` rows |

   Brief each agent: *Verify these rows against the whole repository at `<root>`: every file in the working tree counts, changed or not. The `Look` column is only a starting point, so search beyond it. Read only; open every file and line you cite; a document's own claim is never evidence for code; an absence claim names the patterns and folders you searched; no pytest and no live commands (the junit file lists what ran); return only a table `| ID | Verdict | Evidence | Note |`.*
5. **Merge.** Save each reply to a file and run `python $SK/check_ledger.py $W/ledger.md --merge reply.md`. It writes by ID and never touches a quote.
6. **Re-verify the confident ones.** An agent's miss comes back in the same voice as its hit. Open the cited lines yourself for: every row that is not satisfied (a false alarm costs the owner time), every `PROTO` row of R2, R3, QA6 to QA8, and every MET where the test name does not obviously match the clause. Read the test's assertion. A test that asserts only "no exception" proves nothing, and a guard test must fail when the guard is removed.
7. **Cross-checks the script cannot make.**
   - `decision.md` section 7.1 "Fit" column against the brief verdicts. A "Full" on a row that is not satisfied is an OVERCLAIM against decision.md.
   - The Built and Design labels in `technical-explanation.md`, the README example run, `CLAUDE.md` "Current state" and each plan's `Status:` against the verdicts. The plans' "Still to do" lists predate the live runs on real BigQuery (`decision.md` section 7.2 says so).
   - Brief against client Q&A against `decision.md` where they disagree. Add the finding to the report even when no row captures it.
8. **Final gate.** `python $SK/check_ledger.py $W/ledger.md --junit $W/junit.xml` must exit 0. Fix the ledger and run it again. If it fails only on deliverables not tracked by git, report that to the user and ask before using `--allow-untracked`. Use `--no-decision` only when the user limits the review to the brief, and say so in the report.
9. **Trace the code back.** The gate ends with the `.py` files of `src`, `eval` and `evalkit` that no row cites. Open each folder's modules (at least the docstrings) and place every one: support code for a named row, or behaviour that no requirement asks for. Unrequested behaviour is a finding when it touches what the sources exclude (a memory, preference or profile feature, a persona editor) or what they guard (a path to BigQuery or the model that skips a guard). Cite the support code in the row it serves and run the gate again, or list the folder in the report with the reason. `--strict-code` makes any uncited file fail the gate, for a final clean run.
10. **Report.** Copy the numbers from the checker's output. Do not write them by hand.

   - One coverage line: rows checked, words of the brief and the Q&A under a row, decision.md items, exit code, commit and dirty files.
   - Satisfied counts per source.
   - Findings, worst first: OVERCLAIM and NOT-MET on R2, R3, R5, R7, then CONFLICTs that need the owner, then other NOT-MET and PARTIAL, then LIVE-UNPROVEN.
   - The EXCLUDED rows with their reasons.
   - The code no row cites: one line per folder, support code or unrequested behaviour.
   - What was not run.

## Repo traps

| Trap | What to do |
|---|---|
| Prototype, design-only and out-of-scope are different checks | R2, R3, R5, R7 need code and a test. R1, R4, R6, R8 need a design in the documents (R1's seeded slice is optional). Never accept a design-only item described as working, or a built one hidden as design-only. |
| Owner decisions of 2026-10-22 (no memory agent, no persona management) against the client's "design only" and brief D2e | Judge R4a, R4b, R8a, R8b, QA3a, QA3b by what the documents do. They design it anyway: DESIGN-OK. They state the exclusion and why: CONFLICT, quoting the client's answer and the owner decision. They say nothing: NOT-MET. The owner rules; never turn it into OOS-OK. Absence is a `search:`, so list what you searched. |
| A config value nothing reads | k = 10, the 50-row cap, the byte cap, `max_replans`: read `config.yaml` and the code that reads the key. |
| Deliverables that exist but are not in git | `git ls-files` decides. On 2026-10-26 `docs/architecture.md` and `docs/technical-explanation.md` were untracked, so the repository a reviewer clones did not have them. |
| "Built" because the test file has the right name | The junit file decides. A marker that deselects a test (`live`) is not a pass. |
| The example run and the README's figures | Real output only (the project rule). If you cannot find the run an example came from, say so in the report. Never accept hand-written output. |
| The experience statement (D6c) | Must be the author's words (Q8). Never accept one written by an agent. |
| Pushing | Do not push or change the repository's visibility. Ask first. |

## Rationalization Table

| Excuse | Reality |
|---|---|
| "CLAUDE.md, the PRD and every plan say it is built" | Those are claims about the code. This review exists to check them. |
| "The test is named after the requirement" | A name is not a pass or an assertion. Read the junit result and the assertion. |
| "100% coverage is unrealistic, sample the important rows" | The script counts words and IDs, not effort. An unsampled row is a row nobody looked at. |
| "Coverage 100% must mean everything passes" | It means everything was checked. Report the satisfied count as it is. |
| "The owner already decided, mark it out of scope" | A decision that goes against the client's answer is a CONFLICT for the owner to rule on, not a silent pass. |
| "Live parts cannot be run, skip them" | Mark LIVE-UNPROVEN with the missing run. Do not skip the row and do not run live calls. |
| "The checker is too strict on this row" | Fix the evidence. Do not change the script, `--min-run`, or a row's class to get past it. |
| "The agent said MET" | Open the cited line yourself for the rows in step 6. |
| "Found a serious gap already, enough for a verdict" | A partial pass moves the rest to the next round. Finish every row. |
| "Only the files changed on this branch matter" | A requirement is met by all the code. Unchanged code can break it or satisfy it. Read the tree, not the diff. |
| "The old code was reviewed in an earlier plan" | Plans are claims too. The checker lists code no row cites for exactly this reason. |

## Red Flags: stop and go back

- Choosing what to read from `git diff`, `git status` or the branch's commits
- An absence claim (`search:`) that covered only the `Look` hints or the changed files
- A MET whose evidence you did not open
- A verdict based on a document sentence or a file name
- A requirement turned into `CTX`, or a quote shortened, to make the gap disappear
- "R2 is fine" without reading a test assertion
- The report has a number the checker did not print
- An unexplained `--allow-untracked` or `--no-decision`
- A live command run to "just check one thing"
