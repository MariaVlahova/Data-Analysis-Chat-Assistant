"""What each step may see (plan 07, "Prompt contracts"; the step-input tables of the requirements folder).

A step sees only what it needs. The table is data here, and every prompt builder in `templates.py` reports which inputs it
used. `tests/test_prompt_contracts.py` checks each built prompt against its row, so a later edit that hands the SQL writer
the persona, or the Composer the schema, fails a test instead of drifting in unnoticed.

The builders also have no parameter for what a step must never get: the SQL writer's builder takes no user and no persona.
"""
from __future__ import annotations

from dataclasses import dataclass

# Input names (a row of the tables)
QUESTION = "question"  # the standalone question and the intent
RAW_QUESTION = "raw_question"  # the user's own message, for per-turn overrides and for the Input Guard
HISTORY = "history"  # earlier messages, for the Input Guard only
SUBQUESTION = "subquestion"  # one sub-question's text (the SQL writer's own)
PLAN = "plan"  # all sub-questions, date range, assumptions
PLAN_PARAMS = "plan_params"  # the parameters of one sub-question: the date range
GLOSSARY = "glossary"
GOLDEN_NOTES = "golden_notes"  # question and interpretation notes
GOLDEN_SQL = "golden_sql"
GOLDEN_REPORT = "golden_report"
SCHEMA_NAMES = "schema_names"  # table and column names, one line each (Planner)
SCHEMA_SLICE = "schema_slice"  # the pruned slice for one sub-question (SQL writer)
ALLOWED_TABLES = "allowed_tables"
RULES = "rules"
DATES = "dates"  # today and the dataset's latest date (Planner)
RESOLVED_DATES = "resolved_dates"  # the resolved range, not today (SQL writer)
EARLIER_RESULTS = "earlier_results"  # guarded results of a declared dependency
RESULTS = "results"  # guarded results of every sub-question (Composer)
STATUSES = "statuses"
NOTICES = "notices"
SCOPE_LABEL = "scope_label"  # one line, not the user's identity
PERSONA = "persona"
FAILING_SQL = "failing_sql"  # the failing SQL and its normalized error, on a repair only
MISSING = "missing"  # a replan: what the Evidence Check found missing
SUMMARIES = "summaries"  # guarded summaries, never rows (Evidence Check, replan)


@dataclass(frozen=True)
class Contract:
    required: frozenset[str]
    allowed: frozenset[str]  # everything the step may see; `required` is part of it


CONTRACTS: dict[str, Contract] = {
    "input_guard": Contract(
        required=frozenset({RAW_QUESTION, RULES}),
        allowed=frozenset({RAW_QUESTION, HISTORY, RULES}),
    ),
    "planner": Contract(
        required=frozenset({QUESTION, SCHEMA_NAMES, RULES, DATES, ALLOWED_TABLES}),
        allowed=frozenset({QUESTION, GLOSSARY, GOLDEN_NOTES, SCHEMA_NAMES, RULES, DATES, ALLOWED_TABLES, MISSING, SUMMARIES}),
    ),
    "sql_writer": Contract(
        required=frozenset({SUBQUESTION, SCHEMA_SLICE, RULES, RESOLVED_DATES}),
        allowed=frozenset({SUBQUESTION, PLAN_PARAMS, GLOSSARY, GOLDEN_NOTES, GOLDEN_SQL, SCHEMA_SLICE, RULES, RESOLVED_DATES,
                           EARLIER_RESULTS, FAILING_SQL}),
    ),
    "evidence_check": Contract(
        required=frozenset({QUESTION, PLAN, STATUSES, SUMMARIES, RULES}),
        allowed=frozenset({QUESTION, PLAN, STATUSES, SUMMARIES, RULES}),
    ),
    "composer": Contract(
        required=frozenset({QUESTION, PLAN, RESULTS, STATUSES, SCOPE_LABEL, RULES}),
        allowed=frozenset({QUESTION, RAW_QUESTION, PLAN, RESULTS, STATUSES, NOTICES, SCOPE_LABEL, GLOSSARY, GOLDEN_REPORT, RULES,
                           PERSONA}),
    ),
}


def violations(step: str, used: frozenset[str]) -> list[str]:
    """What is wrong with the inputs a prompt used: inputs the step must not see, and required inputs that are missing."""
    contract = CONTRACTS[step]
    problems = [f"{step} must not see {name}" for name in sorted(used - contract.allowed)]
    problems += [f"{step} must see {name}" for name in sorted(contract.required - used)]
    return problems
