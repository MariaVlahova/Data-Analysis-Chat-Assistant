# Plan 07: Agent graph
Status: not started

**Goal:** Build the full LangGraph pipeline — Input Guard (with its delete-or-save shortcut straight to the Tool Router), Context Assembly, Planner, SQL loop (SQL Guard, dry-run, bounded repair), Result Guard, Evidence Check with a bounded replan defaulting to max_replans = 0, Composer with a numeric grounding check, Output Guard, and a real Tool Router plus HITL Gate wired to a stubbed Saved Reports tool — that answers questions, explains causes and writes reports with action items, with typed errors and honest failure messages.
