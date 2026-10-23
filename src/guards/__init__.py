"""The guards that sit between model output and the world (plans 05 and 06).

Plan 05: `allowlist.py`, `sql_guard.py`, `scope_views.py`, `refusals.py`.
Plan 06: `result_guard.py`, `pseudonymizer.py`, `output_guard.py`.
Import from the submodules: this file stays empty so that `src.guards.query` (which the query runner needs) never pulls
in sqlglot, and `src.guards.pseudonymizer` never pulls in pandas.
"""
