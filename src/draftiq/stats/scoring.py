"""The value function. Phase 1 implements the base-rate and delta terms only --
composition fit and counterpick exposure (score.py's remaining two terms in the
spec) land in Phase 2 once there's a composition feature vector to score against.

Every recommendation must be able to explain itself: `score_candidate` returns the
individual term contributions, not just the total, so the CLI can render exactly why
a champion was recommended.
"""

from __future__ import annotations

from draftiq.models import Champion, RankBracket, Recommendation, Role, TermContribution
from draftiq.providers.base import StatsProvider
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

    return Recommendation(
        champion_id=champion.champion_id,
        champion_name=champion.name,
        total_score=total,
        p_hat=base.p_hat,
        ci_low=base.ci_low,
        ci_high=base.ci_high,
        n_games=stats.games,
        terms=terms,
    )
