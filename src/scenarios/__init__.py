"""Scripted test scenarios for the whole app (`scenario run`): a user, the lines they type, and what must be true of each answer.

A scenario drives the real chat (the same `open_session` as `chat`, so the same guards, graph, tools and traces) with the model of the
environment: `qwen/qwen3-30b-a3b-2507` on this machine for `local`, Gemini for the others. The model is not scripted, so what a scenario
checks is chosen to hold whatever the model words:

- **Invariants** (`checks.py`), always on, for every turn: no personal data anywhere, only guarded SQL, no brand of another user, no destructive
  action without the typed confirmation, inside the budget, no crash.
- **Expectations** (`expect:`), per turn: the outcome, figures that must match the sample data (the right answer is computed from the data
  directly, never from the model), words that must or must not appear, and what the trace shows.
- **Advisory** expectations (`advisory:`): the same checks, reported as warnings. For what depends on how well the model writes.

`runner.py` runs them, `report.py` prints and stores the results.
"""
