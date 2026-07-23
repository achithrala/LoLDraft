"""Shared `suggest` dispatch/validation: which of `greedy.suggest`,
`lookahead.suggest_with_lookahead`, `ban.suggest_bans`, or `priority.suggest_priority`
a given (action type, role, lookahead, any_role, pool) combination should call, and
which combinations are invalid. Used by both the CLI (`cli.py`) and the web API
(`web/app.py`) so the two front ends can never disagree about validation -- extracted
from what was originally just `cli.suggest`'s if/elif chain.

This is also the one place that resolves `pool: bool` into actual champion-id sets --
every `search/*` function takes an already-resolved `pool_ids`/`pool_ids_by_role`
(see their docstrings), not raw player names, so this function is the only thing that
needs to know about `persistence.load_pool_registry()` and `sm.state.roster`.
Deliberately asymmetric by action type, per the confirmed design: for a **pick** (or
`--any-role`), `pool` restricts candidates to the union of `sm.state.roster.ally`'s
pools (there's no way to know in advance which teammate ends up in which slot, so the
union across the whole team stands in for "champions my team plays"). For a **ban**,
`pool` instead adds a bonus/highlight for candidates in the union of
`sm.state.roster.enemy`'s pools -- denying something a specific enemy player actually
plays is worth more than denying something merely popular, but the ban list must
never be *narrowed* to just their known pool (there could always be a good ban
outside it) -- see `search/ban.py`'s docstring.
"""

from __future__ import annotations

from draftiq import persistence
from draftiq.draft.state import DraftStateMachine
from draftiq.models import ActionType, Recommendation, Role, consolidated_pool_ids
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
    pool: bool = False,
) -> tuple[list[Recommendation], bool]:
    """Returns `(recommendations, show_role_column)`. Assumes the caller has already
    checked `sm.is_complete()` -- each front end renders that case differently.
    `suggest_bans`/`suggest_priority` can still raise plain `ValueError` on their own
    (e.g. "already complete", "already filled every role") -- callers should catch
    `ValueError` broadly, not just `SuggestRequestError`."""
    action = sm.current_action_type()
    side = sm.current_side()

    if action is ActionType.BAN:
        if any_role:
            raise SuggestRequestError("--any-role only applies to picks; bans aren't role-locked.")
        pool_ids_by_role = None
        if pool:
            champions = provider.get_champions()
            registry = persistence.load_pool_registry()
            enemy_unfilled = [r for r in Role if r not in sm.filled_roles(side.other())]
            pool_ids_by_role = {
                r: consolidated_pool_ids(registry, sm.state.roster.enemy, r, champions)
                for r in enemy_unfilled
            }
        return suggest_bans(sm, provider, top_n=top_n, pool_ids_by_role=pool_ids_by_role), False

    if any_role:
        if lookahead:
            raise SuggestRequestError("--any-role and --lookahead can't be combined yet.")
        pool_ids_by_role = None
        if pool:
            champions = provider.get_champions()
            registry = persistence.load_pool_registry()
            ally_unfilled = [r for r in Role if r not in sm.filled_roles(side)]
            pool_ids_by_role = {
                r: consolidated_pool_ids(registry, sm.state.roster.ally, r, champions)
                for r in ally_unfilled
            }
        return suggest_priority(sm, provider, top_n=top_n, pool_ids_by_role=pool_ids_by_role), True

    if role is None:
        raise SuggestRequestError("--role is required when suggesting a pick.")

    pool_ids = None
    if pool:
        registry = persistence.load_pool_registry()
        pool_ids = consolidated_pool_ids(
            registry, sm.state.roster.ally, role, provider.get_champions()
        )

    if lookahead:
        return suggest_with_lookahead(sm, provider, role, top_n=top_n, pool_ids=pool_ids), False
    return greedy_suggest(sm, provider, role, top_n=top_n, pool_ids=pool_ids), False
