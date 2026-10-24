"""What the SQL Guard hands to `SafeQueryRunner` (plan 05).

Kept free of sqlglot and of the BigQuery library. The second is a rule: nothing outside `data_access/` may import a
BigQuery client (`tests/test_no_direct_execute.py`), so a parameter here is plain data and the runner turns it into
`bigquery.ArrayQueryParameter`.

A `GuardedQuery` is the proof that the checks ran, so it must not be forgeable. Being an instance is not enough:
`dataclasses.replace(gq, sql=...)` and a copy with one field changed are instances too. So `SQLGuard.check` seals every
field with a key that exists only in this process, and the runner refuses a query whose seal does not match its
fields. Consequences, all intended: a changed field, a hand-made instance and a query that came from another process
are refused; a copy, a deepcopy and a pickle round trip inside the process stay valid; the class cannot be subclassed.
`tests/test_guarded_query_origin.py` also fails if anything but the guard calls `GuardedQuery.issue`.
"""
from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass, field
from typing import Any, Sequence

_KEY = secrets.token_bytes(32)  # never written anywhere; a new one per process

# `GuardedQuery.scope_label` for a user who sees every brand. The SQL Guard writes it and the Result Guard compares with it
# (plan 06, S15), so both use this one constant.
ALL_BRANDS_LABEL = "all brands"


def scope_label_for(mode: str, brands: Sequence[str]) -> str:
    """The words for what a user may see: `all brands`, or `your brands: A, B`. The one place that writes them: the SQL Guard puts
    them on every query, and the answer's scope line (`policy_context`) says the same."""
    return ALL_BRANDS_LABEL if mode == "all" else "your brands: " + ", ".join(brands)


@dataclass(frozen=True)
class ArrayParam:
    """A named array query parameter, for example `ArrayParam("brands", "STRING", ("Nike", "Levi's"))`.

    Values travel as parameters and are never pasted into the SQL text.
    """

    name: str
    element_type: str
    values: tuple[str, ...]


def _plain(value: Any) -> Any:
    """A value in a form `json` can write, the same way every time."""
    if isinstance(value, ArrayParam):
        return [value.name, value.element_type, list(value.values)]
    if isinstance(value, (frozenset, set)):
        return sorted(value)
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


@dataclass(frozen=True)
class GuardedQuery:
    sql: str  # rewritten, generated from the tree, never the original text
    params: tuple[ArrayParam, ...]  # `brands` for a brand manager; empty for the CEO
    tables: frozenset[str]  # tables the model's query named
    id_columns: tuple[str, ...]  # output columns that carry customer IDs (for the Result Guard)
    per_customer: bool  # a ranked per-customer list
    demographic: bool  # touches a quasi-identifier column anywhere
    k_column: str | None  # alias of the injected distinct-customer count, or None
    limit: int  # the LIMIT of the outermost query as it runs
    rewrites: tuple[str, ...]  # rule IDs applied, for the trace
    fingerprint: str  # hash of the normalized original tree, for "same SQL twice" (3.10)
    scope_label: str  # "your brands: A, B" or ALL_BRANDS_LABEL, for the Composer (S15)
    seal: str = field(default="", repr=False)  # set by `issue`; see the module docstring

    def __init_subclass__(cls, **kwargs: Any) -> None:
        raise TypeError("GuardedQuery cannot be subclassed: a subclass could say its own queries are authentic")

    @classmethod
    def issue(cls, **fields: Any) -> "GuardedQuery":
        """Make a sealed query. Only `SQLGuard.check` calls this."""
        query = cls(**fields)
        return dataclasses.replace(query, seal=query._expected_seal())

    def _expected_seal(self) -> str:
        body = {f.name: _plain(getattr(self, f.name)) for f in dataclasses.fields(self) if f.name != "seal"}
        return hmac.new(_KEY, json.dumps(body, sort_keys=True).encode("utf-8"), hashlib.sha256).hexdigest()

    @property
    def authentic(self) -> bool:
        """True when every field is still what `issue` sealed. Never raises: a field that cannot be written out, or a seal
        that is not text, is simply not authentic."""
        try:
            return bool(self.seal) and hmac.compare_digest(self.seal.encode("utf-8"), self._expected_seal().encode("utf-8"))
        except Exception:  # noqa: BLE001 - TypeError for bytes in a field, AttributeError for a seal that is not a str ...
            return False
