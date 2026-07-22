"""Shared `suggest` dispatch/validation: which of `greedy.suggest`,
`lookahead.suggest_with_lookahead`, `ban.suggest_bans`, or `priority.suggest_priority`
a given (action type, role, lookahead, any_role) combination should call, and which
combinations are invalid. Used by both the CLI (`cli.py`) and the web API
(`web/app.py`) so the two front ends can never disagree about validation -- extracted
from what was originally just `cli.suggest`'s if/elif chain.
"""

from __future__ import annotations

from draftiq.draft.state import DraftStateMachine
from draftiq.models import ActionType, Recommendation, Role
from draftiq.providers.base import StatsProvider
from draftiq.search.ban import suggest_bans
from draftiq.search.greedy import suggest as greedy_suggest
from draftiq.search.lookahead import suggest_with_lookahead
from draftiq.search.priority import suggest_priority


class SuggestRequestError(ValueError):
    """An invalid role/lookahead/any_role combination for the draft's current phase."""


def resolve_suggestion(
    sm: DraftStateMachine,
    provider: StatsProvider,
    role: Role | None,
    top_n: int,
    lookahead: bool,
    any_role: bool,
) -> tuple[list[Recommendation], bool]:
    """Returns `(recommendations, show_role_column)`. Assumes the caller has already
    checked `sm.is_complete()` -- each front end renders that case differently.
    `suggest_bans`/`suggest_priority` can still raise plain `ValueError` on their own
    (e.g. "already complete", "already filled every role") -- callers should catch
    `ValueError` broadly, not just `SuggestRequestError`."""
    action = sm.current_action_type()

    if action is ActionType.BAN:
        if any_role:
            raise SuggestRequestError("--any-role only applies to picks; bans aren't role-locked.")
        return suggest_bans(sm, provider, top_n=top_n), False

    if any_role:
        if lookahead:
            raise SuggestRequestError("--any-role and --lookahead can't be combined yet.")
        return suggest_priority(sm, provider, top_n=top_n), True

    if role is None:
        raise SuggestRequestError("--role is required when suggesting a pick.")
    if lookahead:
        return suggest_with_lookahead(sm, provider, role, top_n=top_n), False
    return greedy_suggest(sm, provider, role, top_n=top_n), False
