from __future__ import annotations

import pytest

from draftiq.draft.state import DraftStateMachine
from draftiq.models import DraftMode, RankBracket, Role
from draftiq.providers.manual import ManualCSVProvider
from draftiq.search.ban import POOL_BONUS, suggest_bans
from draftiq.search.greedy import suggest as greedy_suggest
from draftiq.stats.scoring import score_candidate
from draftiq.stats.shrinkage import compute_role_average


def _provider() -> ManualCSVProvider:
    return ManualCSVProvider()


class TestSuggestBans:
    def test_returns_at_most_top_n(self) -> None:
        sm = DraftStateMachine.new(DraftMode.SOLOQ)
        recs = suggest_bans(sm, _provider(), top_n=3)
        assert len(recs) <= 3

    def test_pick_rate_weight_term_always_present(self) -> None:
        sm = DraftStateMachine.new(DraftMode.SOLOQ)
        recs = suggest_bans(sm, _provider(), top_n=20)
        assert recs
        for rec in recs:
            assert any(t.label == "pick_rate weight" for t in rec.terms)

    def test_differs_from_pick_suggest_ranking(self) -> None:
        """The whole point: ban value is about denying the opponent, a different
        question from "what's good for me" -- the two top picks need not agree."""
        sm = DraftStateMachine.new(DraftMode.SOLOQ)
        provider = _provider()

        ban_recs = suggest_bans(sm, provider, top_n=20)
        pick_recs = greedy_suggest(sm, provider, role=Role.TOP, top_n=20)

        assert ban_recs[0].champion_id != pick_recs[0].champion_id or [
            r.champion_id for r in ban_recs[:5]
        ] != [r.champion_id for r in pick_recs[:5]]

    def test_accounts_for_threat_to_our_existing_picks(self) -> None:
        """Darius crushes Malphite (39% win rate for Malphite, per matchups.csv).
        suggest_bans scores a candidate "for them" using our picks as the matchup
        side, so Darius should score higher once we have a Malphite to protect."""
        provider = _provider()
        champions = provider.get_champions()
        champion_by_id = {c.champion_id: c for c in champions}
        stats_by_champ = {
            c.champion_id: provider.get_champion_stats(c.champion_id, Role.TOP, RankBracket.ALL)
            for c in champions
        }
        p0 = compute_role_average(stats_by_champ.values())
        darius = champion_by_id[2]

        without_threat = score_candidate(
            champion=darius,
            role=Role.TOP,
            rank=RankBracket.ALL,
            provider=provider,
            p0=p0,
            ally_ids=set(),
            enemy_ids=set(),
            champion_by_id=champion_by_id,
        )
        with_threat = score_candidate(
            champion=darius,
            role=Role.TOP,
            rank=RankBracket.ALL,
            provider=provider,
            p0=p0,
            ally_ids=set(),
            enemy_ids={3},  # Malphite, our hypothetical existing pick
            champion_by_id=champion_by_id,
        )
        assert with_threat.total_score > without_threat.total_score

    def test_checks_every_unfilled_enemy_role_not_just_ours(self) -> None:
        """Bans aren't role-locked -- a champion who's mediocre for the role we're
        drafting but strong elsewhere should still be able to rank, since it's
        checked against every role the enemy hasn't filled yet."""
        sm = DraftStateMachine.new(DraftMode.SOLOQ)
        recs = suggest_bans(sm, _provider(), top_n=20)
        champion_ids = {r.champion_id for r in recs}
        # Jax (id=4, hand-curated as a top laner with a small-sample but high raw
        # win rate) should be reachable through suggest_bans checking the TOP role
        # among the enemy's unfilled roles, same as it would be for pick suggest.
        assert 4 in champion_ids

    def test_raises_when_draft_is_complete(self) -> None:
        sm = DraftStateMachine.new(DraftMode.SOLOQ)
        roles = [Role.TOP, Role.JUNGLE, Role.MID, Role.BOTTOM, Role.SUPPORT]
        role_cursor = {"blue": 0, "red": 0}
        champ_id = 1
        while not sm.is_complete():
            if sm.current_action_type().value == "ban":
                sm.apply_ban(champion_id=champ_id)
            else:
                side_key = sm.current_side().value
                role = roles[role_cursor[side_key]]
                role_cursor[side_key] += 1
                sm.apply_pick(champion_id=champ_id, role=role)
            champ_id += 1
        with pytest.raises(ValueError, match="already complete"):
            suggest_bans(sm, _provider())


class TestSuggestBansPool:
    """`pool_ids_by_role` is a bonus/highlight, not a restriction (unlike every
    other pool_ids*/pool_ids_by_role parameter in search/) -- see ban.py's module
    docstring: the full ban list is always shown, never narrowed to the enemy
    roster's known pool."""

    def test_in_pool_candidate_gets_bonus_term(self) -> None:
        sm = DraftStateMachine.new(DraftMode.SOLOQ)
        provider = _provider()
        without = {r.champion_id: r for r in suggest_bans(sm, provider, top_n=20)}
        with_pool = {
            r.champion_id: r
            for r in suggest_bans(sm, provider, top_n=20, pool_ids_by_role={Role.TOP: {4}})
        }
        assert not any(t.label == "in enemy pool" for t in without[4].terms)
        pool_term = next(t for t in with_pool[4].terms if t.label == "in enemy pool")
        assert pool_term.value == pytest.approx(POOL_BONUS)
        assert with_pool[4].total_score == pytest.approx(without[4].total_score + POOL_BONUS)

    def test_bonus_does_not_shrink_the_candidate_list(self) -> None:
        sm = DraftStateMachine.new(DraftMode.SOLOQ)
        provider = _provider()
        without = suggest_bans(sm, provider, top_n=20)
        with_pool = suggest_bans(sm, provider, top_n=20, pool_ids_by_role={Role.TOP: {4}})
        assert {r.champion_id for r in without} == {r.champion_id for r in with_pool}

    def test_out_of_pool_candidate_gets_no_bonus(self) -> None:
        sm = DraftStateMachine.new(DraftMode.SOLOQ)
        provider = _provider()
        recs = {
            r.champion_id: r
            for r in suggest_bans(sm, provider, top_n=20, pool_ids_by_role={Role.TOP: {4}})
        }
        # Aatrox (id=1) is a real top laner but not in this pool set.
        assert not any(t.label == "in enemy pool" for t in recs[1].terms)

    def test_multiple_ids_in_resolved_set_all_get_the_bonus(self) -> None:
        """The resolved set here stands in for a union across several enemy
        roster players (see models.consolidated_pool_ids) -- ban.py itself just
        needs to credit every id present, not only one."""
        sm = DraftStateMachine.new(DraftMode.SOLOQ)
        provider = _provider()
        recs = {
            r.champion_id: r
            for r in suggest_bans(sm, provider, top_n=20, pool_ids_by_role={Role.TOP: {1, 4}})
        }
        assert any(t.label == "in enemy pool" for t in recs[1].terms)
        assert any(t.label == "in enemy pool" for t in recs[4].terms)

    def test_none_for_a_role_is_unrestricted_for_that_role(self) -> None:
        """A `{role: None}` entry (no pool data for that role) must behave
        identically to that role not being in the dict at all."""
        sm = DraftStateMachine.new(DraftMode.SOLOQ)
        provider = _provider()
        without_param = suggest_bans(sm, provider, top_n=20)
        with_none = suggest_bans(sm, provider, top_n=20, pool_ids_by_role={Role.TOP: None})
        assert [r.champion_id for r in without_param] == [r.champion_id for r in with_none]
