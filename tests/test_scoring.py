from __future__ import annotations

import pytest

from draftiq.draft.state import DraftStateMachine
from draftiq.models import DraftMode, Recommendation, Role
from draftiq.providers.manual import ManualCSVProvider
from draftiq.search.greedy import suggest

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


class TestSuggestFreshDraft:
    def test_ranks_top_lane_by_shrunk_base_rate_only(self, provider: ManualCSVProvider) -> None:
        sm = DraftStateMachine.new(DraftMode.SOLOQ)
        _burn_ban_phase(sm)
        assert sm.current_action_type().value == "pick"  # B1, nothing drafted yet

        recs = _only(suggest(sm, provider, role=Role.TOP, top_n=20), {1, 2, 3, 4})
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
        sm = DraftStateMachine.new(DraftMode.SOLOQ)
        _burn_ban_phase(sm)
        sm.apply_pick(champion_id=8, role=Role.JUNGLE)  # B1: Kindred (niche, tiny samples)
        sm.apply_pick(champion_id=1, role=Role.TOP)  # R1: Aatrox
        sm.apply_pick(champion_id=6, role=Role.JUNGLE)  # R2: Vi

        recs = _only(suggest(sm, provider, role=Role.TOP, top_n=20), {4})
        # Jax vs Aatrox has real matchup data; Jax's synergy with Kindred does not
        # exist in the fixture, so only the base_rate + matchup terms should appear.
        jax_labels = {t.label for t in recs[0].terms}
        assert jax_labels == {"base_rate", "vs Aatrox"}
