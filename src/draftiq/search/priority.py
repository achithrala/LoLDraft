"""Champion-priority recommendations: "which champion should I grab right now,
whichever role it ends up filling for me" -- a different question from
`greedy.suggest`'s single-role ranking (which champion is best *for a role I've
already chosen*) and from `search/ban.py`'s cross-role check (which is about the
*opponent's* unfilled roles, to find ban value, not pick value).

Three things combine, all reusing patterns already established elsewhere in search/:

1. Cross-role scoring. Same trick as `search/ban.py`: score each legal champion
   against every one of *this side's* still-unfilled roles via `score_candidate`.
   This is what makes a champion who happens to be mediocre in the role you'd
   naively slot them into, but excellent in another role you still need, show up
   correctly. Unlike ban.py, the *full* per-role breakdown is kept (not just the
   best), because flex value (below) needs to compare across roles, not just find
   the max.

2. Flex bonus. A champion who scores well in more than one of your unfilled roles
   is worth more than the single-role score says, independent of win rate: keeping
   them uncommitted to one role for longer denies the opponent information (they
   can't safely counter-pick or ban assuming you'll play it top vs. support) and
   keeps your own options open if a later pick or ban changes what you need. A role
   only counts toward this if it has actual sample data (`n_games > 0`) *and* its
   score is within `FLEX_VIABILITY_MARGIN` of the champion's best role --
   "also plays support" only has draft value if they'd actually be *good* there, not
   just legal there. `FLEX_BONUS_PER_ROLE` is applied per viable role beyond the
   first.

   The `n_games > 0` gate matters more than it looks: a champion with zero recorded
   games in a role shrinks all the way to that role's population-average win rate
   (`shrink_win_rate` with n=0 returns exactly `p0`), which is "no evidence," not
   "proven competence." Without this gate, a jungler who's never once been played
   support would still show a support score close to the support baseline and get
   flagged as a flex pick into it -- treating shrinkage's "we don't know, so assume
   average" as if it meant "we know they're average there." Best-role selection
   (below) applies the same gate for the same reason: a champion's own real role
   should never lose out to an unplayed role that happens to have a slightly higher
   population baseline.

3. Contest risk. A champion who's popular enough that the opponent might take them
   before you get another chance is more urgent to grab now than an equally-strong
   champion nobody else wants. Same shape as counterpick exposure
   (`stats/exposure.py`) and `search/ban.py`'s pick-rate weighting:
   `1 - (1 - pick_rate) ** remaining_enemy_picks`, the probability the enemy lands
   this exact champion across their remaining picks, treated as an independent
   chance per pick (same simplifying assumption exposure and ban.py already make).
   Pick rate is read from the champion's best-scoring role, since that's the role
   both sides are likeliest to actually contest them in.

No new scoring math -- `score_candidate` is reused unchanged, exactly like
`search/ban.py` does; both bonuses are small additive tiebreakers on top of it, on
the same scale as `search/ban.py`'s pick-rate weighting and `stats/composition.py`'s
fit penalties.

`pool_ids_by_role`, if given, is an *already-resolved* `{role: allowed_ids_or_None}`
map (see `models.consolidated_pool_ids`) restricting which roles a candidate is even
considered for -- per-role, not a single flat filter, since a champion pooled for
"top" has no business being scored for "jungle" just because they're pooled
somewhere. Ineligible roles are dropped *before* calling `score_candidate` (not
scored then discarded) to avoid paying for a network round-trip on a role that will
never be used. A candidate eligible in zero of their unfilled roles is skipped
entirely -- this is what keeps the later `max(rankable_roles.values(), ...)` call
from ever seeing an empty sequence.
"""

from __future__ import annotations

from draftiq.draft.state import DraftStateMachine
from draftiq.models import RankBracket, Recommendation, Role, TermContribution
from draftiq.providers.base import StatsProvider
from draftiq.stats.composition import load_hand_curated_features
from draftiq.stats.scoring import score_candidate
from draftiq.stats.shrinkage import DEFAULT_K, DEFAULT_K_MATCHUP, compute_role_average

# Same scale as search/ban.py's PICK_RATE_WEIGHT_SCALE -- a tiebreaker, not something
# that should dominate the ranking. At contest_risk=1.0 (near-certain to be taken)
# this adds +0.1; a champion nobody's contesting adds ~0.
CONTEST_RISK_WEIGHT_SCALE = 0.1

# How close another role's score must be to the champion's best role to count as a
# genuine flex option rather than a fallback they'd only play under duress.
FLEX_VIABILITY_MARGIN = 0.03

# Additive bonus per viable role beyond the champion's best one. Small and linear --
# a true 3-role flex (rare) is more valuable than a 2-role one, but this should never
# outweigh a large win-rate gap on its own.
FLEX_BONUS_PER_ROLE = 0.015


def suggest_priority(
    sm: DraftStateMachine,
    provider: StatsProvider,
    top_n: int = 5,
    k: float = DEFAULT_K,
    k_m: float = DEFAULT_K_MATCHUP,
    pool_ids_by_role: dict[Role, set[int] | None] | None = None,
) -> list[Recommendation]:
    if sm.is_complete():
        raise ValueError("Cannot suggest a pick: the draft is already complete.")

    champions = provider.get_champions()
    champion_by_id = {c.champion_id: c for c in champions}
    rank: RankBracket = sm.state.rank

    side = sm.current_side()
    ally_ids = sm.picked_champion_ids(side)
    enemy_ids = sm.picked_champion_ids(side.other())
    legal_ids = sm.legal_champion_ids(champion_by_id.keys())
    remaining_enemy_picks = sm.remaining_picks(side.other())

    unfilled_roles = [r for r in Role if r not in sm.filled_roles(side)]
    if not unfilled_roles:
        raise ValueError(f"{side.value} has already filled every role.")

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
                include_synergies=bool(ally_ids),
            )
        stats_by_champion = {
            c.champion_id: provider.get_champion_stats(c.champion_id, role, rank) for c in champions
        }
        p0_by_role[role] = compute_role_average(stats_by_champion.values())

    best_by_champion: dict[int, Recommendation] = {}
    for champ_id in legal_ids:
        champion = champion_by_id[champ_id]
        eligible_roles = []
        for role in unfilled_roles:
            allowed = pool_ids_by_role.get(role) if pool_ids_by_role is not None else None
            if allowed is None or champ_id in allowed:
                eligible_roles.append(role)
        if not eligible_roles:
            continue

        rec_by_role: dict[Role, Recommendation] = {
            role: score_candidate(
                champion=champion,
                role=role,
                rank=rank,
                provider=provider,
                p0=p0_by_role[role],
                ally_ids=ally_ids,
                enemy_ids=enemy_ids,
                champion_by_id=champion_by_id,
                k=k,
                k_m=k_m,
                composition_table=composition_table,
                remaining_enemy_ids=legal_ids,
                remaining_enemy_picks=remaining_enemy_picks,
            )
            for role in eligible_roles
        }

        # Only roles with actual sample data can establish "this champion is good
        # here" -- a games=0 role has merely collapsed to that role's population
        # baseline, which is not evidence of anything. Fall back to every unfilled
        # role only if the champion truly has no data anywhere (e.g. a brand new
        # champion), so they still get *a* recommendation rather than none at all.
        played_roles = {role: rec for role, rec in rec_by_role.items() if rec.n_games > 0}
        rankable_roles = played_roles or rec_by_role

        best_rec = max(rankable_roles.values(), key=lambda r: r.total_score)
        viable_roles = [
            role
            for role, rec in rankable_roles.items()
            if rec.total_score >= best_rec.total_score - FLEX_VIABILITY_MARGIN
        ]
        flex_bonus = FLEX_BONUS_PER_ROLE * (len(viable_roles) - 1)

        pick_rate = provider.get_champion_stats(champ_id, best_rec.role, rank).pick_rate or 0.0
        contest_risk = 1.0 - (1.0 - pick_rate) ** remaining_enemy_picks
        contest_bonus = CONTEST_RISK_WEIGHT_SCALE * contest_risk

        terms = list(best_rec.terms)
        if flex_bonus > 0:
            other_roles = ", ".join(r.value for r in viable_roles if r is not best_rec.role)
            terms.append(TermContribution(label=f"flex ({other_roles})", value=flex_bonus))
        terms.append(TermContribution(label="contest_risk", value=contest_bonus))

        best_by_champion[champ_id] = best_rec.model_copy(
            update={
                "total_score": best_rec.total_score + flex_bonus + contest_bonus,
                "terms": terms,
            }
        )

    recommendations = list(best_by_champion.values())
    recommendations.sort(key=lambda r: r.total_score, reverse=True)
    return recommendations[:top_n]
