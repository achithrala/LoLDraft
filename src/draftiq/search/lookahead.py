"""2-ply lookahead: for each of the top candidates from the 1-ply greedy pass,
simulate picking it and estimate the opponent's best immediate response, then
penalize candidates that would hand the opponent a strong follow-up.

This is a different question from counterpick exposure (`stats/exposure.py`):
exposure asks "how badly could *this pick* get countered later," using a
probability-weighted worst case over the whole remaining pool. Lookahead asks "does
picking this specific champion make the *opponent's next pick* better than it would
otherwise be" -- a genuine (if shallow) adversarial simulation rather than a
closed-form estimate.

A real wrinkle: SOLOQ doesn't pre-assign roles to pick slots (role is only known at
`draftiq pick --role` time), so there's no single deterministic "the opponent's next
pick is their jungler." Ply 2 instead checks the opponent's best response across
*each* of their still-unfilled roles and uses the strongest one -- a reasonable
proxy for "what would a rational opponent value most right now," not a guarantee of
what they'll actually pick.

Deliberately not wired into the CLI's default `suggest` path: it's `lookahead_width`
nested `greedy.suggest()` calls (one full scoring pass per opponent role checked),
which is real added latency, especially against a network-bound provider. It's
opt-in via `draftiq suggest --lookahead`.

`pool_ids`, if given, is passed only into ply 1's `greedy.suggest(...)` call --
`_best_opponent_response_score`'s ply-2 calls deliberately take no `pool_ids`
parameter at all, since that's the opponent's best reply, and the opponent doesn't
share your team's champion pool. Keep it that way; don't thread `pool_ids` through
"for consistency" -- that would silently restrict the opponent simulation to your
own pool, which is nonsensical.
"""

from __future__ import annotations

from draftiq.draft.state import DraftStateMachine
from draftiq.models import Recommendation, Role, TermContribution
from draftiq.providers.base import StatsProvider
from draftiq.search import greedy
from draftiq.stats.shrinkage import DEFAULT_K, DEFAULT_K_MATCHUP

DEFAULT_LOOKAHEAD_WIDTH = 8
DEFAULT_LOOKAHEAD_WEIGHT = 0.15


def _best_opponent_response_score(
    simulated_sm: DraftStateMachine,
    provider: StatsProvider,
    k: float,
    k_m: float,
) -> float:
    """The opponent's strongest available follow-up, checked across each of their
    still-unfilled roles. Returns 0.0 if the draft is complete or nothing is legal
    (e.g. every role already filled)."""
    if simulated_sm.is_complete():
        return 0.0
    next_side = simulated_sm.current_side()
    unfilled_roles = [r for r in Role if r not in simulated_sm.filled_roles(next_side)]

    best_score = 0.0
    for candidate_role in unfilled_roles:
        responses = greedy.suggest(simulated_sm, provider, candidate_role, top_n=1, k=k, k_m=k_m)
        if responses:
            best_score = max(best_score, responses[0].total_score)
    return best_score


def suggest_with_lookahead(
    sm: DraftStateMachine,
    provider: StatsProvider,
    role: Role,
    top_n: int = 5,
    lookahead_width: int = DEFAULT_LOOKAHEAD_WIDTH,
    lookahead_weight: float = DEFAULT_LOOKAHEAD_WEIGHT,
    k: float = DEFAULT_K,
    k_m: float = DEFAULT_K_MATCHUP,
    pool_ids: set[int] | None = None,
) -> list[Recommendation]:
    """Ply 1: the normal greedy ranking, widened to `lookahead_width` candidates.
    Ply 2: for each of those, simulate the pick and subtract
    `lookahead_weight * opponent's best response score` from its total. Re-sorts
    and returns the top `top_n`.
    """
    ply1 = greedy.suggest(
        sm, provider, role, top_n=lookahead_width, k=k, k_m=k_m, pool_ids=pool_ids
    )

    adjusted: list[Recommendation] = []
    for rec in ply1:
        simulated_state = sm.state.model_copy(deep=True)
        simulated_sm = DraftStateMachine(simulated_state)
        simulated_sm.apply_pick(rec.champion_id, role)

        response_score = _best_opponent_response_score(simulated_sm, provider, k, k_m)
        if response_score <= 0.0:
            adjusted.append(rec)
            continue

        penalty = lookahead_weight * response_score
        new_terms = [*rec.terms, TermContribution(label="opponent best reply", value=-penalty)]
        adjusted.append(
            rec.model_copy(update={"total_score": rec.total_score - penalty, "terms": new_terms})
        )

    adjusted.sort(key=lambda r: r.total_score, reverse=True)
    return adjusted[:top_n]
