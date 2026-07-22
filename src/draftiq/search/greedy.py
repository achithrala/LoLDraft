"""Phase 1 search: score every legal champion in the requested role independently
(no lookahead) and return the top N by total score. 2-ply lookahead is Phase 2.

`role` must always be supplied by the caller, for both picks and bans -- roles are
assigned before champion select (per the SOLOQ rules), so during the pick phase this
is simply "which role am I drafting for." During the ban phase there are no picks yet
in SOLOQ, so matchup/synergy/exposure terms are moot and this degenerates to "which
champions in this role currently have the strongest base rate" -- a reasonable proxy
for "worth denying," though true ban-specific reasoning is `ban_suggest` (see below).

A small popularity term is added on top of `score_candidate`'s result, same "small
additive tiebreaker" pattern already used by `search/ban.py` and
`search/priority.py`'s own pick-rate weighting -- added here after live OP.GG
suggestions surfaced legitimately-strong-but-rarely-played picks (e.g. a top-lane
Warwick with a large, reliable sample and a genuinely good win rate, just an uncommon
pick there) ahead of standard picks with a similar win rate. This is a distinct signal
from shrinkage's sample-size trust (`k`): `pick_rate` is the champion's share of a
*huge, stable* bracket-wide denominator (`total_games`, often in the millions for
OP.GG), so a rare-but-heavily-sampled pick isn't shrunk much by `k` at all -- shrinkage
alone cannot express "this performed well in the games it got, but few players
actually choose it."

The bonus is `POPULARITY_WEIGHT_SCALE * (pick_rate / max_pick_rate_among_legal_ids)`,
not a flat `POPULARITY_WEIGHT_SCALE * pick_rate` -- deliberately relative to the most
popular *legal* candidate in this exact role/rank query, not an absolute pick_rate
value. A fixed linear scale can't work across both data sources this project supports:
the manual dataset's pick rates are artificially spread (0.5% to 42%) to make
shrinkage's small-sample behavior easy to test, while real OP.GG pick rates for a
single role cluster tightly (single digits, e.g. 4-8% across an entire top lane pool)
-- a scale small enough not to blow up the manual dataset's extreme spread turned out
to be small enough to be a rounding error against real OP.GG data (confirmed: a live
top-lane query only ever produced a 0.001-0.002 bonus, nowhere near enough to compete
with a 1-2 percentage point win-rate gap). Normalizing by the pool's own max pick rate
makes one constant behave consistently regardless of how spread out a given dataset's
pick rates happen to be: the single most-picked legal champion in the role always gets
the full `POPULARITY_WEIGHT_SCALE`, everyone else scales down proportionally.
"""

from __future__ import annotations

from draftiq.draft.state import DraftStateMachine
from draftiq.models import RankBracket, Recommendation, Role, TermContribution
from draftiq.providers.base import StatsProvider
from draftiq.stats.composition import load_hand_curated_features
from draftiq.stats.scoring import score_candidate
from draftiq.stats.shrinkage import DEFAULT_K, DEFAULT_K_MATCHUP, compute_role_average

# The max bonus, given to whichever legal champion is most popular in this exact
# role/rank query -- same scale as stats/composition.py's fit penalties (0.01-0.02),
# since this is meant to nudge between close-in-win-rate options, not override a real
# win-rate edge.
POPULARITY_WEIGHT_SCALE = 0.02


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

    pick_rates = {champ_id: stats_by_champion[champ_id].pick_rate or 0.0 for champ_id in legal_ids}
    max_pick_rate = max(pick_rates.values(), default=0.0)

    recommendations = []
    for champ_id in legal_ids:
        rec = score_candidate(
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
        popularity_bonus = (
            POPULARITY_WEIGHT_SCALE * (pick_rates[champ_id] / max_pick_rate)
            if max_pick_rate > 0
            else 0.0
        )
        terms = [*rec.terms, TermContribution(label="popularity", value=popularity_bonus)]
        recommendations.append(
            rec.model_copy(
                update={"total_score": rec.total_score + popularity_bonus, "terms": terms}
            )
        )

    recommendations.sort(key=lambda r: r.total_score, reverse=True)
    return recommendations[:top_n]
