"""SPOT: the alert threshold from extreme value theory (T11; DET-05, PR-06).

The PRD asks for the threshold to come from the tail of healthy scores rather
than a plain quantile. SPOT (Siffer et al., 2017) looks only at the healthy
scores above a starting level ``t`` (an upper quantile), fits a Generalized
Pareto distribution to how far they exceed it, and reads off the score that
healthy operation would exceed with a chosen small probability ``q``:

    threshold = t + (sigma / gamma) * ((q * n / n_t) ** (-gamma) - 1)

with the exponential limit ``t - sigma * log(q * n / n_t)`` as gamma -> 0.

Only SPOT, never DSPOT: DSPOT follows drift and would absorb a slow leak. The
risk level q is chosen on the calibrate weeks with the same gate and budget
the empirical rule uses (``calibrate_q``): the largest q at which gated
incidents, and those at every smaller q, stay within two a week. Nothing here
looks at the four failures.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np
from scipy.stats import genpareto

from pdm_common.gate import GateParams, gate

WEEK_MIN = 7 * 1440
Q_GRID = np.logspace(-2, -6, 81)          # 1e-2 down to 1e-6, twenty points a decade


@dataclass(frozen=True)
class SpotFit:
    t: float              # the starting level: the init_quantile of the healthy scores
    gamma: float          # GPD shape
    sigma: float          # GPD scale
    n: int                # healthy scores
    n_t: int              # of them above t
    init_quantile: float

    def as_dict(self) -> dict:
        return asdict(self)


def fit_spot(scores, init_quantile: float = 0.98) -> SpotFit:
    """Fit the GPD to the excesses of the healthy ``scores`` over their
    ``init_quantile`` (maximum likelihood, location fixed at 0)."""
    scores = np.asarray(scores, dtype=float)
    scores = scores[np.isfinite(scores)]
    t = float(np.quantile(scores, init_quantile))
    excess = scores[scores > t] - t
    if len(excess) < 10:
        raise ValueError(f"only {len(excess)} scores above the {init_quantile} quantile; SPOT needs more")
    gamma, _, sigma = genpareto.fit(excess, floc=0)
    return SpotFit(t=t, gamma=float(gamma), sigma=float(sigma), n=int(len(scores)), n_t=int(len(excess)),
                   init_quantile=float(init_quantile))


def threshold(fit: SpotFit, q: float) -> float:
    """The score healthy operation exceeds with probability ``q``."""
    r = q * fit.n / fit.n_t
    if abs(fit.gamma) < 1e-6:
        return fit.t - fit.sigma * math.log(r)
    return fit.t + (fit.sigma / fit.gamma) * (r ** (-fit.gamma) - 1)


def _rate(end_ns, scores, tau, params: GateParams) -> float:
    """Gated incidents per week of window time (one window per minute)."""
    opens, _, _ = gate(end_ns, np.asarray(scores) > tau, params)
    return len(opens) / (len(end_ns) / WEEK_MIN) if len(end_ns) else 0.0


def calibrate_q(fit: SpotFit, cal_end_ns, cal_scores, params: GateParams, budget: float = 2.0,
                q_grid=Q_GRID) -> dict:
    """Choose q on the calibrate weeks: scan q from 1e-2 down to 1e-6 and keep the
    largest q at which the gated rate, and the rate at every smaller q, stay at or
    under the budget (the empirical rule's pick_threshold, on SPOT thresholds).

    Returns q, the threshold, the rate it gives and the scan itself."""
    q_grid = np.sort(np.asarray(q_grid, dtype=float))[::-1]            # descending q = ascending threshold
    taus = np.array([threshold(fit, q) for q in q_grid])
    rates = np.array([_rate(cal_end_ns, cal_scores, tau, params) for tau in taus])
    over = np.flatnonzero(rates > budget)
    j = 0 if len(over) == 0 else min(over[-1] + 1, len(rates) - 1)
    return {"q": float(q_grid[j]), "threshold": float(taus[j]), "calibrate_rate": float(rates[j]),
            "within_budget": bool(rates[j] <= budget),
            "scan": [{"q": float(q), "threshold": float(tau), "rate": float(rate)}
                     for q, tau, rate in zip(q_grid, taus, rates)]}
