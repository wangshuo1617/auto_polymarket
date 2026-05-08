"""Monte Carlo path-integrated fair value (Phase B2 — shadow only).

Standalone MC barrier-touch simulator. Asymptotically equivalent to
`profit_optimizer._barrier_touch_prob`'s closed-form reflection-principle
formula, but path-based so that B3 AI scenarios (custom drift / vol regime
overlays) and B4 hit-rate analysis can plug in.

NEVER touched by production GBM advisory path. Output written to
`advisory_pathview_shadow_views.components.path_mc`.

env config:
  ADVISORY_MC_N_PATHS (default 5000)
  ADVISORY_MC_DT_DAYS (default 1.0; 1d step matches σ_daily scale)
  ADVISORY_MC_SEED (default unset → fresh RNG per call)
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Optional

import random


@dataclass
class MCResult:
    p_touch: float
    p_touch_se: float  # binomial std error
    n_paths: int
    dt_days: float
    note: str = ""


def _resolve_n_paths(override: Optional[int]) -> int:
    if override is not None:
        return max(100, int(override))
    raw = os.environ.get("ADVISORY_MC_N_PATHS", "5000")
    try:
        return max(100, int(raw))
    except (TypeError, ValueError):
        return 5000


def _resolve_dt(override: Optional[float]) -> float:
    if override is not None:
        return max(0.05, float(override))
    raw = os.environ.get("ADVISORY_MC_DT_DAYS", "1.0")
    try:
        return max(0.05, float(raw))
    except (TypeError, ValueError):
        return 1.0


def _resolve_rng(override_seed: Optional[int]) -> random.Random:
    if override_seed is not None:
        return random.Random(int(override_seed))
    seed = os.environ.get("ADVISORY_MC_SEED")
    if seed is not None and seed.strip():
        try:
            return random.Random(int(seed))
        except ValueError:
            pass
    return random.Random()


def _bridge_touch_prob(log_S0: float, log_S1: float, log_K: float,
                       sigma_step: float, is_above: bool) -> float:
    """Brownian-bridge prob a path touches barrier between two endpoints.
    P(max_{0..1} W_t >= a | W_0=0, W_1=b) = exp(-2·a·(a-b) / σ²) for a>max(0,b)."""
    if sigma_step <= 1e-12:
        return 0.0
    if is_above:
        if log_S0 >= log_K or log_S1 >= log_K:
            return 1.0
        a = log_K - log_S0
        b = log_S1 - log_S0
    else:
        if log_S0 <= log_K or log_S1 <= log_K:
            return 1.0
        a = log_S0 - log_K
        b = log_S0 - log_S1
    if a <= 0:
        return 1.0
    diff = a - b
    if diff <= 0:
        return 1.0
    return math.exp(-2.0 * a * diff / (sigma_step * sigma_step))


def _path_touch_score(zs, log_S0, log_K, mu_step, sigma_step, is_above) -> float:
    """Unbiased per-path estimator with Brownian-bridge correction."""
    log_S = log_S0
    p_no_touch = 1.0
    for z in zs:
        log_S_next = log_S + mu_step + sigma_step * z
        if (is_above and log_S_next >= log_K) or \
           (not is_above and log_S_next <= log_K):
            return 1.0
        p_bridge = _bridge_touch_prob(log_S, log_S_next, log_K, sigma_step, is_above)
        p_no_touch *= (1.0 - p_bridge)
        log_S = log_S_next
    return 1.0 - p_no_touch


def mc_barrier_touch(
    current_price: float,
    strike: float,
    direction: str,
    mu_daily: float,
    sigma_daily: float,
    days_left: float,
    *,
    n_paths: Optional[int] = None,
    dt_days: Optional[float] = None,
    seed: Optional[int] = None,
    fat_tail_mult: float = 1.0,
) -> MCResult:
    """Estimate P(barrier touched any time in (0, days_left]).

    Uses GBM with daily log-returns, antithetic variates for variance
    reduction. fat_tail_mult passed through (use 1.15 for realized,
    1.05 for IV) so caller can match closed-form baseline scaling.
    """
    n = _resolve_n_paths(n_paths)
    dt = _resolve_dt(dt_days)
    rng = _resolve_rng(seed)

    if days_left <= 0 or sigma_daily <= 1e-9:
        hit = 1.0 if (
            (direction == "above" and current_price >= strike)
            or (direction == "below" and current_price <= strike)
        ) else 0.0
        return MCResult(p_touch=hit, p_touch_se=0.0, n_paths=0,
                        dt_days=dt, note="degenerate_inputs")

    sigma_eff = sigma_daily * fat_tail_mult
    mu_log = mu_daily - 0.5 * sigma_eff ** 2

    n_steps = max(1, int(math.ceil(days_left / dt)))
    actual_T = n_steps * dt
    mu_step = mu_log * dt
    sigma_step = sigma_eff * math.sqrt(dt)

    log_S0 = math.log(current_price)
    log_K = math.log(strike)
    is_above = (direction == "above")

    n_pairs = n // 2
    score_sum = 0.0
    score_sq_sum = 0.0
    n_actual = 0

    def _accumulate(zs):
        nonlocal score_sum, score_sq_sum, n_actual
        s = _path_touch_score(zs, log_S0, log_K, mu_step, sigma_step, is_above)
        score_sum += s
        score_sq_sum += s * s
        n_actual += 1

    for _ in range(n_pairs):
        zs = [rng.gauss(0.0, 1.0) for _ in range(n_steps)]
        _accumulate(zs)
        _accumulate([-z for z in zs])

    if n % 2 == 1:
        _accumulate([rng.gauss(0.0, 1.0) for _ in range(n_steps)])

    p = score_sum / n_actual
    var = max(0.0, score_sq_sum / n_actual - p * p)
    se = math.sqrt(var / n_actual)

    return MCResult(
        p_touch=p,
        p_touch_se=se,
        n_paths=n_actual,
        dt_days=dt,
        note=f"steps={n_steps},T={actual_T:.4f}d,σ_eff={sigma_eff:.5f}",
    )
