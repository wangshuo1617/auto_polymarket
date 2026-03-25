"""
Risk-based position sizing for 5m up/down strategy.

Computes a risk score [0, 1] from entry-time signals and adjusts
stake_usd proportionally: lower stakes on high-risk windows,
full (or larger) stakes on low-risk windows.

The goal: control average loss magnitude so the strategy remains
profitable even with occasional total-loss events (token → 0).
"""

from typing import NamedTuple


class RiskAssessment(NamedTuple):
    risk_score: float  # 0.0 (low risk) to 1.0 (high risk)
    risk_level: str  # "low", "medium", "high", "very_high"
    adjusted_stake: float  # USDC amount for this trade
    entry_price_risk: float
    direction_risk: float
    stability_risk: float


def compute_entry_price_risk(entry_price: float) -> float:
    """Entry price component of risk.

    Calibrated via walk-forward analysis (5d-train / 3d-test / 2d-step)
    on 443 real trades from 2026-03-07 to 2026-03-19.

    Consensus across all out-of-sample test windows:
      <0.45  : WR 75%, avgPnL -1.84, stable=Mixed   → risk 1.00
      0.45-0.60: WR 76%, avgPnL +1.20, stable=Yes(+) → risk 0.00  (best R/R)
      0.60-0.70: WR 57%, avgPnL -0.75, stable=Mixed  → risk 0.65
      0.70-0.80: WR 73%, avgPnL -0.22, stable=Mixed  → risk 0.45
      0.80-0.85: WR 87%, avgPnL +1.17, stable=Yes(+) → risk 0.00  (best zone)
      0.85-0.90: WR 79%, avgPnL -0.62, stable=Mixed  → risk 0.65
      0.90-0.95: WR 92%, avgPnL +0.21, stable=Mixed  → risk 0.15
      >=0.95 : WR 100%, avgPnL +0.30, stable=Yes(+) → risk 0.00  (boost)
    """
    if entry_price <= 0:
        return 1.0
    if entry_price < 0.45:
        return 1.0   # Too few trades, wildly unstable WR
    if entry_price < 0.60:
        return 0.0   # Best R/R zone: high avgPnL, stably positive
    if entry_price < 0.70:
        return 0.65  # Lowest WR (57%), net negative, mixed stability
    if entry_price < 0.80:
        return 0.45  # Marginal: WR 73% but slightly net negative
    if entry_price < 0.85:
        return 0.0   # Best absolute zone: WR 87%, stably positive
    if entry_price < 0.90:
        return 0.65  # Loss black-hole: worst net PnL despite 79% WR
    if entry_price < 0.95:
        return 0.15  # High WR (92%), small edge; slight reduction only
    return 0.0       # >= 0.95: 100% WR, stably positive → full boost


def compute_direction_risk(
    abs_btc_diff: float, min_direction_diff: float
) -> float:
    """Direction strength component: weaker BTC signal = higher risk.

    ratio = abs_diff / threshold:
    - ratio ≈ 1 (barely above threshold) → risk = 1.0
    - ratio >= 4 (very strong signal)    → risk = 0.0
    """
    if min_direction_diff <= 0:
        return 0.0
    ratio = abs_btc_diff / min_direction_diff
    if ratio <= 1.0:
        return 1.0
    if ratio >= 4.0:
        return 0.0
    return max(0.0, 1.0 - (ratio - 1.0) / 3.0)


def compute_stability_risk(
    btc_cross_count: int, max_btc_cross_count: int
) -> float:
    """Stability component: more BTC crossings over open = higher risk."""
    if max_btc_cross_count <= 1:
        return 0.0
    return min(1.0, btc_cross_count / max_btc_cross_count)


def compute_confidence_boost(entry_price: float) -> float:
    """Extra multiplier for highest-confidence entry prices.

    Applied *after* the normal risk sizing so that only the most
    historically reliable zones get above-base-stake allocation.
    Zones without 100 % WR in walk-forward data stay at 1.0 (no boost).
    """
    if entry_price >= 0.95:
        return 1.5   # 100 % WR across all OOS windows, boost 50 %
    return 1.0


def assess_risk(
    entry_price: float,
    abs_btc_diff: float,
    min_direction_diff: float,
    btc_cross_count: int,
    max_btc_cross_count: int,
    base_stake: float,
    min_stake_ratio: float = 0.15,
    max_stake_ratio: float = 1.0,
    confidence_boost_enabled: bool = True,
    w_price: float = 0.50,
    w_direction: float = 0.15,
    w_stability: float = 0.35,
) -> RiskAssessment:
    """Compute risk score and adjusted stake.

    Parameters
    ----------
    entry_price : best ask price at entry time
    abs_btc_diff : |BTC projected close - window open|
    min_direction_diff : threshold from strategy params
    btc_cross_count : times BTC crossed window open price
    max_btc_cross_count : configured maximum allowed crossings
    base_stake : nominal stake_usd from config
    min_stake_ratio : floor for adjusted stake (fraction of base)
    max_stake_ratio : ceiling for adjusted stake (fraction of base)
    confidence_boost_enabled : apply extra multiplier for >=0.95
    w_price / w_direction / w_stability : component weights

    Returns
    -------
    RiskAssessment with risk_score, risk_level, adjusted_stake,
    and per-component risk values.
    """
    price_risk = compute_entry_price_risk(entry_price)
    direction_risk = compute_direction_risk(abs_btc_diff, min_direction_diff)
    stability_risk = compute_stability_risk(btc_cross_count, max_btc_cross_count)

    risk_score = (
        w_price * price_risk
        + w_direction * direction_risk
        + w_stability * stability_risk
    )
    risk_score = max(0.0, min(1.0, risk_score))

    # Linear interpolation: low risk → max_stake_ratio, high risk → min_stake_ratio
    scale = max_stake_ratio - (max_stake_ratio - min_stake_ratio) * risk_score
    adjusted_stake = base_stake * scale

    # Targeted boost for highest-confidence zones (applied after risk scaling)
    if confidence_boost_enabled:
        adjusted_stake *= compute_confidence_boost(entry_price)

    adjusted_stake = max(
        base_stake * min_stake_ratio,
        min(base_stake * max_stake_ratio * compute_confidence_boost(entry_price)
            if confidence_boost_enabled else base_stake * max_stake_ratio,
            adjusted_stake),
    )

    if risk_score <= 0.25:
        level = "low"
    elif risk_score <= 0.45:
        level = "medium"
    elif risk_score <= 0.65:
        level = "high"
    else:
        level = "very_high"

    # Risk-level-based stake overrides
    if level == "very_high":
        adjusted_stake = 0.0
    elif level == "high":
        adjusted_stake = min(adjusted_stake, base_stake * 0.50)

    return RiskAssessment(
        risk_score=risk_score,
        risk_level=level,
        adjusted_stake=adjusted_stake,
        entry_price_risk=price_risk,
        direction_risk=direction_risk,
        stability_risk=stability_risk,
    )
