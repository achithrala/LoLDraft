"""The value function. All 5 terms from the spec are implemented: base rate,
matchup deltas, synergy deltas, composition fit, and counterpick exposure.

Every recommendation must be able to explain itself: `score_candidate` returns the
individual term contributions, not just the total, so the CLI can render exactly why
a champion was recommended.
"""

from __future__ import annotations

from draftiq.models import Champion, RankBracket, Recommendation, Role, TermContribution
from draftiq.providers.base import StatsProvider
from draftiq.stats.composition import CompositionFeatures, comp_fit, get_champion_features
from draftiq.stats.exposure import compute_exposure
from draftiq.stats.shrinkage import DEFAULT_K, DEFAULT_K_MATCHUP, shrink_delta, shrink_win_rate


def score_candidate(
    champion: Champion,
    role: Role,
    rank: RankBracket,
    provider: StatsProvider,
    p0: float,
    ally_ids: set[int],
    enemy_ids: set[int],
    champion_by_id: dict[int, Champion],
    k: float = DEFAULT_K,
    k_m: float = DEFAULT_K_MATCHUP,
    composition_table: dict[str, CompositionFeatures] | None = None,
    remaining_enemy_ids: set[int] | None = None,
    remaining_enemy_picks: int = 0,
) -> Recommendation:
    stats = provider.get_champion_stats(champion.champion_id, role, rank)
    base = shrink_win_rate(stats.wins, stats.games, p0, k=k)

    terms = [TermContribution(label="base_rate", value=base.p_hat)]
    total = base.p_hat

    for enemy_id in sorted(enemy_ids):
        matchup = provider.get_matchup(champion.champion_id, enemy_id, role, rank)
        if matchup.games == 0:
            continue
        raw_wr = matchup.wins / matchup.games
        d_raw = raw_wr - base.p_hat
        d_shrunk = shrink_delta(d_raw, matchup.games, k_m=k_m)
        enemy_name = champion_by_id[enemy_id].name if enemy_id in champion_by_id else str(enemy_id)
        terms.append(TermContribution(label=f"vs {enemy_name}", value=d_shrunk))
        total += d_shrunk

    for ally_id in sorted(ally_ids):
        synergy = provider.get_synergy(champion.champion_id, ally_id, rank)
        if synergy.games == 0:
            continue
        raw_wr = synergy.wins / synergy.games
        d_raw = raw_wr - base.p_hat
        d_shrunk = shrink_delta(d_raw, synergy.games, k_m=k_m)
        ally_name = champion_by_id[ally_id].name if ally_id in champion_by_id else str(ally_id)
        terms.append(TermContribution(label=f"with {ally_name}", value=d_shrunk))
        total += d_shrunk

    if composition_table is not None:
        candidate_features = get_champion_features(champion, composition_table)
        ally_features = [
            get_champion_features(champion_by_id[aid], composition_table)
            for aid in ally_ids
            if aid in champion_by_id
        ]
        fit_total, fit_terms = comp_fit(candidate_features, ally_features)
        terms.extend(fit_terms)
        total += fit_total

    if remaining_enemy_ids and remaining_enemy_picks > 0:
        exposure, exposure_term = compute_exposure(
            champion=champion,
            role=role,
            rank=rank,
            provider=provider,
            base_p_hat=base.p_hat,
            remaining_enemy_ids=remaining_enemy_ids,
            remaining_enemy_picks=remaining_enemy_picks,
            champion_by_id=champion_by_id,
            k_m=k_m,
        )
        if exposure_term is not None:
            terms.append(exposure_term)
        total -= exposure

    return Recommendation(
        champion_id=champion.champion_id,
        champion_name=champion.name,
        role=role,
        total_score=total,
        p_hat=base.p_hat,
        ci_low=base.ci_low,
        ci_high=base.ci_high,
        n_games=stats.games,
        terms=terms,
    )
