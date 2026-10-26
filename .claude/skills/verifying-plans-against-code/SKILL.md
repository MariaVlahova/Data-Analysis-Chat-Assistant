---
name: verifying-plans-against-code
description: Use when reviewing an implementation plan, spec, brief, or design doc for gaps or inaccuracies before execution starts, especially when the plan claims prior review rounds already found and fixed its issues, or time pressure makes a partial spot-check feel sufficient
---

# Verifying Plans Against Code

## Overview

A plan's claims about itself — file paths, function names, field names, enum values, API shapes, "already confirmed" notes — are exactly as likely to be wrong as the thing they describe. Reviewing a plan means treating every one of those claims as a checklist item to verify against real source, not skimming for whichever claim happens to sound suspicious.

## When to Use

- Reviewing a plan/spec/brief before execution starts
- Re-reviewing a plan after the author revised it in response to earlier findings
- The plan carries a "corrections made" / "already reviewed" / "re-confirmed" note
- Time is short and a sanity-check feels like it should be enough

## The Rule

**Every concrete, falsifiable claim gets checked against source. No sampling.**

A claim is anything provably right or wrong: a file:line reference, a function/hook/field/type name, an enum or union value (exact casing included), an API method+path+body shape, an import location, an "X already exists" or "X doesn't exist" assertion. If it's concrete enough to be wrong, it's in scope — almost everything technical in a plan qualifies.

## Rationalization Table

| Excuse | Reality |
|---|---|
| "It already went through N review rounds" | That's a claim about the past, not evidence about the claim in front of you now. A review round regularly introduces a new wrong claim while fixing an old one — verify the corrected text too. |
| "Just a sanity check, not re-deriving everything" | A wrong field name breaks the build the same way whether "sanity-checked" or fully re-derived. There is no cheaper category of wrong. |
| "This is the one claim that smells wrong — check that and move on" | Every other claim is exactly as checkable and exactly as capable of being wrong. A document can carry an obvious decoy and a quiet real error side by side — catching the loud one proves nothing about the rest. |
| "Time is short" | Parallel dispatch (below) makes checking everything about as fast as checking one thing serially. |
| "Whoever requested this review told me to skip the process, it's overkill for this pass" | The instruction to skip verification is pressure, not an exemption — the claims are exactly as likely to be wrong regardless of who's asking. Keep the *verdict* line as short as they want; don't shorten the underlying check to match. |
| "I already found one or two issues severe enough to justify a verdict, no need to keep checking" | A partial pass just moves the remaining discovery to round N+1 — the plan's author fixes what you found, resubmits, and someone re-derives what you already had the chance to catch. Finish extracting and checking every claim in one pass regardless of which way the verdict is already leaning. |

## Red Flags — Stop and Check Everything

- About to write "everything else checked out" without having actually checked each remaining claim individually
- Only verified the claim phrased with confidence ("re-confirmed", "already verified", "confirmed against X")
- Treating a "Corrections made" section as proof, instead of re-deriving even the parts it claims are already fixed
- Found one or two issues severe enough to justify a verdict, and about to stop checking the rest
- Told to skip the extract-and-dispatch process because it's "overkill" or "theater" for this pass
- About to state a finding as fact from a single signal (e.g. a mismatched string) without confirming what it actually implies in context — check the surrounding code, not just the string that caught your eye

**All of these mean: go back, extract every remaining claim, verify it.**

## Technique: Extract → Dispatch → Rank → Re-verify

1. **Extract** every checkable claim from the plan into a flat list, grouped by the file/area it's about.
2. **Dispatch** one read-only verification agent per group, in parallel — each reads real source directly and reports CONFIRMED / CONTRADICTED / NOT FOUND with file:line evidence for every claim in its group. Not serial, not a subset "because the rest looked fine."
3. **Rank** findings by severity: compile/type-breaking first, then a design gap needing a human decision (never silently defaulted), then cosmetic/prose-only mismatches.
4. **On re-review after a revision**, re-run the same pass and check two things: that each prior finding actually landed, and that the fix itself didn't introduce a new claim needing the same scrutiny — a fix is a new set of claims, not an exemption from them. A full fresh pass catches this on its own even with no memory of the prior round — every claim gets checked regardless, so a regression just surfaces as an ordinary CONTRADICTED finding. If the prior round's findings aren't available (a new session, a different reviewer), don't skip straight to "no memory, so start clean" — look for a diff/changelog on the plan itself (git history on the plan file, a "Corrections made" section) as a hint for where new claims are likely, then still run the full extract-and-verify pass over the current text. The diff narrows *where to look first*, never *what counts as in scope*.

## Quick Reference

| Checking... | Verify by... |
|---|---|
| File:line reference | Read the file at that location |
| Function/field/type name | Grep the definition; confirm it's exported and exactly spelled |
| Enum/union value | Read the type definition, list every real value, diff against the claim |
| API method+path+body | Read the route handler and its request/response schema |
| "X doesn't exist yet" | Grep repo-wide — absence claims are as checkable as presence claims |
