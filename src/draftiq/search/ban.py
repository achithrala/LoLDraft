"""Ban recommendations: rank legal champions by how much banning them denies the
opponent, not by how good they'd be for the banning side -- which is what
`greedy.suggest` would tell you if used during the ban phase (its documented
"reasonable proxy, not true ban-specific reasoning" limitation).

Bans aren't role-locked (anyone can ban anything), so each candidate is checked
across every one of the opponent's still-unfilled roles and its best showing is
used, weighted by how likely it is to actually be picked -- "nobody bans a 0.3%
pick rate champion," same philosophy as counterpick exposure.

The key trick: `score_candidate` scored with the sides swapped -- the *opponent's*
existing picks as allies (their synergy) and *our* existing picks as the matchup
side (the threat this champion would pose to us) -- is exactly "how good would this
be for them," which is the natural definition of ban value before pick-rate
weighting. No new scoring formula needed, just a different pair of inputs to the
existing one.

Deliberately does not compute counterpick exposure for the hypothetical enemy pick
-- that's a second-order "if they picked this, who might counter *them* later"
question, adding real cost for a speculative ban-time analysis. Composition fit is
still included: does this champion complete a strong comp for them.
"""

from __future__ import annotations

from draftiq.draft.state import DraftStateMachine
from draftiq.models import RankBracket, Recommendation, Role, TermContribution
from draftiq.providers.base import StatsProvider
from draftiq.stats.composition import load_hand_curated_features
from draftiq.stats.scoring import score_candidate
from draftiq.stats.shrinkage import DEFAULT_K, DEFAULT_K_MATCHUP, compute_role_average

# Small additive bonus for popularity, on the same scale as comp_fit's penalties --
# a tiebreaker, not something that should dominate the ranking. At pick_rate=0.30
# (a heavily contested champion) this adds +0.03; at pick_rate=0.01 it's +0.001,
# effectively nothing.
PICK_RATE_WEIGHT_SCALE = 0.1


def suggest_bans(
    sm: DraftStateMachine,
    provider: StatsProvider,
    top_n: int = 5,
    k: float = DEFAULT_K,
    k_m: float = DEFAULT_K_MATCHUP,
) -> list[Recommendation]:
    if sm.is_complete():
        raise ValueError("Cannot suggest a ban: the draft is already complete.")

    champions = provider.get_champions()
    champion_by_id = {c.champion_id: c for c in champions}
    rank: RankBracket = sm.state.rank

    banning_side = sm.current_side()
    enemy_side = banning_side.other()
    our_picks = sm.picked_champion_ids(banning_side)
    enemy_picks = sm.picked_champion_ids(enemy_side)
    legal_ids = sm.legal_champion_ids(champion_by_id.keys())

    unfilled_roles = [r for r in Role if r not in sm.filled_roles(enemy_side)]
    if not unfilled_roles:
        unfilled_roles = list(Role)

    composition_table = load_hand_curated_features()

    p0_by_role: dict[Role, float] = {}
    for role in unfilled_roles:
        if hasattr(provider, "prefetch_for_suggest"):
            provider.prefetch_for_suggest(champion_by_id.keys(), role, rank)
            provider.prefetch_for_suggest(
                legal_ids,
                role,
                rank,
                include_matchups=True,
                include_synergies=bool(enemy_picks),
            )
        stats_by_champion = {
            c.champion_id: provider.get_champion_stats(c.champion_id, role, rank) for c in champions
        }
        p0_by_role[role] = compute_role_average(stats_by_champion.values())

    best_by_champion: dict[int, Recommendation] = {}
    for champ_id in legal_ids:
        champion = champion_by_id[champ_id]
        best_score = float("-inf")
        best_rec: Recommendation | None = None

        for role in unfilled_roles:
            rec = score_candidate(
                champion=champion,
                role=role,
                rank=rank,
                provider=provider,
                p0=p0_by_role[role],
                ally_ids=enemy_picks,  # their synergy, if they picked this
                enemy_ids=our_picks,  # the threat this poses to us
                champion_by_id=champion_by_id,
                k=k,
                k_m=k_m,
                composition_table=composition_table,
            )
            pick_rate = provider.get_champion_stats(champ_id, role, rank).pick_rate or 0.0
            pick_rate_bonus = PICK_RATE_WEIGHT_SCALE * pick_rate
            adjusted_score = rec.total_score + pick_rate_bonus

            if adjusted_score > best_score:
                best_score = adjusted_score
                terms = [
                    *rec.terms,
                    TermContribution(label="pick_rate weight", value=pick_rate_bonus),
                ]
                best_rec = rec.model_copy(update={"total_score": adjusted_score, "terms": terms})

        if best_rec is not None:
            best_by_champion[champ_id] = best_rec

    recommendations = list(best_by_champion.values())
    recommendations.sort(key=lambda r: r.total_score, reverse=True)
    return recommendations[:top_n]
