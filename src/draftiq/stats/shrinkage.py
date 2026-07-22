"""Empirical Bayes shrinkage toward a role's average win rate, plus Beta credible
intervals.

No dependency outside the standard library is approved for this project, so the Beta
distribution's quantile function (needed for the credible interval) is implemented
directly via the regularized incomplete beta function (Lentz's continued-fraction
method, as in Numerical Recipes) and inverted by bisection. This is a well-known,
numerically stable algorithm -- see `tests/test_shrinkage.py` for validation against
closed-form cases (the Beta(1,1) = Uniform(0,1) special case in particular).
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

from draftiq.models import ChampionStats

DEFAULT_K = 300.0
DEFAULT_K_MATCHUP = 150.0
DEFAULT_CREDIBLE_MASS = 0.90

_MAX_ITER = 200
_EPS = 3e-16
_FPMIN = 1e-300


def _log_beta(a: float, b: float) -> float:
    return math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function."""
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < _FPMIN:
        d = _FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, _MAX_ITER + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < _FPMIN:
            d = _FPMIN
        c = 1.0 + aa / c
        if abs(c) < _FPMIN:
            c = _FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < _FPMIN:
            d = _FPMIN
        c = 1.0 + aa / c
        if abs(c) < _FPMIN:
            c = _FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < _EPS:
            break
    return h


def regularized_incomplete_beta(a: float, b: float, x: float) -> float:
    """I_x(a, b): the Beta(a, b) CDF evaluated at x."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    log_bt = -_log_beta(a, b) + a * math.log(x) + b * math.log(1.0 - x)
    bt = math.exp(log_bt)
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def beta_ppf(p: float, a: float, b: float, tol: float = 1e-10) -> float:
    """Inverse Beta(a, b) CDF via bisection. `p` must be in [0, 1]."""
    if not 0.0 <= p <= 1.0:
        raise ValueError(f"p must be in [0, 1], got {p}")
    if p == 0.0:
        return 0.0
    if p == 1.0:
        return 1.0
    lo, hi = 0.0, 1.0
    for _ in range(_MAX_ITER):
        mid = (lo + hi) / 2
        if regularized_incomplete_beta(a, b, mid) < p:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    return (lo + hi) / 2


@dataclass(frozen=True)
class ShrinkageResult:
    p_hat: float
    alpha: float
    beta: float
    ci_low: float
    ci_high: float
    n_games: int


def compute_role_average(stats: Iterable[ChampionStats]) -> float:
    """p0: the games-weighted average win rate across a set of champions (typically
    all champions in one role/rank bracket). Falls back to 0.50 only when there is
    truly no data at all -- never hardcoded as the default."""
    total_wins = 0
    total_games = 0
    for s in stats:
        total_wins += s.wins
        total_games += s.games
    if total_games == 0:
        return 0.5
    return total_wins / total_games


def shrink_win_rate(
    wins: int,
    games: int,
    p0: float,
    k: float = DEFAULT_K,
    credible_mass: float = DEFAULT_CREDIBLE_MASS,
) -> ShrinkageResult:
    """p_hat = (w + k*p0) / (n + k), with a Beta(w + k*p0, n - w + k*(1-p0))
    posterior and its `credible_mass` credible interval."""
    if wins < 0 or games < 0 or wins > games:
        raise ValueError(f"invalid wins/games: wins={wins}, games={games}")
    if not 0.0 <= p0 <= 1.0:
        raise ValueError(f"p0 must be in [0, 1], got {p0}")
    if k <= 0:
        raise ValueError(f"k must be positive, got {k}")
    if not 0.0 < credible_mass < 1.0:
        raise ValueError(f"credible_mass must be in (0, 1), got {credible_mass}")

    alpha = wins + k * p0
    beta_param = games - wins + k * (1.0 - p0)
    p_hat = alpha / (alpha + beta_param)

    tail = (1.0 - credible_mass) / 2.0
    ci_low = beta_ppf(tail, alpha, beta_param)
    ci_high = beta_ppf(1.0 - tail, alpha, beta_param)

    return ShrinkageResult(
        p_hat=p_hat,
        alpha=alpha,
        beta=beta_param,
        ci_low=ci_low,
        ci_high=ci_high,
        n_games=games,
    )


def shrink_delta(d_raw: float, n: int, k_m: float = DEFAULT_K_MATCHUP) -> float:
    """Shrinks a raw matchup/synergy delta toward zero in proportion to its sample
    size: `d_raw * n / (n + k_m)`. A delta with n=0 always collapses to exactly 0."""
    if n < 0:
        raise ValueError(f"n must be non-negative, got {n}")
    if k_m <= 0:
        raise ValueError(f"k_m must be positive, got {k_m}")
    if n == 0:
        return 0.0
    return d_raw * n / (n + k_m)
