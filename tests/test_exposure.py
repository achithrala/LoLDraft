from __future__ import annotations

import pytest

from draftiq.models import RankBracket, Role
from draftiq.providers.manual import ManualCSVProvider
from draftiq.stats.exposure import compute_exposure
from draftiq.stats.shrinkage import compute_role_average, shrink_win_rate

MALPHITE = 3
DARIUS = 2  # Malphite loses this matchup badly (39% win rate, 5000 games)
JAX = 4  # Malphite also loses to Jax (45% win rate, 3000 games) -- less severe
AATROX = 1


@pytest.fixture
def provider() -> ManualCSVProvider:
    return ManualCSVProvider()


def _p_hat_for(provider: ManualCSVProvider, champion_id: int) -> float:
    champions = provider.get_champions()
    stats_by_champ = {
        c.champion_id: provider.get_champion_stats(c.champion_id, Role.TOP, RankBracket.ALL)
        for c in champions
    }
    p0 = compute_role_average(stats_by_champ.values())
    stats = stats_by_champ[champion_id]
    return shrink_win_rate(stats.wins, stats.games, p0).p_hat


class TestComputeExposure:
    def test_more_remaining_picks_means_more_exposure(self, provider: ManualCSVProvider) -> None:
        """This is the pick-order-priority behavior: the exact same candidate, in
        the exact same board state, is riskier to pick when the enemy has more
        picks left to find a counter with."""
        champion = {c.champion_id: c for c in provider.get_champions()}[MALPHITE]
        base_p_hat = _p_hat_for(provider, MALPHITE)
        champion_by_id = {c.champion_id: c for c in provider.get_champions()}
        remaining_pool = {DARIUS, JAX, AATROX}

        exposure_many_picks_left, _ = compute_exposure(
            champion=champion,
            role=Role.TOP,
            rank=RankBracket.ALL,
            provider=provider,
            base_p_hat=base_p_hat,
            remaining_enemy_ids=remaining_pool,
            remaining_enemy_picks=5,
            champion_by_id=champion_by_id,
        )
        exposure_one_pick_left, _ = compute_exposure(
            champion=champion,
            role=Role.TOP,
            rank=RankBracket.ALL,
            provider=provider,
            base_p_hat=base_p_hat,
            remaining_enemy_ids=remaining_pool,
            remaining_enemy_picks=1,
            champion_by_id=champion_by_id,
        )
        assert exposure_many_picks_left > exposure_one_pick_left > 0.0

    def test_zero_remaining_picks_means_zero_exposure(self, provider: ManualCSVProvider) -> None:
        """Picking last (the enemy has no picks left after this one) means there is
        no future counter risk at all -- exposure must be exactly zero, not just
        small."""
        champion = {c.champion_id: c for c in provider.get_champions()}[MALPHITE]
        base_p_hat = _p_hat_for(provider, MALPHITE)
        champion_by_id = {c.champion_id: c for c in provider.get_champions()}

        exposure, term = compute_exposure(
            champion=champion,
            role=Role.TOP,
            rank=RankBracket.ALL,
            provider=provider,
            base_p_hat=base_p_hat,
            remaining_enemy_ids={DARIUS, JAX, AATROX},
            remaining_enemy_picks=0,
            champion_by_id=champion_by_id,
        )
        assert exposure == 0.0
        assert term is None

    def test_picks_the_single_worst_counter(self, provider: ManualCSVProvider) -> None:
        champion = {c.champion_id: c for c in provider.get_champions()}[MALPHITE]
        base_p_hat = _p_hat_for(provider, MALPHITE)
        champion_by_id = {c.champion_id: c for c in provider.get_champions()}

        _, term = compute_exposure(
            champion=champion,
            role=Role.TOP,
            rank=RankBracket.ALL,
            provider=provider,
            base_p_hat=base_p_hat,
            remaining_enemy_ids={DARIUS, JAX, AATROX},
            remaining_enemy_picks=5,
            champion_by_id=champion_by_id,
        )
        # Darius (39% win rate, 5000 games) is a more severe counter to Malphite
        # than Jax (45% win rate, 3000 games) -- Darius should be picked, not Jax.
        assert term is not None
        assert term.label == "exposure to Darius"
        assert term.value < 0.0

    def test_no_matchup_data_in_remaining_pool_gives_zero_exposure(
        self, provider: ManualCSVProvider
    ) -> None:
        champion = {c.champion_id: c for c in provider.get_champions()}[MALPHITE]
        base_p_hat = _p_hat_for(provider, MALPHITE)
        champion_by_id = {c.champion_id: c for c in provider.get_champions()}

        # Champions 13-18 (bottom/support) have no TOP-role matchup data vs Malphite.
        exposure, term = compute_exposure(
            champion=champion,
            role=Role.TOP,
            rank=RankBracket.ALL,
            provider=provider,
            base_p_hat=base_p_hat,
            remaining_enemy_ids={13, 14, 15, 16, 17, 18},
            remaining_enemy_picks=5,
            champion_by_id=champion_by_id,
        )
        assert exposure == 0.0
        assert term is None

    def test_favorable_matchup_is_not_exposure(self, provider: ManualCSVProvider) -> None:
        """Jax beats Darius 56% (per matchups.csv) -- a favorable matchup should
        never register as exposure, since exposure only cares about matchups the
        candidate loses."""
        champion = {c.champion_id: c for c in provider.get_champions()}[JAX]
        base_p_hat = _p_hat_for(provider, JAX)
        champion_by_id = {c.champion_id: c for c in provider.get_champions()}

        exposure, term = compute_exposure(
            champion=champion,
            role=Role.TOP,
            rank=RankBracket.ALL,
            provider=provider,
            base_p_hat=base_p_hat,
            remaining_enemy_ids={DARIUS},
            remaining_enemy_picks=5,
            champion_by_id=champion_by_id,
        )
        assert exposure == 0.0
        assert term is None

    def test_candidate_excluded_from_its_own_remaining_pool(
        self, provider: ManualCSVProvider
    ) -> None:
        """If the candidate itself is (erroneously) included in remaining_enemy_ids,
        it must never be compared against itself."""
        champion = {c.champion_id: c for c in provider.get_champions()}[MALPHITE]
        base_p_hat = _p_hat_for(provider, MALPHITE)
        champion_by_id = {c.champion_id: c for c in provider.get_champions()}

        exposure, term = compute_exposure(
            champion=champion,
            role=Role.TOP,
            rank=RankBracket.ALL,
            provider=provider,
            base_p_hat=base_p_hat,
            remaining_enemy_ids={MALPHITE},
            remaining_enemy_picks=5,
            champion_by_id=champion_by_id,
        )
        assert exposure == 0.0
        assert term is None
