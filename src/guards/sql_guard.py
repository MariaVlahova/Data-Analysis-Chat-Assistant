"""The SQL Guard (plan 05, decision 3.4 and 3.5, F11 to F13, F17, F32).

`SQLGuard.check` takes the SQL text the model wrote and returns a `GuardedQuery`, or raises. It checks the sqlglot parse
tree, never the text, so comments, odd spacing and keyword splitting cannot hide anything. The SQL that runs is
generated from the rewritten tree, never the original text.

Steps, in order. The first failure stops the check:
 0  the user has access at all                              NO_ACCESS
 1  the text parses                                         SQL_SYNTAX (repairable)
 2  one statement, and it is a SELECT                       MULTI_STATEMENT, NOT_SELECT
 3  no denied function, table function or system variable   DISALLOWED_FUNCTION
 4  only the four tables of the configured dataset          TABLE_NOT_ALLOWED, WILDCARD_TABLE
 5  no blocked column name anywhere                         PII_COLUMN
 6  every column resolves, and its lineage is known         SQL_UNKNOWN_COLUMN (repairable)
 7  customer-row and demographic rules R1 to R5             PER_CUSTOMER_ROWS, DEMOGRAPHIC_ROW_LEVEL
 8  scope rewrite (brand filter, allowed columns only)      SCOPE_REWRITE_INCOMPLETE
 9  distinct-customer count for the Result Guard, LIMIT
10  SQL text from the tree, read back and checked again

A guard violation is never retried (3.11). `SQL_SYNTAX` and `SQL_UNKNOWN_COLUMN` are the model's own mistakes and
the SQL loop may repair them. Code enforces every rule; nothing here relies on the prompt.
"""
from __future__ import annotations

import functools
import hashlib
import logging
import re

import sqlglot
from sqlglot import exp, transforms
from sqlglot.dialects.bigquery import BigQuery
from sqlglot.errors import OptimizeError, ParseError, TokenError
from sqlglot.optimizer.qualify import qualify
from sqlglot.tokens import TokenType

from src.access.users import UserContext, require_access
from src.config import ALWAYS_DENIED_FUNCTIONS, Settings
from src.data_access.errors_map import normalize_message
from src.errors import AgentError, ConfigError, ErrorKind, crash_summary
from src.guards.allowlist import Allowlist, fold
from src.guards.lineage import Analysis, LineageTooComplex, analyze, outer_selects, source_names, unwrap
from src.guards.query import ArrayParam, GuardedQuery
from src.guards.refusals import NO_RANKED_LISTS_TEXT, violation
from src.guards.scope_views import BRANDS_PARAM, ScopeViews, ViewMode, base_table_refs, strip_table_paths
from src.observability import guard_decision, span

# sqlglot logs a warning that quotes part of the SQL when it falls back to a generic command. The log filter masks
# patterns, not string literals, so keep that text out of the logs. (The error we raise carries no SQL.)
logging.getLogger("sqlglot").setLevel(logging.ERROR)

K_ALIAS = "__k_users"  # the injected distinct-customer count (R5); extended with "_" if the query already uses it

# Function namespaces that are part of BigQuery itself. Any other `name.name(...)` call is a function stored in some
# dataset, which could read tables the allow-list does not cover.
BUILTIN_NAMESPACES = frozenset({"SAFE", "NET", "HLL_COUNT", "KLL_QUANTILES", "AEAD", "KEYS"})



def _transform_list(function: object) -> list | None:
    """The list of helper functions inside one of sqlglot's `transforms.preprocess(...)` results, or None."""
    for cell in getattr(function, "__closure__", None) or ():
        try:
            contents = cell.cell_contents
        except ValueError:  # an empty cell
            continue
        if isinstance(contents, list) and contents and all(callable(item) for item in contents):
            return contents
    return None


@functools.lru_cache(maxsize=1)
def _bigquery_dialect() -> type:
    """BigQuery, with one of sqlglot's helpers for translating from other dialects left out of the SELECT.

    The helper is `_alias_ordered_group`. It swaps `GROUP BY <expression>` for the SELECT alias whenever there is an
    ORDER BY. If the alias is also the name of a column in FROM (`DATE_TRUNC(created_at, MONTH) AS created_at`), the
    GROUP BY then names that column instead of the month. The model wrote BigQuery for BigQuery, so its GROUP BY goes
    out as written.

    The class is built from sqlglot's own list for the SELECT with that one helper removed, so whatever else the
    installed version applies stays. If the list is not where this expects it, or the helper is not in it, sqlglot has
    changed under us: stop at startup, because the SQL that runs is generated from this class.
    """
    chain = _transform_list(BigQuery.Generator.TRANSFORMS.get(exp.Select))
    if not chain or "_alias_ordered_group" not in {getattr(item, "__name__", "") for item in chain}:
        raise ConfigError(
            f"This version of sqlglot ({sqlglot.__version__}) builds BigQuery SELECTs in a way the SQL Guard has not been checked against.",
            fix="Run: pip install -r requirements.txt (it pins the sqlglot 30.x the guard was built on).",
        )
    kept = [item for item in chain if getattr(item, "__name__", "") != "_alias_ordered_group"]

    class BigQueryAsWritten(BigQuery):
        class Generator(BigQuery.Generator):
            TRANSFORMS = {**BigQuery.Generator.TRANSFORMS, exp.Select: transforms.preprocess(kept)}

    # Whatever the mechanism, what matters is what comes out. The probe is the case the helper breaks.
    probe = sqlglot.parse_one(
        "SELECT DATE_TRUNC(created_at, MONTH) AS created_at, COUNT(*) AS n FROM t "
        "GROUP BY DATE_TRUNC(created_at, MONTH) ORDER BY created_at", read="bigquery",
    )
    if "GROUP BY DATE_TRUNC(created_at, MONTH)" not in probe.sql(dialect=BigQueryAsWritten, comments=False):
        raise ConfigError(
            f"This version of sqlglot ({sqlglot.__version__}) still rewrites a GROUP BY into its alias, which the SQL Guard does not allow.",
            fix="Run: pip install -r requirements.txt (it pins the sqlglot 30.x the guard was built on).",
        )
    return BigQueryAsWritten


def _to_sql(tree: exp.Expression) -> str:
    """One canonical spelling: sqlglot keeps a function name as it was written (`count`, `Count`), and the fingerprint
    and the executed text should not depend on that. Comments never reach the output."""
    return tree.sql(dialect=_bigquery_dialect(), comments=False, normalize_functions="upper")


_UNRESOLVED = re.compile(r"Column '([^']{1,80})' could not be resolved|Unknown column: (\S{1,80})")
_SAFE_NAME = re.compile(r"[A-Za-z0-9_.-]{1,80}")
_RAW_STRING = re.compile(r"[A-Za-z]{0,2}")
_OCTAL, _HEX = frozenset("01234567"), frozenset("0123456789abcdefABCDEF")
_SIMPLE_ESCAPES = frozenset("abfnrtv\\?\"'`")  # \a \b \f \n \r \t \v \\ \? \" \' \`
_TOKEN = re.compile(r"<Token[^>]*>")
_CLASS = re.compile(r"<class '(?:[\w.]*\.)?(\w+)'>")

# Table arguments that the scope rewrite would silently drop (time travel, sampling, pivots, hints ...).
_PLAIN_TABLE_ARGS = frozenset({"this", "db", "catalog", "alias", "joins"})  # `joins`: FROM (orders o JOIN ...), kept by the rewrite


_PLAIN_INTEGER = re.compile(r"-?\d+")
# Functions whose interval sqlglot keeps as (this, expression, unit) and not as an Interval node.
_DATE_DELTAS = tuple(
    getattr(exp, name) for name in ("DateAdd", "DateSub", "TimestampAdd", "TimestampSub", "DatetimeAdd", "DatetimeSub", "TimeAdd", "TimeSub")
    if hasattr(exp, name)
)


def generate_sql(tree: exp.Expression) -> str:
    """The BigQuery text for a tree, in one canonical spelling. Changes the tree: it is the last thing done to it.

    sqlglot writes `INTERVAL 30 DAY` as `INTERVAL '30' DAY`. BigQuery documents the unquoted integer for a single
    unit, so that is what goes out. Quoted range forms such as `INTERVAL '1-2' YEAR TO MONTH` are left as they are.
    """
    for node in tree.find_all(exp.Interval, *_DATE_DELTAS):
        key = "this" if isinstance(node, exp.Interval) else "expression"
        value, unit = node.args.get(key), node.args.get("unit")
        if isinstance(value, exp.Literal) and value.is_string and isinstance(unit, exp.Var) and _PLAIN_INTEGER.fullmatch(value.name):
            node.set(key, exp.Literal.number(value.name))
    return _to_sql(tree)


def _names_in(node: exp.Expression) -> list[str]:
    """The name parts of a path in front of a call, left to right: `proj`.ds is ['proj', 'ds']."""
    if isinstance(node, exp.Dot):
        return _names_in(node.this) + _names_in(node.expression)
    if isinstance(node, exp.Column):
        return [part.name for part in node.parts]
    if isinstance(node, (exp.Identifier, exp.Var)):
        return [node.name]
    return ["(an expression)"]  # anything else in front of a call is no namespace


def _call_path(call: exp.Func) -> list[str]:
    """Where a call sits, as the parts of its name. `proj.ds.f(1)`, `` `my-project`.ds.f(1) `` and `` `my-project.ds.f`(1) ``
    are all ['proj' or 'my-project', 'ds', 'f']. Built from the tree, because the text of a path with a hyphen or a
    space in it is not something a pattern should have to read."""
    # `.name` is the function's name only for an unknown function (Anonymous). For a known one it is the text of the first
    # argument: Host('https://a.test/x').name is 'https://a.test/x'.
    parts = [call.name if isinstance(call, exp.Anonymous) else call.sql_name()]
    node: exp.Expression = call
    while isinstance(node.parent, exp.Dot) and node.parent.expression is node:
        parts = _names_in(node.parent.this) + parts
        node = node.parent
    return [piece for part in parts for piece in part.split(".") if piece]


def _is_builtin_path(path: list[str]) -> bool:
    """A call may be a bare name, or `NAMESPACE.name` for a namespace BigQuery has (`NET.HOST`), with an optional `SAFE.` in
    front (`SAFE.DIVIDE`, `SAFE.NET.HOST`). `SAFE.dataset.function` is a stored function with a prefix, not a built-in."""
    parts = [part.upper() for part in path]
    if parts and parts[0] == "SAFE":
        parts = parts[1:]
    return len(parts) <= 1 or (len(parts) == 2 and parts[0] in BUILTIN_NAMESPACES - {"SAFE"})


_NAME_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _called_names(text: str) -> set[str]:
    """Every name written directly before a `(` in SQL text, with its dots: `NET.HOST`, `ML.PREDICT`, `COUNT`. One pass
    over the tokens, so the cost does not grow with how deeply the calls are nested."""
    names: set[str] = set()
    chain: list[str] = []
    after_dot = False
    for token in sqlglot.tokenize(text, read="bigquery"):
        kind = token.token_type
        if kind == TokenType.DOT:
            after_dot = bool(chain)
            continue
        if kind == TokenType.L_PAREN:
            if chain and not after_dot:
                names.add(".".join(chain))
            chain, after_dot = [], False
            continue
        if kind == TokenType.IDENTIFIER or _NAME_TOKEN.fullmatch(token.text):
            parts = token.text.split(".") if kind == TokenType.IDENTIFIER else [token.text]
            chain = chain + parts if after_dot else list(parts)
            after_dot = False
            continue
        chain, after_dot = [], False
    return names


def _safe_name(name: str) -> str:
    """A column name for an error detail, which the SQL repair step reads. Only a plain name goes in: anything with
    other characters in it could be a value the user typed (an email, say) that was written as a name."""
    return name if _SAFE_NAME.fullmatch(name) else "(a name with unusual characters)"


def _has_illegal_escape(body: str) -> bool:
    r"""True when a non-raw string body holds a backslash sequence that BigQuery rejects: `\d`, `\.`, `\s`, `\_` ..."""
    i = 0
    while i < len(body):
        if body[i] != "\\":
            i += 1
            continue
        nxt = body[i + 1:i + 2]
        if nxt and nxt in _SIMPLE_ESCAPES:
            i += 2
        elif nxt in ("x", "X") and len(body[i + 2:i + 4]) == 2 and set(body[i + 2:i + 4]) <= _HEX:
            i += 4
        elif nxt == "u" and len(body[i + 2:i + 6]) == 4 and set(body[i + 2:i + 6]) <= _HEX:
            i += 6
        elif nxt == "U" and len(body[i + 2:i + 10]) == 8 and set(body[i + 2:i + 10]) <= _HEX:
            i += 10
        elif len(body[i + 1:i + 4]) == 3 and set(body[i + 1:i + 4]) <= _OCTAL:
            i += 4
        else:
            return True
    return False


def _keeps_every_row(node: exp.Expression) -> bool:
    """True for a plain SELECT or a tree of UNION ALL: the only shapes where one more column per branch changes no row."""
    node = unwrap(node)
    if isinstance(node, exp.Select):
        return not node.args.get("distinct")  # SELECT DISTINCT removes duplicate rows, and another column changes which are duplicates
    if isinstance(node, exp.Union) and not node.args.get("distinct"):
        return _keeps_every_row(node.this) and _keeps_every_row(node.expression)
    return False


def _require_supported_sqlglot() -> None:
    """The guard reads sqlglot's argument names (`from_`, `with_`) and node types. Older versions call the first two
    `from` and `with`, and then `source_names` finds nothing and every demographic result would be withheld, with no
    error. So check once, loudly, when the guard is built."""
    probe = sqlglot.parse_one("WITH a AS (SELECT 1 AS x) SELECT x FROM a", read="bigquery")
    _bigquery_dialect()  # raises ConfigError itself if sqlglot's BigQuery generator is not the one this was written against
    if not {"from_", "with_"} <= set(probe.args) or not hasattr(exp, "TableColumn"):
        raise ConfigError(
            f"This version of sqlglot ({sqlglot.__version__}) is not one the SQL Guard was built for.",
            fix="Run: pip install -r requirements.txt (it asks for sqlglot 30.24 or newer, below 31).",
        )


class SQLGuard:
    def __init__(self, settings: Settings, allowlist: Allowlist) -> None:
        if fold(allowlist.dataset) != fold(settings.bigquery.dataset):
            raise ConfigError(
                f"The allow-list is for dataset {allowlist.dataset}, but bigquery.dataset is {settings.bigquery.dataset}.",
                fix="Make schema_allowlist.yaml and config.yaml name the same dataset.",
            )
        self._allowlist = allowlist
        self._guards = settings.guards
        self._row_limit = settings.bigquery.sql_row_limit
        self._project, _, self._dataset_id = settings.bigquery.dataset.rpartition(".")
        self._views = ScopeViews(allowlist, settings.bigquery.dataset)
        _require_supported_sqlglot()
        # The built-in list is a floor: config can add names, never remove one.
        self._denied = tuple(name.upper() for name in (*ALWAYS_DENIED_FUNCTIONS, *settings.guards.denied_functions))
        # The schema qualify sees: allowed columns only. A blocked or unlisted column is simply unknown to it. It is
        # keyed by dataset too, because the analysis copy keeps the dataset on every table (see `_analyze`).
        self._schema = {
            self._dataset_id: {t: {c: "UNKNOWN" for c in allowlist.allowed(t)} for t in allowlist.table_names()}
        }

    # entry point -----------------------------------------------------------------------------------

    def check(self, sql: str, user: UserContext) -> GuardedQuery:
        with span("guard.sql", "guard", scope_mode=user.scope_mode) as sp:
            try:
                gq = self._check(sql, user)
            except AgentError as err:
                self._record_refusal(err)
                raise
            except Exception as exc:  # noqa: BLE001 - a guard that crashes blocks the query: it never lets one through
                err = AgentError(ErrorKind.INTERNAL, detail=crash_summary(exc))
                self._record_refusal(err, rule="GUARD_ERROR")
                raise err from None
            sp.set(
                fingerprint=gq.fingerprint, tables=sorted(gq.tables), limit=gq.limit, rewrites=list(gq.rewrites),
                per_customer=gq.per_customer, demographic=gq.demographic,
            )
            guard_decision("sql", "rewrite" if gq.rewrites else "pass", rule=gq.rewrites[0] if gq.rewrites else None,
                           rewrites=list(gq.rewrites), fingerprint=gq.fingerprint)
            return gq

    @staticmethod
    def _record_refusal(err: AgentError, rule: str | None = None) -> None:
        # The model's own mistakes (SQL_SYNTAX, SQL_UNKNOWN_COLUMN) are not guard decisions: the loop repairs them.
        if err.kind in (ErrorKind.GUARD_VIOLATION, ErrorKind.NO_ACCESS, ErrorKind.INTERNAL):
            guard_decision("sql", "block", rule=rule or err.rule_id or err.kind.value, error_kind=err.kind.value)

    def _check(self, sql: str, user: UserContext) -> GuardedQuery:
        require_access(user)  # step 0: an empty brand list is no access, never "all brands" (S14)
        mode: ViewMode = "all" if user.scope_mode == "all" else "brands"

        root, fingerprint = self._parse(sql)  # steps 1 and 2
        self._check_functions(root)  # step 3
        refs = self._check_tables(root)  # step 4
        self._check_blocked_names(root)  # step 5
        analysis = self._analyze(root)  # step 6
        per_customer, demographic = self._apply_rules(analysis)  # step 7

        rewrites: list[str] = []
        done = self._views.rewrite(refs, mode)  # step 8
        self._views.verify(root, done)
        tables = done.tables
        if tables:
            rewrites.append("SCOPE_BRANDS" if mode == "brands" else "SCOPE_ALL")

        k_column = None  # step 9
        if demographic:
            k_column = self._inject_customer_count(root, analysis)
            if k_column:
                rewrites.append("K_USERS_ADDED")
        limit, limit_rule = self._apply_limit(root, per_customer)
        if limit_rule:
            rewrites.append(limit_rule)
        id_columns = self._name_id_outputs(root, analysis) if per_customer else ()

        try:
            text = generate_sql(root)  # step 10
        except Exception:  # noqa: BLE001 - sqlglot parses some malformed expressions and then cannot write them
            raise AgentError(ErrorKind.SQL_SYNTAX, detail="the query could not be written back as SQL: an expression in it is malformed") from None
        self._verify_output(text, mode, sum(self._views.reference_count(t, mode) for t in tables))
        params = (ArrayParam(BRANDS_PARAM, "STRING", tuple(user.brands)),) if mode == "brands" and tables else ()
        return GuardedQuery.issue(
            sql=text,
            params=params,
            tables=frozenset(tables),
            id_columns=id_columns,
            per_customer=per_customer,
            demographic=demographic,
            k_column=k_column,
            limit=limit,
            rewrites=tuple(rewrites),
            fingerprint=fingerprint,
            scope_label="all brands" if mode == "all" else "your brands: " + ", ".join(user.brands),
        )

    # steps 1 and 2: parse, one SELECT -------------------------------------------------------------------

    def _parse(self, sql: str) -> tuple[exp.Expression, str]:
        if not isinstance(sql, str) or not sql.strip():
            raise AgentError(ErrorKind.SQL_SYNTAX, detail="the query is empty")
        try:
            statements = sqlglot.parse(sql, read="bigquery")
        except ParseError as exc:
            raise AgentError(ErrorKind.SQL_SYNTAX, detail=self._parse_detail(exc)) from None
        except TokenError:
            raise AgentError(ErrorKind.SQL_SYNTAX, detail="the query has an unclosed quote or a character BigQuery does not accept") from None
        except Exception:  # noqa: BLE001 - for example RecursionError on absurdly nested SQL
            raise AgentError(ErrorKind.SQL_SYNTAX, detail="the query could not be read") from None

        statements = [s for s in statements if s is not None and not isinstance(s, exp.Semicolon)]
        if not statements:
            raise AgentError(ErrorKind.SQL_SYNTAX, detail="the query holds no statement")
        if len(statements) > 1:
            raise violation("MULTI_STATEMENT", f"{len(statements)} statements")
        root = statements[0]
        if not isinstance(unwrap(root), (exp.Select, exp.SetOperation)):
            raise violation("NOT_SELECT", f"the statement is a {type(root).__name__}")
        # sqlglot copies `/* sqlglot.meta key=value */` comments into node metadata, and some of that metadata changes
        # what the generator writes (the spelling of a function name). Text from the model decides none of it.
        for node in root.walk():
            node.meta.clear()
        if any(with_.args.get("recursive") for with_ in root.find_all(exp.With)):
            # The lineage cannot follow a query that reads its own output, and analytics do not need one.
            raise AgentError(ErrorKind.SQL_SYNTAX, detail="WITH RECURSIVE is not supported: write the query without recursion")
        self._check_string_escapes(sql)
        # The original tree, comments removed and keywords in one case: the same query in other spacing hashes alike.
        try:
            text = _to_sql(root)
        except Exception:  # noqa: BLE001 - for example `FORMAT_DATE(, x)`: it parses, and then it cannot be written
            raise AgentError(ErrorKind.SQL_SYNTAX, detail="the query has a malformed expression") from None
        return root, hashlib.sha256(text.encode("utf-8")).hexdigest()

    @staticmethod
    def _check_string_escapes(sql: str) -> None:
        """BigQuery refuses a string such as '\\d+' ("illegal escape sequence") and the SQL loop would fix it with a raw
        string. sqlglot reads it as 'd+' and writes that back, so the query would run and mean something else. So the
        escapes are checked here, on the text the model wrote. Raw strings (r'...') take backslashes as they are."""
        for token in sqlglot.tokenize(sql, read="bigquery"):
            kind = token.token_type.name
            if "STRING" not in kind or "RAW" in kind:
                continue
            text = sql[token.start:token.end + 1]
            prefix = _RAW_STRING.match(text).group(0)
            if "r" in prefix.lower():
                continue
            rest = text[len(prefix):]
            quote = rest[:3] if rest[:3] in ("'''", '"""') else rest[:1]
            if quote and _has_illegal_escape(rest[len(quote):-len(quote)]):
                raise AgentError(
                    ErrorKind.SQL_SYNTAX,
                    detail=f"a string literal (line {token.line}) has a backslash escape BigQuery rejects: double the backslash or use a raw string r'...'",
                )

    @staticmethod
    def _parse_detail(exc: ParseError) -> str:
        """sqlglot's description and position, without the token text or the SQL around it: either can be a literal."""
        first = (exc.errors or [{}])[0]
        description = _CLASS.sub(r"\1", _TOKEN.sub("<token>", str(first.get("description") or "syntax error")))
        position = f" (line {first['line']}, column {first['col']})" if first.get("line") else ""
        return normalize_message(description) + position

    # step 3: functions, table functions, system variables ----------------------------------------------

    def _check_functions(self, root: exp.Expression) -> None:
        parameters = list(root.find_all(exp.Parameter, exp.Placeholder, exp.SessionParameter))
        if any(p.sql(dialect="bigquery").startswith("@@") for p in parameters):
            raise violation("DISALLOWED_FUNCTION", "a system variable (@@name)")
        if parameters:  # the values travel as our own parameters; the model writes values into the query
            raise AgentError(ErrorKind.SQL_SYNTAX, detail="query parameters (@name or ?) are not supported: write the value in the query")

        for table in root.find_all(exp.Table):
            if isinstance(table.this, exp.Func):
                raise violation("DISALLOWED_FUNCTION", "a table function (a function used as a table)")
            if not isinstance(table.this, exp.Identifier):
                raise violation("TABLE_NOT_ALLOWED", "a table path with too many parts")

        for call in root.find_all(exp.Func):
            path = _call_path(call)  # from the tree: the generated text cannot show a project called `my-project`
            self._check_called(".".join(path), path, call.sql_name())

    def _check_called(self, dotted: str, path: list[str], *also: str) -> None:
        """One call, by every name it is known by. The deny list is matched on each, and a dotted name must be a built-in one."""
        for name in (dotted, *also):
            if name and self._is_denied(name.upper()):
                raise violation("DISALLOWED_FUNCTION", f"function {_safe_name(name.upper())} is on the deny list")
        if not _is_builtin_path(path):
            raise violation("DISALLOWED_FUNCTION", "a function stored in a dataset (user-defined)")

    def _is_denied(self, head: str) -> bool:
        return any(head.startswith(name[:-1]) if name.endswith(".*") else head == name for name in self._denied)

    # step 4: tables --------------------------------------------------------------------------------

    def _check_tables(self, root: exp.Expression) -> list[exp.Table]:
        refs = base_table_refs(root)  # sqlglot scopes: a CTE called `orders` is not a base table
        # No detail below repeats the table path: it is the model's text, and a value the user typed can end up in a
        # table position. The rule ID says what was wrong.
        for ref in refs:
            name, db, catalog = ref.name, ref.db, ref.catalog
            if "*" in name or "?" in name:
                raise violation("WILDCARD_TABLE", "a wildcard table")
            if any(value for key, value in ref.args.items() if key not in _PLAIN_TABLE_ARGS):
                raise violation("TABLE_NOT_ALLOWED", "a table option that is not supported (time travel, sample, pivot)")
            alias = ref.args.get("alias")
            if isinstance(alias, exp.TableAlias) and alias.args.get("columns"):
                raise violation("TABLE_NOT_ALLOWED", "a column list on a table alias")
            if db and fold(db) != fold(self._dataset_id):
                raise violation("TABLE_NOT_ALLOWED", "a table in another dataset")
            if catalog and fold(catalog) != fold(self._project):
                raise violation("TABLE_NOT_ALLOWED", "a table in another project")
            if self._allowlist.table_name(name) is None:
                raise violation("TABLE_NOT_ALLOWED", "a table that is not one of the approved four")
        return refs

    # step 5: blocked names ---------------------------------------------------------------------------

    def _check_blocked_names(self, root: exp.Expression) -> None:
        """Any identifier in any clause with a blocked name: a column, an alias made from one, a field, in any case and
        quoted or not. A harmless alias that happens to be called `email` is refused too: that fails closed."""
        blocked = self._allowlist.blocked_names
        for node in root.find_all(exp.Identifier, exp.Var):
            for part in fold(node.name).split("."):  # `users.email` in one pair of backticks is two names
                if part in blocked:
                    raise violation("PII_COLUMN", f"a blocked column name ({part}) is used")

    # step 6: lineage on a copy ---------------------------------------------------------------------------

    def _analyze(self, root: exp.Expression) -> Analysis:
        """Qualify a copy, so that every column names its source, then follow customer IDs and quasi-identifiers.
        The copy is thrown away: the query that runs is the original, rewritten."""
        copy = root.copy()
        copy_refs = base_table_refs(copy)
        strip_table_paths(copy_refs)  # the same normalisation the rewrite applies to the query that runs
        for ref in copy_refs:
            name = self._allowlist.table_name(ref.name)
            if name is None:
                raise violation("TABLE_NOT_ALLOWED", "a table was not approved")
            # Step 4 approved the path. The copy spells it `dataset.table`, never a bare name: a table with a dataset can
            # not be a CTE, but a bare `users` would be read as the model's own CTE called `users` when there is one,
            # and the lineage would follow the CTE's columns instead of the table's.
            ref.set("this", exp.to_identifier(name))
            ref.set("db", exp.to_identifier(self._dataset_id))
            ref.set("catalog", None)
        try:
            qualified = qualify(
                copy, dialect="bigquery", schema=self._schema, validate_qualify_columns=True,
                quote_identifiers=False, identify=False,
            )
            return analyze(qualified, self._allowlist)
        except Exception as exc:  # noqa: BLE001 - no lineage, no execution: it is the model's to repair
            raise self._analysis_error(exc) from None

    @staticmethod
    def _analysis_error(exc: Exception) -> AgentError:
        if isinstance(exc, LineageTooComplex):
            return AgentError(ErrorKind.SQL_SYNTAX, detail="the query is too deeply nested to analyse: write it with fewer layers")
        if isinstance(exc, OptimizeError):
            match = _UNRESOLVED.search(str(exc))
            if match:
                name = _safe_name(match.group(1) or match.group(2))
                return AgentError(
                    ErrorKind.SQL_UNKNOWN_COLUMN,
                    detail=f"column {name} was not found, or it exists in more than one table: use the table alias",
                )
        return AgentError(ErrorKind.SQL_SYNTAX, detail=f"the query could not be analysed ({type(exc).__name__})")

    # step 7: customer rows and demographics (S7) ----------------------------------------------------------

    def _apply_rules(self, found: Analysis) -> tuple[bool, bool]:
        """Returns (per_customer, demographic)."""
        if found.id_outputs:
            if not self._guards.allow_ranked_customer_lists:  # every ID output; and the usual text would offer a ranked list
                raise violation("PER_CUSTOMER_ROWS", "ranked customer lists are switched off", text=NO_RANKED_LISTS_TEXT)
            if not found.ranked_list:  # R1
                raise violation("PER_CUSTOMER_ROWS", "a customer ID is returned outside a ranked spend list")
            if found.demographic:  # R3
                raise violation("PER_CUSTOMER_ROWS", "customer rows next to a demographic column")
            return True, False  # R2
        if found.demographic:
            if not found.aggregated:  # R4
                if found.grouped_below and not found.collects:
                    # Not row-level data: the grouping happens inside a CTE or a subquery, and a plain SELECT presents it. The
                    # Result Guard needs the group size next to the groups, which one SELECT can carry. So the model is told
                    # how to write it (repairable), not refused (a violation is never retried).
                    raise AgentError(
                        ErrorKind.SQL_SYNTAX,
                        detail="customer groups must be grouped and returned by the same SELECT: put the GROUP BY in the outermost "
                               "SELECT and use window functions (for example SUM(x) OVER ()) instead of a CTE or subquery around it",
                    )
                raise violation("DEMOGRAPHIC_ROW_LEVEL", "a demographic column without aggregation")
            return False, True  # R5
        return False, False

    # step 9: the customer count for the Result Guard (R5), LIMIT ---------------------------------------

    def _inject_customer_count(self, root: exp.Expression, found: Analysis) -> str | None:
        """Add the number of distinct customers to every outermost SELECT, so the Result Guard can hide small groups: a
        `COUNT(DISTINCT id)` for one source that holds customers, the `LEAST` of several for more (see `_k_sources`).
        Returns its alias, or None when the customers cannot be counted (the Result Guard then withholds the result:
        group sizes cannot be verified)."""
        branches = outer_selects(root)
        if not branches or len(branches) != len(found.k_sources) or not _keeps_every_row(root):
            return None  # (EXCEPT, INTERSECT, UNION DISTINCT and SELECT DISTINCT compare whole rows: another column would change the answer)
        plans: list[list[tuple[str, str]]] = []
        for branch, sources in zip(branches, found.k_sources):
            if sources is None or branch.args.get("kind"):
                return None  # no ID column in reach, or `SELECT AS STRUCT/VALUE`, which cannot take another column
            names = source_names(branch)
            if any(demographic and fold(alias) not in names for alias, _, demographic in sources):
                return None  # a source with demographic data that the rewritten query cannot name, so cannot be counted
            usable = [(alias, column) for alias, column, _ in sources if fold(alias) in names]
            if not usable:
                return None
            plans.append(usable)
        taken = {fold(i.name) for i in root.find_all(exp.Identifier)}
        alias = K_ALIAS
        while fold(alias) in taken:
            alias += "_"
        for branch, usable in zip(branches, plans):
            counts = [exp.Count(this=exp.Distinct(expressions=[exp.column(column, table=source)])) for source, column in usable]
            size = counts[0] if len(counts) == 1 else exp.Least(this=counts[0], expressions=counts[1:])
            branch.append("expressions", exp.Alias(this=size, alias=exp.to_identifier(alias)))
        return alias

    @staticmethod
    def _name_id_outputs(root: exp.Expression, found: Analysis) -> tuple[str, ...]:
        """The real names of the output columns that hold customer IDs, for the Result Guard to replace with fake IDs.

        BigQuery calls an unaliased expression `f0_`, `f1_` ..., which the Result Guard could not tell from any other
        column, so such an output gets the alias `customer_id`. A bare column keeps its own name."""
        (branch,) = outer_selects(root)  # a ranked list is one SELECT (rule R2)
        projections = list(branch.expressions)
        if any(isinstance(p, exp.Star) or (isinstance(p, exp.Column) and isinstance(p.this, exp.Star)) for p in projections):
            raise violation("PER_CUSTOMER_ROWS", "a star in a ranked customer list")  # positions would not line up
        taken = {fold(i.name) for i in root.find_all(exp.Identifier)}
        names = []
        for position in found.id_positions:
            projection = projections[position]
            if isinstance(projection, exp.Alias):
                names.append(projection.alias)
            elif isinstance(projection, exp.Column):
                names.append(projection.name)
            else:
                alias = "customer_id"
                while fold(alias) in taken:
                    alias += "_"
                taken.add(fold(alias))
                projection.replace(exp.Alias(this=projection.copy(), alias=exp.to_identifier(alias)))
                names.append(alias)
        return tuple(names)

    def _apply_limit(self, root: exp.Expression, per_customer: bool) -> tuple[int, str | None]:
        """The LIMIT of the whole query (the union, for a set operation): added when missing, clamped when too big. A
        ranked customer list is clamped to the rows the model sees. Inner LIMITs stay as written."""
        cap = min(self._row_limit, self._guards.rows_to_model) if per_customer else self._row_limit
        current = None
        node = root.args.get("limit")
        if isinstance(node, exp.Limit) and isinstance(node.expression, exp.Literal) and not node.expression.is_string:
            current = int(node.expression.name) if node.expression.name.isdigit() else None
        if current is not None and current <= cap:
            return current, None
        root.set("limit", exp.Limit(expression=exp.Literal.number(cap)))
        return cap, "LIMIT_CLAMPED" if current is not None else "LIMIT_ADDED"

    # step 10: read the generated text back ------------------------------------------------------------

    def _verify_output(self, text: str, mode: ViewMode, expected_refs: int) -> None:
        """Parse the SQL that will run, and check it as if it had come from the model: one SELECT, no blocked name,
        every table reference spelled out in full and inside the views, the brand parameter present exactly when
        needed. A second look at the generator's output, by a different path than the rewrite."""
        try:
            parsed = [s for s in sqlglot.parse(text, read="bigquery") if s is not None]
        except Exception:  # noqa: BLE001
            raise AgentError(ErrorKind.SQL_SYNTAX, detail="the rewritten query could not be read back: an expression in it is malformed") from None
        if len(parsed) != 1 or not isinstance(unwrap(parsed[0]), (exp.Select, exp.SetOperation)):
            raise violation("SCOPE_REWRITE_INCOMPLETE", "the rewritten query is not a single SELECT")
        tree = parsed[0]
        self._check_blocked_names(tree)
        for name in _called_names(text):  # what the generator calls each function, which can differ from the tree's name
            self._check_called(name, name.split("."))
        refs = base_table_refs(tree)
        if len(refs) != expected_refs:
            raise violation("SCOPE_REWRITE_INCOMPLETE", f"expected {expected_refs} table reference(s), found {len(refs)}")
        for ref in refs:
            in_full = fold(ref.catalog) == fold(self._project) and fold(ref.db) == fold(self._dataset_id)
            if not in_full or self._allowlist.table_name(ref.name) is None:
                raise violation("SCOPE_REWRITE_INCOMPLETE", "a table reference is not spelled out in full")
        used = {p.name for p in tree.find_all(exp.Parameter)}
        if used != ({BRANDS_PARAM} if mode == "brands" and expected_refs else set()):
            raise violation("SCOPE_REWRITE_INCOMPLETE", "the brand parameter does not match the user's scope")
