from __future__ import annotations

import pytest

from draftiq.models import ChampionStats, RankBracket, Role
from draftiq.stats.shrinkage import (
    beta_ppf,
    compute_role_average,
    regularized_incomplete_beta,
    shrink_delta,
    shrink_win_rate,
)


def _stats(wins: int, games: int) -> ChampionStats:
    return ChampionStats(
        champion_id=1,
        role=Role.TOP,
        rank=RankBracket.ALL,
        patch="SYNTH-1",
        wins=wins,
        games=games,
    )


class TestRegularizedIncompleteBeta:
    def test_boundaries(self) -> None:
        assert regularized_incomplete_beta(2.0, 3.0, 0.0) == 0.0
        assert regularized_incomplete_beta(2.0, 3.0, 1.0) == 1.0

    def test_uniform_distribution_is_identity(self) -> None:
        # Beta(1, 1) is Uniform(0, 1), so its CDF is the identity function.
        for x in (0.1, 0.25, 0.5, 0.75, 0.9):
            assert regularized_incomplete_beta(1.0, 1.0, x) == pytest.approx(x, abs=1e-9)

    def test_symmetric_case_median(self) -> None:
        # Beta(a, a) is symmetric about 0.5.
        assert regularized_incomplete_beta(5.0, 5.0, 0.5) == pytest.approx(0.5, abs=1e-9)


class TestBetaPpf:
    def test_uniform_ppf_is_identity(self) -> None:
        for p in (0.05, 0.25, 0.5, 0.75, 0.95):
            assert beta_ppf(p, 1.0, 1.0) == pytest.approx(p, abs=1e-8)

    def test_roundtrips_through_cdf(self) -> None:
        a, b = 12.0, 37.0
        for p in (0.05, 0.5, 0.95):
            x = beta_ppf(p, a, b)
            assert regularized_incomplete_beta(a, b, x) == pytest.approx(p, abs=1e-6)

    def test_endpoints(self) -> None:
        assert beta_ppf(0.0, 3.0, 4.0) == 0.0
        assert beta_ppf(1.0, 3.0, 4.0) == 1.0

    def test_rejects_out_of_range_p(self) -> None:
        with pytest.raises(ValueError):
            beta_ppf(1.5, 2.0, 2.0)
        with pytest.raises(ValueError):
            beta_ppf(-0.1, 2.0, 2.0)


class TestComputeRoleAverage:
    def test_weighted_by_games(self) -> None:
        # One champion with a huge sample should dominate a tiny one.
        stats = [_stats(wins=5100, games=10000), _stats(wins=9, games=10)]
        p0 = compute_role_average(stats)
        assert p0 == pytest.approx((5100 + 9) / (10000 + 10))

    def test_empty_falls_back_to_half(self) -> None:
        assert compute_role_average([]) == 0.5

    def test_all_zero_games_falls_back_to_half(self) -> None:
        stats = [_stats(wins=0, games=0), _stats(wins=0, games=0)]
        assert compute_role_average(stats) == 0.5


class TestShrinkWinRate:
    def test_large_sample_close_to_raw_rate(self) -> None:
        result = shrink_win_rate(wins=52000, games=100000, p0=0.5, k=300.0)
        assert result.p_hat == pytest.approx(0.52, abs=0.002)

    def test_zero_games_collapses_to_p0(self) -> None:
        result = shrink_win_rate(wins=0, games=0, p0=0.51, k=300.0)
        assert result.p_hat == pytest.approx(0.51)

    def test_small_sample_pulled_toward_p0(self) -> None:
        # 8/10 raw win rate (0.8) with only 10 games should land far below 0.8,
        # much closer to p0=0.5 than to the raw rate.
        result = shrink_win_rate(wins=8, games=10, p0=0.5, k=300.0)
        assert result.p_hat < 0.55

    def test_credible_interval_contains_p_hat_and_widens_with_fewer_games(self) -> None:
        wide = shrink_win_rate(wins=26, games=50, p0=0.5, k=300.0)
        narrow = shrink_win_rate(wins=26000, games=50000, p0=0.5, k=300.0)
        assert wide.ci_low < wide.p_hat < wide.ci_high
        assert narrow.ci_low < narrow.p_hat < narrow.ci_high
        assert (wide.ci_high - wide.ci_low) > (narrow.ci_high - narrow.ci_low)

    def test_rejects_wins_exceeding_games(self) -> None:
        with pytest.raises(ValueError):
            shrink_win_rate(wins=11, games=10, p0=0.5)

    def test_rejects_p0_out_of_range(self) -> None:
        with pytest.raises(ValueError):
            shrink_win_rate(wins=1, games=10, p0=1.5)


class TestShrinkDelta:
    def test_zero_games_gives_zero_influence(self) -> None:
        assert shrink_delta(d_raw=0.3, n=0) == 0.0

    def test_large_sample_approaches_raw_delta(self) -> None:
        result = shrink_delta(d_raw=0.1, n=1_000_000, k_m=150.0)
        assert result == pytest.approx(0.1, abs=1e-3)

    def test_small_sample_mostly_collapsed(self) -> None:
        result = shrink_delta(d_raw=0.5, n=5, k_m=150.0)
        assert abs(result) < 0.05

    def test_rejects_negative_n(self) -> None:
        with pytest.raises(ValueError):
            shrink_delta(d_raw=0.1, n=-1)
