"""The Result Guard: what happens to query rows before the model, the graph state or the user can see them (plan 06).

Plan 07 calls `SafeQueryRunner.run()` and `ResultGuard.apply()` inside the same node function. The raw DataFrame is a
local variable of that function and never leaves it. Only a `GuardedResult` does, and every cell in it is a plain
value that has been through the steps below.

Steps, in order, all inside one fail-closed wrapper (Result Guard note, part E):
  A. column masking     a column whose name is sensitive has every value replaced by `[MASKED]`
  B. value scan         a text cell has e-mail, phone, ID, card, address, coordinates and postal codes replaced by tokens
  C. pseudonymize       a customer ID column has every cell replaced by a fake ID that is fixed for the session
  S. small groups       a result about groups of customers loses the groups with fewer than `guards.k_anonymity`
                        customers (S7); if the group sizes cannot be checked the whole result is withheld
  D. cap                at most `guards.rows_to_model` rows are kept, after suppression
Then the summary (counts and min/max/sum, never rows), the notices the Composer must pass on, and one `guard.decision`.

Fail closed: any exception, any cell type we do not handle, a query that is not the SQL Guard's own, or a run that takes
longer than the time limit drops the payload and raises RESULT_GUARD_FAILED. Nothing partial goes through.

Known residual risk, not solved here (plan 06): hiding one small group but showing a total can reveal the hidden group
("complementary suppression").
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Literal, Mapping

import numpy as np
import pandas as pd

from src import pii
from src.config import Settings
from src.errors import AgentError, ErrorKind, crash_summary
from src.guards.allowlist import Allowlist, fold
from src.guards.pseudonymizer import SessionPseudonymizer
from src.guards.query import ALL_BRANDS_LABEL, GuardedQuery
from src.observability import current_trace_id, guard_decision, span

log = logging.getLogger(__name__)

MASKED = "[MASKED]"
TIMEOUT_S = 5.0  # a guard that runs this long has a problem; 1,000 rows take milliseconds
MAX_NESTING = 8  # arrays inside structs inside arrays
# A regex cannot be interrupted, so the guard bounds what it feeds one: a cell above these limits fails closed. They are
# far above anything the model should be sent (50 rows of them would be a million tokens).
MAX_CELL_CHARS = 20_000
MAX_CELL_NODES = 2_000  # values inside one array or struct cell; 2,000 numbers are about 10,000 characters once written out

Status = Literal["ok", "empty", "withheld"]

# The notice texts (plan 07 renders them into the answer, so it never implies a complete list when it is not) ---------
NOTICE_WITHHELD = "Group sizes could not be checked, so the result was withheld."
NOTICE_CUSTOMER_LABELS = "Customer numbers are labels for this session only."
NOTICE_CUSTOMER_SPEND_SCOPE = "Each customer's spend counts only purchases in your brands."  # S15
NOTICE_VALUES_MASKED = "Some personal details inside text values were hidden."


def notice_groups_hidden(count: int, k: int) -> str:
    noun, verb = ("group", "was") if count == 1 else ("groups", "were")
    return f"{count:,} {noun} with fewer than {k} customers {verb} hidden."


def notice_columns_masked(count: int) -> str:
    noun, verb, pronoun = ("column", "was", "it") if count == 1 else ("columns", "were", "they")
    return f"{count:,} {noun} {verb} hidden because {pronoun} held personal data."


def notice_truncated(shown: int, total: int) -> str:
    return f"Showing top {shown:,} of {total:,} rows."


def notice_not_checked(count: int) -> str:
    noun = "row was" if count == 1 else "rows were"
    return f"{count:,} more {noun} not checked, so this result may be incomplete."


# Column names (step A) -----------------------------------------------------------------------------------------------
# A name is split into WORDS (separators and camelCase), and the words are compared, not the letters: `smartphone_revenue`,
# `headphones_sold` and `addressable_market` are metrics, `phone_number`, `E-mail` and `firstName` are not. `name` alone is
# not sensitive (it is the product name) but a person word next to it is (`customer_name`, `user_name`).
_SENSITIVE_WORDS = frozenset(
    "email emails phone telephone tel address addresses street postcode postal zip zipcode surname forename firstname lastname "
    "fullname givenname username latitude longitude lat lng lon ssn dob birth birthday birthdate passport".split()
)
_PERSON_WORDS = frozenset(
    "customer user client buyer shopper contact account person owner recipient cardholder first last full given family middle "
    "sur fore".split()
)
_SENSITIVE_PHRASES = (  # every word of one of these, in any order
    ("e", "mail"), ("post", "code"), ("credit", "card"), ("card", "number"), ("social", "security"),
    ("mobile", "number"), ("mobile", "phone"), ("cell", "phone"), ("cell", "number"),
)
_SENSITIVE_WHOLE_NAMES = frozenset({"mobile", "cell"})  # alone they are a phone number; `mobile_orders` is a metric
# Words that end in one of these are the sensitive word with something in front: `useremail`, `homeaddress`. `phone` is not
# here on purpose (`smartphone`), and neither is a prefix rule (`addressable`).
_SENSITIVE_SUFFIXES = ("email", "address", "surname", "firstname", "lastname", "fullname", "postcode", "zipcode", "latitude", "longitude")
# A column that holds a customer ID, whatever the query called it. `id` alone is not one: it is as likely a product ID,
# so the SQL Guard names the ID columns it found (`GuardedQuery.id_columns`). `number`, `num` and `no` are not here: they
# name counts as often as IDs (`buyers_number`), and a count must not become `Customer #21257`.
_CUSTOMER_ID_NAME = re.compile(r"(?:^|[\W_])(?:user|customer|cust|buyer|shopper|client)s?[\W_]*(?:id|ids|key)$")
_SEPARATORS = re.compile(r"[\W_]+")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _spelled(name: str) -> str:
    """A column name in the form it is compared in: camelCase split into words, then folded (case, width)."""
    return fold(_CAMEL_BOUNDARY.sub("_", name))


def _is_sensitive_name(spelled: str) -> bool:
    words = [word for word in _SEPARATORS.split(spelled) if word]
    present = set(words)
    if "".join(words) in _SENSITIVE_WHOLE_NAMES or present & _SENSITIVE_WORDS:
        return True
    if "name" in present and present & _PERSON_WORDS:
        return True
    if any(all(part in present for part in phrase) for phrase in _SENSITIVE_PHRASES):
        return True
    return any(word != suffix and word.endswith(suffix) for word in words for suffix in _SENSITIVE_SUFFIXES) or any(
        word == person + "name" for word in words for person in _PERSON_WORDS  # `customername`
    )


@dataclass
class GuardedResult:
    columns: list[str]
    rows: list[dict]  # at most `rows_to_model`, already masked. Safe for the model and the state.
    total_rows: int  # after suppression, before the cap
    suppressed_groups: int
    truncated: bool
    masked_columns: list[str]
    notices: list[str]  # plain sentences the Composer must pass on
    status: Status
    summary: dict  # row count, columns, per numeric column min/max/sum, null counts. Never rows.


@dataclass
class _Counts:
    """What the guard changed. Only counts: they go into the span and the `guard.decision`, so no value can."""

    columns_masked: int = 0
    cells_masked: int = 0  # every cell replaced by a mask: whole columns and values found by a pattern
    values_masked: int = 0  # the part of `cells_masked` that a pattern found inside a text value
    ids_replaced: int = 0
    groups_hidden: int = 0
    rows_cut: int = 0

    @property
    def first_rule(self) -> str | None:
        """The rule ID of the first change, or None when the rows went through untouched."""
        for rule, count in (("COLUMN_MASKED", self.columns_masked), ("VALUE_MASKED", self.values_masked),
                            ("IDS_REPLACED", self.ids_replaced), ("GROUPS_HIDDEN", self.groups_hidden),
                            ("ROWS_CAPPED", self.rows_cut)):
            if count:
                return rule
        return None


def _fail(detail: str) -> AgentError:
    return AgentError(ErrorKind.RESULT_GUARD_FAILED, detail=detail, trace_id=current_trace_id())


# Cells (step B) ----------------------------------------------------------------------------------------------------

def _is_null(value: Any) -> bool:
    if value is None or value is pd.NA or value is pd.NaT:
        return True
    return isinstance(value, float) and math.isnan(value)


def _scalar(value: Any) -> Any:
    """A cell as a plain Python value. Raises TypeError for a type we do not handle, which fails the guard closed."""
    if isinstance(value, np.datetime64):
        return None if np.isnat(value) else pd.Timestamp(value).isoformat()
    if isinstance(value, np.timedelta64):
        raise TypeError("duration cells are not supported")
    if isinstance(value, np.generic):
        value = value.item()
    if _is_null(value):
        return None
    if isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None  # JSON has no Infinity
    if isinstance(value, Decimal):
        if not value.is_finite():
            return None
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):  # pd.Timestamp is a datetime
        return value.isoformat()
    raise TypeError(f"cell type {type(value).__name__} is not supported")


def _nested(value: Any, budget: list[int] | None = None, depth: int = 0) -> Any:
    """An array or struct cell as nested lists, dicts and plain values, ready for `json.dumps`.

    `budget` is how many more values the whole cell may hold, counted as the length of each array or struct met. The size
    is checked before a big array is copied."""
    budget = [MAX_CELL_NODES] if budget is None else budget
    if depth > MAX_NESTING:
        raise TypeError("cell is nested too deeply")
    size = value.size if isinstance(value, np.ndarray) else len(value) if isinstance(value, (Mapping, list, tuple)) else 0
    budget[0] -= size
    if budget[0] < 0:
        raise TypeError("cell holds too many values")
    if isinstance(value, np.ndarray):
        value = value.tolist()
    if isinstance(value, Mapping):
        return {str(key): _nested(item, budget, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_nested(item, budget, depth + 1) for item in value]
    return _scalar(value)


def _mask_strings(value: Any) -> tuple[Any, bool]:
    """The nested value with every string (and every key) masked, and whether anything changed."""
    if isinstance(value, str):
        masked = pii.mask(value, extended=True)
        return masked, masked != value
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        changed = False
        for key, item in value.items():
            masked_key, key_changed = _mask_strings(key)
            masked_item, item_changed = _mask_strings(item)
            out[masked_key] = masked_item
            changed = changed or key_changed or item_changed
        return out, changed
    if isinstance(value, list):
        pairs = [_mask_strings(item) for item in value]
        return [item for item, _ in pairs], any(changed for _, changed in pairs)
    return value, False


def _k_value(value: Any) -> int:
    """The distinct-customer count of a group. Anything else than a whole number cannot be verified."""
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, bool) or value is None:
        raise TypeError("group size is not a number")
    if isinstance(value, (float, Decimal)) and value == value and value == int(value):
        return int(value)
    if isinstance(value, int):
        return value
    raise TypeError("group size is not a whole number")


def _unique(names: list[str]) -> list[str]:
    """Output names: a repeated name (`id`, `id`) becomes `id`, `id_2`, so every cell keeps its own key."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for name in names:
        seen[name] = seen.get(name, 0) + 1
        out.append(name if seen[name] == 1 else f"{name}_{seen[name]}")
    taken = set(out)
    if len(taken) != len(out):  # `id_2` was also a real column
        raise _fail("two result columns have the same name")
    return out


class ResultGuard:
    def __init__(
        self,
        settings: Settings,
        allowlist: Allowlist,
        pseudonymizer: SessionPseudonymizer,
        *,
        timeout_s: float = TIMEOUT_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._k = settings.guards.k_anonymity
        self._cap = settings.guards.rows_to_model
        self._blocked = allowlist.blocked_names
        self._blocked_squashed = frozenset(_SEPARATORS.sub("", name) for name in allowlist.blocked_names)
        self._id_names = frozenset(fold(column) for _, column in allowlist.customer_id_columns if fold(column) != "id")
        self._pseudonymizer = pseudonymizer
        self._timeout_s = timeout_s
        self._clock = clock

    # entry point ------------------------------------------------------------------------------------------------

    def apply(self, df: pd.DataFrame, gq: GuardedQuery, total_rows: int) -> GuardedResult:
        """Raises RESULT_GUARD_FAILED. `total_rows` is the full count BigQuery reported, which is more than `len(df)`
        when the runner cut the frame.

        One wrapper covers everything, the tracing calls too: if recording the decision fails, the caller still gets the
        typed error and no rows (part E), not an arbitrary exception that plan 07 would report as INTERNAL."""
        with span("guard.result", "guard") as sp:
            try:
                result, counts = self._apply(df, gq, total_rows)
                self._record(sp, result, counts)
            except Exception as exc:  # noqa: BLE001 - fail closed: whatever went wrong, no rows go through
                err = exc if isinstance(exc, AgentError) and exc.kind is ErrorKind.RESULT_GUARD_FAILED else _fail(crash_summary(exc))
                try:
                    guard_decision("result", "block", rule="GUARD_ERROR")
                except Exception as logging_exc:  # noqa: BLE001 - the typed error below is what the caller must get
                    log.error("could not record the Result Guard failure: %s", crash_summary(logging_exc))
                raise err from None
            return result

    @staticmethod
    def _record(sp: Any, result: GuardedResult, counts: _Counts) -> None:
        detail = dict(vars(counts), status=result.status)
        sp.set(rows=len(result.rows), **detail)
        if result.status == "withheld":
            guard_decision("result", "block", rule="GROUP_SIZE_UNKNOWN", **detail)
        elif counts.first_rule:
            guard_decision("result", "rewrite", rule=counts.first_rule, **detail)
        else:
            guard_decision("result", "pass", **detail)

    # the steps --------------------------------------------------------------------------------------------------

    def _apply(self, df: pd.DataFrame, gq: GuardedQuery, total_rows: int) -> tuple[GuardedResult, _Counts]:
        deadline = self._clock() + self._timeout_s
        if not (isinstance(gq, GuardedQuery) and gq.authentic):
            # A look-alike could say `id_columns=()` and let real customer IDs through.
            raise _fail("the query is not one the SQL Guard issued")
        if not isinstance(df, pd.DataFrame):
            raise _fail("the result is not a table")
        if isinstance(total_rows, bool) or not isinstance(total_rows, int) or total_rows < 0:
            raise _fail("the row count is not a whole number")

        counts = _Counts()
        originals = [str(column) for column in df.columns]
        if any(len(name) > MAX_CELL_CHARS for name in originals):
            raise _fail("a column name is too long to check")
        roles = [self._role(name, gq) for name in originals]
        self._check_roles(roles, originals, gq)
        # A header can be data too (a PIVOT names its columns after values), so it is scanned like a cell. The roles above
        # were decided on the real names; the keys of the rows are the cleaned ones.
        headers = [pii.mask(name, extended=True) for name in originals]
        renamed = sum(shown != real for shown, real in zip(headers, originals))
        counts.cells_masked += renamed
        counts.values_masked += renamed
        names = _unique(headers)
        keep = [i for i, role in enumerate(roles) if role != "k"]  # the group-size column is dropped from the output
        out_names = [names[i] for i in keep]
        masked_names = [names[i] for i in keep if roles[i] == "masked"]
        counts.columns_masked = len(masked_names)

        if len(df) == 0:
            return self._build(out_names, [], masked_names, gq, counts, hidden=0, unseen=0), counts
        if gq.demographic and (gq.k_column is None or gq.per_customer):
            # Group sizes cannot be verified (or customer rows sit next to demographics): fail closed, S7.
            return self._withheld(), counts

        k_index = roles.index("k") if "k" in roles else None
        rows: list[list[Any]] = []
        sizes: list[int] = []
        for raw in df.itertuples(index=False, name=None):
            self._check_time(deadline)
            if k_index is not None:
                sizes.append(_k_value(raw[k_index]))
            rows.append([self._cell(roles[i], raw[i], counts, deadline) for i in keep])

        hidden = 0
        if k_index is not None:  # S: remove the groups of fewer than k customers, before the cap
            survivors = [row for row, size in zip(rows, sizes) if size >= self._k]
            hidden, rows = len(rows) - len(survivors), survivors
        result = self._build(out_names, rows, masked_names, gq, counts, hidden=hidden, unseen=max(0, total_rows - len(df)))
        self._check_time(deadline)  # the summary and the notices count too
        return result, counts

    def _role(self, name: str, gq: GuardedQuery) -> Literal["masked", "k", "id", "plain"]:
        spelled = _spelled(name)
        if fold(name) in self._blocked or _SEPARATORS.sub("", spelled) in self._blocked_squashed or _is_sensitive_name(spelled):
            return "masked"
        if gq.demographic and gq.k_column is not None and fold(name) == fold(gq.k_column):
            return "k"
        if fold(name) in {fold(column) for column in gq.id_columns} or fold(name) in self._id_names or _CUSTOMER_ID_NAME.search(spelled):
            return "id"
        return "plain"

    @staticmethod
    def _check_roles(roles: list[str], names: list[str], gq: GuardedQuery) -> None:
        if gq.demographic and gq.k_column is not None and "k" not in roles:
            raise _fail("the group-size column is missing from the result")
        if gq.per_customer:
            declared = {fold(column) for column in gq.id_columns}
            # If the customer ID came back under a name the SQL Guard did not expect, it would pass as ordinary data.
            if not declared or not any(role == "id" and fold(name) in declared for role, name in zip(roles, names)):
                raise _fail("the customer ID column of a ranked list is missing from the result")

    def _cell(self, role: str, value: Any, counts: _Counts, deadline: float) -> Any:
        self._check_time(deadline)  # also between the cells of one row, so a row of long cells cannot run on
        if role == "masked":  # every value, nulls too: which rows had a value is information as well
            counts.cells_masked += 1
            return MASKED
        if role == "id":
            if _is_null(value):
                return None
            counts.ids_replaced += 1
            return self._pseudonymizer.fake_id(value)  # the raw cell: a derived string such as 'c' || id is replaced whole
        if isinstance(value, (np.ndarray, list, tuple, Mapping)):
            return self._scan_nested(_nested(value), counts)
        value = _scalar(value)
        return self._scan(value, counts) if isinstance(value, str) else value

    @staticmethod
    def _scan(text: str, counts: _Counts) -> str:
        if len(text) > MAX_CELL_CHARS:
            raise _fail("a cell is too long to check")
        masked = pii.mask(text, extended=True)
        if masked != text:
            counts.cells_masked += 1
            counts.values_masked += 1
        return masked

    @staticmethod
    def _scan_nested(value: Any, counts: _Counts) -> str:
        """An array or struct as JSON text, scanned twice: every string inside it as the text it is, and then the JSON.

        JSON writes a line break inside a string as the two characters `\\n`, which the address pattern cannot read across, so
        the strings are scanned before they are written. The JSON text is scanned too, for what only shows across values."""
        if len(json.dumps(value, ensure_ascii=False, allow_nan=False)) > MAX_CELL_CHARS:
            raise _fail("a cell is too long to check")
        masked_value, changed = _mask_strings(value)
        text = json.dumps(masked_value, ensure_ascii=False, allow_nan=False)
        masked = pii.mask(text, extended=True)
        if changed or masked != text:
            counts.cells_masked += 1
            counts.values_masked += 1
        return masked

    def _check_time(self, deadline: float) -> None:
        if self._clock() > deadline:
            raise TimeoutError("the Result Guard ran out of time")

    # the result -------------------------------------------------------------------------------------------------

    @staticmethod
    def _withheld() -> GuardedResult:
        return GuardedResult(
            columns=[], rows=[], total_rows=0, suppressed_groups=0, truncated=False, masked_columns=[],
            notices=[NOTICE_WITHHELD], status="withheld", summary=_summary([], []),
        )

    def _build(
        self, names: list[str], rows: list[list[Any]], masked_names: list[str], gq: GuardedQuery, counts: _Counts,
        *, hidden: int, unseen: int,
    ) -> GuardedResult:
        """`rows` are the rows that survived suppression. `unseen` counts the rows past the runner's cut: the guard
        never saw them, so the total counts them as they are."""
        shown = rows[: self._cap]  # D: the cap comes after the suppression
        total = len(rows) + unseen
        truncated = total > len(shown)
        counts.groups_hidden, counts.rows_cut = hidden, len(rows) - len(shown)

        notices: list[str] = []
        if masked_names:
            notices.append(notice_columns_masked(len(masked_names)))
        if counts.values_masked:
            notices.append(NOTICE_VALUES_MASKED)
        if hidden:
            notices.append(notice_groups_hidden(hidden, self._k))
        if gq.per_customer and shown:
            notices.append(NOTICE_CUSTOMER_LABELS)
            if gq.scope_label != ALL_BRANDS_LABEL:
                notices.append(NOTICE_CUSTOMER_SPEND_SCOPE)
        if truncated and shown:
            notices.append(notice_truncated(len(shown), total))
        elif unseen and not shown:
            # Everything the guard could look at was hidden, and the rest was never looked at: "all groups were hidden"
            # alone would say the result is complete.
            notices.append(notice_not_checked(unseen))

        return GuardedResult(
            columns=list(names),
            rows=[dict(zip(names, row)) for row in shown],
            total_rows=total,
            suppressed_groups=hidden,
            truncated=truncated,
            masked_columns=list(masked_names),
            notices=notices,
            status="ok" if shown else "empty",
            summary=_summary(names, rows),
        )


def _summary(names: list[str], rows: list[list[Any]]) -> dict:
    """Row count, columns, min/max/sum of each numeric column and null counts, from the guarded rows only."""
    numeric: dict[str, dict[str, int | float]] = {}
    nulls: dict[str, int] = {}
    for index, name in enumerate(names):
        values = [row[index] for row in rows]
        present = [value for value in values if value is not None]
        nulls[name] = len(values) - len(present)
        if present and all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in present):
            total = sum(present) if all(isinstance(value, int) for value in present) else math.fsum(present)
            numeric[name] = {"min": min(present), "max": max(present), "sum": total}
    return {"row_count": len(rows), "columns": list(names), "numeric": numeric, "nulls": nulls}
