"""Phase 1 search: score every legal champion in the requested role independently
(no lookahead) and return the top N by total score. 2-ply lookahead is Phase 2.

`role` must always be supplied by the caller, for both picks and bans -- roles are
assigned before champion select (per the SOLOQ rules), so during the pick phase this
is simply "which role am I drafting for." During the ban phase there are no picks yet
in SOLOQ, so matchup/synergy/exposure terms are moot and this degenerates to "which
champions in this role currently have the strongest base rate" -- a reasonable proxy
for "worth denying," though true ban-specific reasoning is `ban_suggest` (see below).
"""

from __future__ import annotations

from draftiq.draft.state import DraftStateMachine
from draftiq.models import RankBracket, Recommendation, Role
from draftiq.providers.base import StatsProvider
from draftiq.stats.composition import load_hand_curated_features
from draftiq.stats.scoring import score_candidate
from draftiq.stats.shrinkage import DEFAULT_K, DEFAULT_K_MATCHUP, compute_role_average


def suggest(
    sm: DraftStateMachine,
    provider: StatsProvider,
    role: Role,
    top_n: int = 5,
    k: float = DEFAULT_K,
    k_m: float = DEFAULT_K_MATCHUP,
) -> list[Recommendation]:
    if sm.is_complete():
        raise ValueError("Cannot suggest a pick or ban: the draft is already complete.")

    champions = provider.get_champions()
    champion_by_id = {c.champion_id: c for c in champions}
    rank: RankBracket = sm.state.rank

    side = sm.current_side()
    ally_ids = sm.picked_champion_ids(side)
    enemy_ids = sm.picked_champion_ids(side.other())
    legal_ids = sm.legal_champion_ids(champion_by_id.keys())
    remaining_enemy_picks = sm.remaining_picks(side.other())

    # Not part of the StatsProvider protocol -- providers with no meaningful
    # per-call cost (e.g. ManualCSVProvider, a local dict lookup) simply don't
    # implement it. OpggProvider does: a cold call here is otherwise one
    # sequential HTTP round-trip per champion against a live API, which for a
    # full ~170-champion roster is unacceptably slow for a live draft.
    # `include_matchups` is unconditional now (not just when `enemy_ids` is
    # non-empty): counterpick exposure needs every legal candidate's matchup data
    # even before anyone on the enemy side has picked yet.
    if hasattr(provider, "prefetch_for_suggest"):
        provider.prefetch_for_suggest(champion_by_id.keys(), role, rank)
        provider.prefetch_for_suggest(
            legal_ids,
            role,
            rank,
            include_matchups=True,
            include_synergies=bool(ally_ids),
        )

    stats_by_champion = {
        c.champion_id: provider.get_champion_stats(c.champion_id, role, rank) for c in champions
    }
    p0 = compute_role_average(stats_by_champion.values())
    composition_table = load_hand_curated_features()

    recommendations = [
        score_candidate(
            champion=champion_by_id[champ_id],
            role=role,
            rank=rank,
            provider=provider,
            p0=p0,
            ally_ids=ally_ids,
            enemy_ids=enemy_ids,
            champion_by_id=champion_by_id,
            k=k,
            k_m=k_m,
            composition_table=composition_table,
            remaining_enemy_ids=legal_ids,
            remaining_enemy_picks=remaining_enemy_picks,
        )
        for champ_id in legal_ids
    ]
    recommendations.sort(key=lambda r: r.total_score, reverse=True)
    return recommendations[:top_n]
