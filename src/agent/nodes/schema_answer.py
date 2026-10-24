"""`describe_schema` (plan 07, F5): the answer to "what data do you have?".

No SQL and no model call after the Input Guard. The text is built from `catalog.overview()` (the allow-listed tables and
columns, each with one line of our own words; never a blocked column), a fixed line about personal details, and a short fixed
list of example questions. It goes through the Output Guard like every other answer.
"""
from __future__ import annotations

from src.agent.boundary import node
from src.agent.deps import FINAL_TEXT, Deps
from src.agent.state import TurnState

PERSONAL_DATA_LINE = "Personal contact details (names, e-mail addresses, phone numbers, street addresses) are not available to me."
EXAMPLE_QUESTIONS = (
    "What was monthly revenue for my brands this year?",
    "Who are my top customers by spend?",
    "Compare product X and product Y, and say why they perform differently.",
    "Why are customers in one state spending less than in another?",
    "Create a report for Q1 with insights and action items for Q2.",
)


def make_describe_schema(deps: Deps):
    @node(deps, "describe_schema")
    def describe_schema(state: TurnState) -> dict:
        examples = "\n".join(f"- {question}" for question in EXAMPLE_QUESTIONS)
        text = f"{deps.catalog.overview()}\n\n{PERSONAL_DATA_LINE}\n\nYou can ask, for example:\n{examples}"
        deps.runtime(state).hold(FINAL_TEXT, text)  # held here until the Output Guard has passed it
        return {"partial": False}

    return describe_schema
