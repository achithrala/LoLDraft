"""Counterpick exposure: the spec's 4th score term, and the piece that actually
makes pick order matter.

If the enemy still has picks left after this one, a candidate with a severe
uncountered matchup remaining in the legal pool is risky -- picking it early (with
many enemy picks still to come) is riskier than picking the exact same champion
last (with zero enemy picks left to punish it). This finds the single worst
remaining counter for a candidate (by shrunk matchup delta -- the same shrinkage the
regular matchup term uses) and weights it by the probability the enemy actually
lands that specific counter across their remaining picks.

The spec says to weight by "how many enemy picks remain" and "how likely those
counters are to actually be picked (weight by pick rate)" as two separate factors,
but doesn't give an exact formula. Rather than two ad-hoc multipliers, this treats
each remaining enemy pick as an independent chance to land the worst counter and
combines both into one probability: `1 - (1 - pick_rate) ** remaining_picks`. More
remaining picks and a more popular counter both push this toward 1; zero remaining
picks (this is the enemy's last one, already spoken for) makes it exactly 0.
"""

from __future__ import annotations

from draftiq.models import Champion, RankBracket, Role, TermContribution
from draftiq.providers.base import StatsProvider
from draftiq.stats.shrinkage import DEFAULT_K_MATCHUP, shrink_delta


def compute_exposure(
    champion: Champion,
    role: Role,
    rank: RankBracket,
    provider: StatsProvider,
    base_p_hat: float,
    remaining_enemy_ids: set[int],
    remaining_enemy_picks: int,
    champion_by_id: dict[int, Champion],
    k_m: float = DEFAULT_K_MATCHUP,
) -> tuple[float, TermContribution | None]:
    """Returns `(exposure, term)`: `exposure` is a non-negative penalty to subtract
    from the candidate's score, and `term` is the breakdown entry to render (`None`
    if no counter with real matchup data was found in the remaining pool)."""
    worst_opponent_id: int | None = None
    worst_raw_exposure = 0.0

    for opponent_id in remaining_enemy_ids:
        if opponent_id == champion.champion_id:
            continue
        matchup = provider.get_matchup(champion.champion_id, opponent_id, role, rank)
        if matchup.games == 0:
            continue
        raw_wr = matchup.wins / matchup.games
        d_shrunk = shrink_delta(raw_wr - base_p_hat, matchup.games, k_m=k_m)
        raw_exposure = -d_shrunk
        if raw_exposure > worst_raw_exposure:
            worst_raw_exposure = raw_exposure
            worst_opponent_id = opponent_id

    if worst_opponent_id is None or worst_raw_exposure <= 0.0 or remaining_enemy_picks <= 0:
        return 0.0, None

    counter_stats = provider.get_champion_stats(worst_opponent_id, role, rank)
    pick_rate = counter_stats.pick_rate or 0.0
    likelihood = 1.0 - (1.0 - pick_rate) ** remaining_enemy_picks
    exposure = worst_raw_exposure * likelihood
    if exposure <= 0.0:
        return 0.0, None

    counter_name = (
        champion_by_id[worst_opponent_id].name
        if worst_opponent_id in champion_by_id
        else str(worst_opponent_id)
    )
    term = TermContribution(label=f"exposure to {counter_name}", value=-exposure)
    return exposure, term
