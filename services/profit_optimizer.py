"""
面向 Polymarket 月度 BTC 事件的收益优化上下文构建。
仅提供分析输入，不直接下单。
"""
from __future__ import annotations

import json
import re
from math import erf, sqrt


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def _parse_usdc_balance(usdc_balance: str | float | int) -> float:
    if isinstance(usdc_balance, (int, float)):
        return max(0.0, float(usdc_balance))
    text = str(usdc_balance or "").replace("$", "").replace(",", "").strip()
    return max(0.0, _to_float(text))


def _ensure_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _parse_market_prices(market: dict) -> tuple[float | None, float | None]:
    outcomes = _ensure_list(market.get("outcomes"))
    prices = _ensure_list(market.get("outcomePrices"))
    if not outcomes or not prices or len(outcomes) != len(prices):
        return None, None

    yes_price = None
    no_price = None
    for name, raw in zip(outcomes, prices):
        p = _to_float(raw, default=-1.0)
        if p < 0:
            continue
        if p > 1.0:
            p = p / 100.0
        label = str(name).strip().lower()
        if label == "yes":
            yes_price = p
        elif label == "no":
            no_price = p

    if yes_price is None and no_price is not None:
        yes_price = max(0.0, min(1.0, 1.0 - no_price))
    if no_price is None and yes_price is not None:
        no_price = max(0.0, min(1.0, 1.0 - yes_price))
    return yes_price, no_price


def _extract_strike_and_direction(question: str) -> tuple[float | None, str]:
    q = str(question or "")
    numbers = re.findall(r"\$?\s*(\d{2,3}(?:,\d{3})+|\d{4,6})", q)
    strike = None
    if numbers:
        strike_text = numbers[0].replace(",", "")
        strike = _to_float(strike_text, default=0.0)
        if strike <= 0:
            strike = None

    ql = q.lower()
    above_keys = ["above", "over", "greater", "at least", "reach", "hit", "higher than"]
    below_keys = ["below", "under", "less", "at most", "lower than"]

    direction = "unknown"
    if any(k in ql for k in above_keys):
        direction = "above"
    if any(k in ql for k in below_keys):
        direction = "below"

    return strike, direction


def _build_scenario_probs(
    future_possibility_context: dict,
    daily_volatility_profile: dict,
) -> dict:
    drawdown = _to_float(future_possibility_context.get("drawdown_from_month_high_pct"), 0.0)
    days_left = int(_to_float(future_possibility_context.get("days_left_in_month"), 0.0))
    regime = str(daily_volatility_profile.get("market_regime") or "unknown")
    atr_pct = max(0.2, _to_float(daily_volatility_profile.get("atr_pct"), 1.5))

    p_down = 0.34
    p_reclaim = 0.33
    p_fast_rebound = 0.33

    if regime == "trend_down":
        p_down += 0.1
        p_fast_rebound -= 0.05
    elif regime == "trend_up":
        p_fast_rebound += 0.1
        p_down -= 0.05

    if drawdown <= -6.0 and days_left >= 10:
        p_reclaim += 0.08
        p_down -= 0.04
    if drawdown <= -10.0:
        p_fast_rebound += 0.04

    if atr_pct >= 3.0:
        p_fast_rebound += 0.04
        p_down += 0.02
        p_reclaim -= 0.06

    p_down = max(0.05, p_down)
    p_reclaim = max(0.05, p_reclaim)
    p_fast_rebound = max(0.05, p_fast_rebound)
    total = p_down + p_reclaim + p_fast_rebound

    return {
        "downtrend_continuation": round(p_down / total, 4),
        "range_then_reclaim": round(p_reclaim / total, 4),
        "fast_rebound": round(p_fast_rebound / total, 4),
    }


def build_profit_optimization_context(
    polymarket_event_situation: dict,
    future_possibility_context: dict,
    daily_volatility_profile: dict,
    usdc_balance: str | float | int,
) -> dict:
    """构建“期望收益最大化”所需的结构化先验和边际机会列表。"""
    current_price = _to_float(future_possibility_context.get("current_btc_price"), 0.0)
    days_left = max(1, int(_to_float(future_possibility_context.get("days_left_in_month"), 1.0)))

    scenario_probs = _build_scenario_probs(future_possibility_context, daily_volatility_profile)

    drift_daily = _to_float(future_possibility_context.get("drawdown_from_month_high_pct"), 0.0) / 100.0 / 14.0
    drift_daily = max(-0.01, min(0.01, -drift_daily * 0.25))

    sigma_daily = _to_float(daily_volatility_profile.get("realized_vol_daily_pct"), 0.0) / 100.0
    if sigma_daily <= 0:
        sigma_daily = max(0.008, _to_float(daily_volatility_profile.get("atr_pct"), 1.8) / 100.0 * 0.6)

    mu_ret = drift_daily * days_left
    sigma_ret = max(0.01, sigma_daily * sqrt(days_left))

    balance = _parse_usdc_balance(usdc_balance)
    risk_budget_ratio = 0.35
    total_risk_budget = balance * risk_budget_ratio

    edges = []
    markets = polymarket_event_situation.get("markets", []) if isinstance(polymarket_event_situation, dict) else []
    for market in markets:
        if not isinstance(market, dict):
            continue
        question = str(market.get("question") or "")
        strike, direction = _extract_strike_and_direction(question)
        yes_price, no_price = _parse_market_prices(market)

        if current_price <= 0 or strike is None or direction == "unknown" or yes_price is None or no_price is None:
            continue

        threshold_ret = strike / current_price - 1.0
        z = (threshold_ret - mu_ret) / sigma_ret
        p_above = max(0.001, min(0.999, 1.0 - _norm_cdf(z)))

        p_yes = p_above if direction == "above" else (1.0 - p_above)
        p_no = 1.0 - p_yes

        ev_yes = p_yes - yes_price
        ev_no = p_no - no_price

        if ev_yes >= ev_no:
            chosen_side = "Yes"
            chosen_price = yes_price
            chosen_prob = p_yes
            edge = ev_yes
        else:
            chosen_side = "No"
            chosen_price = no_price
            chosen_prob = p_no
            edge = ev_no

        if chosen_price >= 0.999:
            kelly = 0.0
        else:
            # Binary contract fractional Kelly approximation on paid-premium basis.
            kelly = max(0.0, (chosen_prob - chosen_price) / max(1e-6, 1.0 - chosen_price))

        fractional_kelly = 0.25 * kelly
        suggested_alloc = min(total_risk_budget * 0.4, balance * 0.2, balance * fractional_kelly)

        edges.append({
            "question": question,
            "direction_in_question": direction,
            "strike": round(strike, 2),
            "model_prob_yes": round(p_yes, 4),
            "implied_prob_yes": round(yes_price, 4),
            "edge_yes": round(ev_yes, 4),
            "edge_no": round(ev_no, 4),
            "best_side": chosen_side,
            "best_side_price": round(chosen_price, 4),
            "best_side_edge": round(edge, 4),
            "fractional_kelly": round(fractional_kelly, 4),
            "suggested_max_alloc_usdc": round(max(0.0, suggested_alloc), 2),
        })

    edges.sort(key=lambda x: x.get("best_side_edge", -1.0), reverse=True)

    top_edges = [x for x in edges if x.get("best_side_edge", 0.0) > 0.015][:8]

    return {
        "objective": "maximize_expected_value_under_risk_budget",
        "risk_budget": {
            "usdc_balance": round(balance, 2),
            "risk_budget_ratio": risk_budget_ratio,
            "total_risk_budget_usdc": round(total_risk_budget, 2),
            "single_market_cap_ratio": 0.2,
            "kelly_fraction": 0.25,
        },
        "scenario_probabilities": scenario_probs,
        "distribution_assumption": {
            "days_left": days_left,
            "mu_return": round(mu_ret, 4),
            "sigma_return": round(sigma_ret, 4),
            "current_btc_price": round(current_price, 2),
        },
        "top_edge_opportunities": top_edges,
        "all_edge_count": len(edges),
    }
