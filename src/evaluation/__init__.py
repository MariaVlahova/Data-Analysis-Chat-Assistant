"""The judge on a real conversation: the `/eval` command of the chat (plan 09).

The chat never judges by itself. A turn makes no judge call, and nothing here runs until someone types `/eval`. `evidence.py` turns the turns of a
chat into what a judge may read; `judging.py` is the judge itself (the rubrics of `evalkit/rubrics.xlsx` over the model gateway, shared with the
offline `eval/run_judge.py`); `live.py` is the command.

Only this package imports `evalkit` (the generic scoring framework). Nothing in `src/` imports `eval/`, which stays test tooling.
"""
