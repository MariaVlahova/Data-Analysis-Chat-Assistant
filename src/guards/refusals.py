"""What the user is told when the SQL Guard refuses a query (plan 05, F32).

One plain sentence or two per rule ID. No SQL, no table or column names from the query, and never a list of the
blocked columns: the text says what kind of request it was, and what the agent can do instead. Plan 07 shows it.
The rule IDs are stable strings: traces, metrics and the evaluation set (plan 09) key on them.
"""
from __future__ import annotations

from src.errors import AgentError, ErrorKind, default_message

REFUSALS: dict[str, str] = {
    "MULTI_STATEMENT": "I can only run one query at a time, so I did not run that.",
    "NOT_SELECT": "I can only read data. I can't add, change or delete anything in the store's data.",
    "DISALLOWED_FUNCTION": "That needs a function I'm not allowed to use, so I did not run it.",
    "WILDCARD_TABLE": "That needs tables I'm not allowed to use. I only use the store's orders, order items, products and customers.",
    "TABLE_NOT_ALLOWED": "That needs data I'm not allowed to use. I only use the store's orders, order items, products and customers.",
    "NO_DATA_TABLE": (
        "That query does not read any of the store's data, so I did not run it. I answer questions from the store's orders, "
        "order items, products and customers."
    ),
    "PII_COLUMN": (
        "That asks for personal details that could identify a customer, such as who they are or how to reach them. "
        "I never show those. I can show totals, trends and rankings instead."
    ),
    "PER_CUSTOMER_ROWS": (
        "I can't list customer-level records like that. I can show a ranked list of top customers by spend, "
        "or totals for groups of customers."
    ),
    "DEMOGRAPHIC_ROW_LEVEL": (
        "I can't show customer details such as age or location one customer at a time. "
        "I can show totals for groups of customers instead."
    ),
    "SCOPE_REWRITE_INCOMPLETE": "I couldn't safely limit that query to the data you may see, so I did not run it.",
    "UNGUARDED_QUERY": "That query did not pass my safety checks, so I did not run it.",
    "SESSION_LABEL": (
        "Numbers such as \"Customer #48213\" are names for this conversation only. They are not customer IDs, so I can't look up "
        "or follow one customer by them. I can show the ranked list again, or totals for groups of customers."
    ),
}

# The ranked-list text above promises something that `guards.allow_ranked_customer_lists: false` takes away.
NO_RANKED_LISTS_TEXT = "I can't list customer-level records. I can show totals for groups of customers instead."

_FALLBACK = default_message(ErrorKind.GUARD_VIOLATION)


def refusal_text(rule_id: str) -> str:
    """The user-facing sentence for a rule ID. An ID without an entry gets the generic refusal."""
    return REFUSALS.get(rule_id, _FALLBACK)


def violation(rule_id: str, detail: str = "", *, text: str | None = None) -> AgentError:
    """A `GUARD_VIOLATION` with the refusal text for the rule, or `text` when the usual one would promise something this
    run does not allow. `detail` is for the trace only: keep it short and free of SQL literals (the error masks it and
    cuts it to 300 characters)."""
    return AgentError(ErrorKind.GUARD_VIOLATION, text or refusal_text(rule_id), detail=f"{rule_id}: {detail}".rstrip(": "), rule_id=rule_id)
