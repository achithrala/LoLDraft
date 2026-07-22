from __future__ import annotations

import pytest

from draftiq.draft.state import DraftStateMachine
from draftiq.models import DraftMode, RankBracket, Recommendation, Role
from draftiq.providers.manual import ManualCSVProvider
from draftiq.search.greedy import suggest
from draftiq.stats.scoring import score_candidate
from draftiq.stats.shrinkage import compute_role_average

# All ten bans go to ids that don't exist in the fixture's champion registry, so every
# real champion stays legal for the picks that follow -- these tests only care about
# the pick-phase scoring math, not ban validation.
DUMMY_BAN_IDS = range(1000, 1010)


def _burn_ban_phase(sm: DraftStateMachine) -> None:
    for champ_id in DUMMY_BAN_IDS:
        sm.apply_ban(champion_id=champ_id)


def _only(recs: list[Recommendation], ids: set[int]) -> list[Recommendation]:
    """suggest() scores every legal champion, including ones with zero data in the
    requested role (which all tie at p0). These tests care about the ordering among a
    specific handful of real top-lane champions, so filter the noise out rather than
    asserting on the full ranked list."""
    return [r for r in recs if r.champion_id in ids]


@pytest.fixture
def provider() -> ManualCSVProvider:
    return ManualCSVProvider()


class TestScoreCandidateBaseRateOnly:
    """score_candidate in isolation, with composition/exposure both disabled --
    the direct successor to what was originally a suggest()-level test, before
    composition fit and counterpick exposure became always-on parts of suggest().
    """

    def test_ranks_top_lane_by_shrunk_base_rate_only(self, provider: ManualCSVProvider) -> None:
        champions = provider.get_champions()
        champion_by_id = {c.champion_id: c for c in champions}
        stats_by_champ = {
            c.champion_id: provider.get_champion_stats(c.champion_id, Role.TOP, RankBracket.ALL)
            for c in champions
        }
        p0 = compute_role_average(stats_by_champ.values())

        recs = [
            score_candidate(
                champion=champion_by_id[champ_id],
                role=Role.TOP,
                rank=RankBracket.ALL,
                provider=provider,
                p0=p0,
                ally_ids=set(),
                enemy_ids=set(),
                champion_by_id=champion_by_id,
            )
            for champ_id in (1, 2, 3, 4)
        ]
        recs.sort(key=lambda r: r.total_score, reverse=True)

        # Jax has the highest raw win rate (54%) but only 500 games; shrinkage pulls
        # it down toward the ~51.6% role average, yet it should still edge out
        # Malphite (52.4% raw, 25k games) because 500 games isn't *nothing*.
        assert [r.champion_id for r in recs] == [4, 3, 2, 1]
        jax = recs[0]
        assert jax.p_hat == pytest.approx(0.5311, abs=5e-4)
        assert jax.total_score == pytest.approx(jax.p_hat)
        assert [t.label for t in jax.terms] == ["base_rate"]
        assert jax.ci_low < jax.p_hat < jax.ci_high


class TestSuggestWithPicksOnBoard:
    def test_matchup_and_synergy_deltas_shift_the_ranking(
        self, provider: ManualCSVProvider
    ) -> None:
        sm = DraftStateMachine.new(DraftMode.SOLOQ)
        _burn_ban_phase(sm)
        sm.apply_pick(champion_id=12, role=Role.MID)  # B1: blue picks Yasuo
        sm.apply_pick(champion_id=2, role=Role.TOP)  # R1: red picks Darius
        sm.apply_pick(champion_id=8, role=Role.JUNGLE)  # R2: red picks Kindred
        assert sm.current_side().value == "blue"  # now at B2

        recs = _only(suggest(sm, provider, role=Role.TOP, top_n=20), {1, 3, 4})
        ids = [r.champion_id for r in recs]
        # Jax: strong base rate + favorable matchup into Darius, no synergy data
        #   with Yasuo -> stays on top.
        # Aatrox: decent base rate, unfavorable matchup into Darius.
        # Malphite: big synergy bonus with Yasuo (wombo combo) but crushed by Darius
        #   in lane -> falls to the bottom despite the synergy term.
        assert ids == [4, 1, 3]

        by_id = {r.champion_id: r for r in recs}
        jax_terms = {t.label: t.value for t in by_id[4].terms}
        assert jax_terms["vs Darius"] == pytest.approx(0.0279, abs=5e-3)
        assert "with Yasuo" not in jax_terms  # no synergy data for Jax+Yasuo

        malphite_terms = {t.label: t.value for t in by_id[3].terms}
        assert malphite_terms["with Yasuo"] == pytest.approx(0.0511, abs=5e-3)
        assert malphite_terms["vs Darius"] == pytest.approx(-0.1299, abs=5e-3)
        assert by_id[3].total_score < by_id[4].total_score

    def test_zero_sample_matchup_or_synergy_contributes_nothing(
        self, provider: ManualCSVProvider
    ) -> None:
        # score_candidate directly, composition/exposure disabled: the same
        # isolation as TestScoreCandidateBaseRateOnly above, but exercising the
        # matchup/synergy loops with an ally whose synergy data doesn't exist.
        champions = provider.get_champions()
        champion_by_id = {c.champion_id: c for c in champions}
        stats_by_champ = {
            c.champion_id: provider.get_champion_stats(c.champion_id, Role.TOP, RankBracket.ALL)
            for c in champions
        }
        p0 = compute_role_average(stats_by_champ.values())

        jax = score_candidate(
            champion=champion_by_id[4],
            role=Role.TOP,
            rank=RankBracket.ALL,
            provider=provider,
            p0=p0,
            ally_ids={8},  # Kindred: no synergy data with Jax in the fixture
            enemy_ids={1},  # Aatrox: real matchup data exists
            champion_by_id=champion_by_id,
        )
        # Jax vs Aatrox has real matchup data; Jax's synergy with Kindred does not
        # exist in the fixture, so only the base_rate + matchup terms should appear.
        jax_labels = {t.label for t in jax.terms}
        assert jax_labels == {"base_rate", "vs Aatrox"}


class TestSuggestCompositionAndExposure:
    """End-to-end through suggest(): confirms composition fit and counterpick
    exposure (both wired in as always-on parts of the pipeline) actually surface in
    real recommendations, not just in their own isolated unit tests."""

    def test_composition_fit_penalizes_a_solo_ap_no_frontline_pick(
        self, provider: ManualCSVProvider
    ) -> None:
        sm = DraftStateMachine.new(DraftMode.SOLOQ)
        _burn_ban_phase(sm)
        assert sm.current_side().value == "blue"  # B1, nothing picked yet

        recs = _only(suggest(sm, provider, role=Role.MID, top_n=20), {9})  # Ahri
        ahri = recs[0]
        labels = {t.label for t in ahri.terms}
        # Ahri alone: 100% AP (damage skew) and no frontline champion on the team.
        assert "damage_skew" in labels
        assert "no_frontline" in labels

    def test_exposure_term_appears_for_a_real_remaining_counter(
        self, provider: ManualCSVProvider
    ) -> None:
        sm = DraftStateMachine.new(DraftMode.SOLOQ)
        _burn_ban_phase(sm)
        assert sm.current_side().value == "blue"  # B1: red has all 5 picks left

        recs = _only(suggest(sm, provider, role=Role.TOP, top_n=20), {3})  # Malphite
        malphite = recs[0]
        labels = {t.label for t in malphite.terms}
        # Darius remains in the pool and crushes Malphite in lane (39% win rate) --
        # with 5 red picks still to come, that must show up as real exposure.
        assert "exposure to Darius" in labels
        exposure_terms = [t.value for t in malphite.terms if t.label == "exposure to Darius"]
        assert exposure_terms[0] < 0.0
