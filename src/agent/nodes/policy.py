"""`policy_context` (plan 07, S14, NFR15): who is asking and what may they see, read again at the start of every turn.

`users.yaml` is read each turn, so a change of brands applies to the next question. If the list cannot be read, the turn is
blocked (`ACCESS_STORE_DOWN`): a list we cannot read is not a list we can trust. A user with no brands is blocked
(`NO_ACCESS`): an empty list never means "all brands". The `UserContext` is kept in the turn's runtime, not in state.

A run that resumes after a confirmation starts at the HITL Gate, after this node. `current_user` makes the same check for it,
so the confirmation of a delete is also refused if the user's access was taken away in between.
"""
from __future__ import annotations

from src.access.users import UserContext, require_access
from src.agent.boundary import node
from src.agent.deps import Deps
from src.agent.state import TurnState
from src.errors import AgentError, ConfigError, ErrorKind, crash_summary
from src.guards.query import scope_label_for


def scope_label(user: UserContext) -> str:
    """The same words the SQL Guard puts on a query (`GuardedQuery.scope_label`): what the answer says about its scope."""
    return scope_label_for(user.scope_mode, user.brands)


def _checked_user(deps: Deps) -> UserContext:
    try:
        user = deps.load_user()
    except AgentError:
        raise  # ACCESS_STORE_DOWN
    except ConfigError:
        # The file was read, and the user is no longer in it (or the entry is broken): no access, fail closed.
        raise AgentError(ErrorKind.NO_ACCESS, detail="the user is not in the access list") from None
    except Exception as exc:  # noqa: BLE001 - whatever stops the check blocks the turn
        raise AgentError(ErrorKind.ACCESS_STORE_DOWN, detail=crash_summary(exc)) from None
    require_access(user)  # S14: no brands means no access, never "all brands"
    return user


def current_user(deps: Deps, state: TurnState) -> UserContext:
    """The user as this turn's access check read them. A resumed run did not pass `policy_context`, so it checks here."""
    runtime = deps.runtime(state)
    if runtime.user is None:
        runtime.user = _checked_user(deps)
    return runtime.user


def make_policy_context(deps: Deps):
    @node(deps, "policy_context")
    def policy_context(state: TurnState) -> dict:
        user = _checked_user(deps)
        deps.runtime(state).user = user
        # A conversation resumed in a new process has shown labels this process never issued. Their numbers are no customer's ID
        # either, so they are taken as handed out before any query can name one (plan 08, checkpoints on disk).
        deps.result_guard.retire_session_labels(state.get("shown_labels", []))
        return {"user_hash": user.user_hash, "scope_label": scope_label(user)}

    return policy_context
