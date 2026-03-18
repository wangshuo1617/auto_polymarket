"""
面向 Polymarket 月度 BTC 事件的收益优化上下文构建。
仅提供分析输入，不直接下单。
"""
from __future__ import annotations

import json
import re
from math import erf, sqrt
from pathlib import Path


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def _clamp(value: float, min_v: float, max_v: float) -> float:
    return max(min_v, min(max_v, value))


def _load_realtime_effective_params() -> dict:
    """加载实时参数更新器输出的 effective_values（不存在则回退为空）。"""
    params_path = Path(__file__).resolve().parents[1] / "output" / "realtime_params_effective.json"
    if not params_path.exists():
        return {}
    try:
        with params_path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(payload, dict):
        return {}
    effective_values = payload.get("effective_values")
    return effective_values if isinstance(effective_values, dict) else {}


def _load_minimal_market_model(model_name: str = "minimal_market_model") -> dict:
    """加载最小模型（概率校准 + 成本模型），不存在则回退为空。"""
    model_path = Path(__file__).resolve().parents[1] / "models" / f"{model_name}.json"
    if not model_path.exists():
        return {}
    try:
        with model_path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _param_value(
    param_overrides: dict,
    key: str,
    default: float,
    min_v: float,
    max_v: float,
) -> float:
    raw = param_overrides.get(key, default)
    return _clamp(_to_float(raw, default), min_v, max_v)


def _normalize_asset(asset: str | None) -> str:
    normalized = str(asset or "btc").strip().lower()
    return "oil" if normalized == "oil" else "btc"


def _calibrate_probability(prob: float, model_payload: dict | None) -> tuple[float, bool]:
    """使用分箱校准表对概率进行校准；失败时返回原始概率。"""
    p = _clamp(_to_float(prob, 0.5), 0.001, 0.999)
    if not model_payload:
        return p, False
    calib = model_payload.get("probability_calibration")
    if not isinstance(calib, dict):
        return p, False
    bins = calib.get("bins")
    if not isinstance(bins, list) or not bins:
        return p, False

    for b in bins:
        if not isinstance(b, dict):
            continue
        lo = _to_float(b.get("lo"), None)
        hi = _to_float(b.get("hi"), None)
        calibrated = _to_float(b.get("calibrated_prob"), None)
        if lo is None or hi is None or calibrated is None:
            continue
        if (p >= lo and p < hi) or (p == 0.999 and hi >= 0.999):
            return _clamp(calibrated, 0.001, 0.999), True
    return p, False


def _estimate_cost_prob_with_model(
    *,
    sigma_daily_pct: float,
    default_cost_prob: float,
    model_payload: dict | None,
) -> tuple[float, bool]:
    """
    使用最小模型的线性成本模型估计 cost_prob。
    若模型不可用则回退 default_cost_prob。
    """
    fallback = _clamp(default_cost_prob, 0.0, 0.2)
    if not model_payload:
        return fallback, False
    cost_model = model_payload.get("cost_model")
    if not isinstance(cost_model, dict):
        return fallback, False
    intercept = _to_float(cost_model.get("intercept"), None)
    beta_sigma = _to_float(cost_model.get("beta_sigma"), None)
    if intercept is None or beta_sigma is None:
        return fallback, False
    est = intercept + beta_sigma * max(0.0, sigma_daily_pct)
    return _clamp(est, 0.0, 0.2), True


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
    param_overrides: dict | None = None,
) -> dict:
    param_overrides = param_overrides or {}
    drawdown = _to_float(future_possibility_context.get("drawdown_from_month_high_pct"), 0.0)
    days_left = int(_to_float(future_possibility_context.get("days_left_in_month"), 0.0))
    regime = str(daily_volatility_profile.get("market_regime") or "unknown")
    atr_pct = max(0.2, _to_float(daily_volatility_profile.get("atr_pct"), 1.5))

    p_down = _param_value(param_overrides, "scenario_weight_downtrend", 0.34, 0.05, 0.8)
    p_reclaim = _param_value(param_overrides, "scenario_weight_reclaim", 0.33, 0.05, 0.8)
    p_fast_rebound = _param_value(param_overrides, "scenario_weight_fast_rebound", 0.33, 0.05, 0.8)
    drawdown_retest_threshold = _param_value(
        param_overrides, "drawdown_retest_threshold_pct", -6.0, -20.0, -1.0
    )
    drawdown_fast_rebound_threshold = _param_value(
        param_overrides, "drawdown_high_vol_threshold_pct", -10.0, -30.0, -2.0
    )

    if regime == "trend_down":
        p_down += 0.1
        p_fast_rebound -= 0.05
    elif regime == "trend_up":
        p_fast_rebound += 0.1
        p_down -= 0.05

    if drawdown <= drawdown_retest_threshold and days_left >= 10:
        p_reclaim += 0.08
        p_down -= 0.04
    if drawdown <= drawdown_fast_rebound_threshold:
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
    asset: str = "btc",
) -> dict:
    """构建“期望收益最大化”所需的结构化先验和边际机会列表。"""
    param_overrides = _load_realtime_effective_params()
    asset_name = _normalize_asset(asset)
    model_name = "minimal_market_model_oil" if asset_name == "oil" else "minimal_market_model"
    minimal_model = _load_minimal_market_model(model_name=model_name)
    current_price = _to_float(future_possibility_context.get("current_btc_price"), 0.0)
    days_left = max(1, int(_to_float(future_possibility_context.get("days_left_in_month"), 1.0)))

    scenario_probs = _build_scenario_probs(
        future_possibility_context,
        daily_volatility_profile,
        param_overrides=param_overrides,
    )

    drift_daily = _to_float(future_possibility_context.get("drawdown_from_month_high_pct"), 0.0) / 100.0 / 14.0
    drift_daily = max(-0.01, min(0.01, -drift_daily * 0.25))

    sigma_daily = _to_float(daily_volatility_profile.get("realized_vol_daily_pct"), 0.0) / 100.0
    sigma_daily_override = _param_value(param_overrides, "sigma_daily_pct", 0.0, 0.0, 20.0) / 100.0
    if sigma_daily_override > 0:
        sigma_daily = sigma_daily_override
    if sigma_daily <= 0:
        atr_pct_proxy = _param_value(
            param_overrides,
            "atr_pct_proxy",
            _to_float(daily_volatility_profile.get("atr_pct"), 1.8),
            0.2,
            20.0,
        )
        sigma_daily = max(0.008, atr_pct_proxy / 100.0 * 0.6)

    mu_ret = drift_daily * days_left
    sigma_ret = max(0.01, sigma_daily * sqrt(days_left))

    balance = _parse_usdc_balance(usdc_balance)
    risk_budget_ratio = _param_value(param_overrides, "risk_budget_ratio", 0.35, 0.05, 0.5)
    single_market_cap_ratio = _param_value(param_overrides, "single_market_cap_ratio", 0.2, 0.05, 0.35)
    kelly_fraction = _param_value(param_overrides, "kelly_fraction", 0.25, 0.05, 0.5)
    edge_entry_threshold = _param_value(param_overrides, "edge_entry_threshold", 0.015, 0.002, 0.06)
    fee_bps = _param_value(param_overrides, "fee_bps", 0.0, 0.0, 1000.0)
    slippage_bps = _param_value(param_overrides, "slippage_bps", 0.0, 0.0, 1000.0)
    impact_bps = _param_value(param_overrides, "impact_bps", 0.0, 0.0, 1000.0)
    total_cost_prob_default = (fee_bps + slippage_bps + impact_bps) / 10000.0
    total_cost_prob, cost_model_applied = _estimate_cost_prob_with_model(
        sigma_daily_pct=sigma_daily * 100.0,
        default_cost_prob=total_cost_prob_default,
        model_payload=minimal_model,
    )
    total_risk_budget = balance * risk_budget_ratio
    calibration_applied_count = 0

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

        p_yes_raw = p_above if direction == "above" else (1.0 - p_above)
        p_yes, calibrated_used = _calibrate_probability(p_yes_raw, minimal_model)
        if calibrated_used:
            calibration_applied_count += 1
        p_no = 1.0 - p_yes

        ev_yes = p_yes - yes_price - total_cost_prob
        ev_no = p_no - no_price - total_cost_prob

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

        effective_price = min(0.999, chosen_price + total_cost_prob)
        if effective_price >= 0.999:
            kelly = 0.0
        else:
            # Binary contract fractional Kelly approximation on paid-premium basis.
            kelly = max(0.0, (chosen_prob - effective_price) / max(1e-6, 1.0 - effective_price))

        fractional_kelly = kelly_fraction * kelly
        suggested_alloc = min(
            total_risk_budget * 0.4,
            balance * single_market_cap_ratio,
            balance * fractional_kelly,
        )

        edges.append({
            "question": question,
            "direction_in_question": direction,
            "strike": round(strike, 2),
            "model_prob_yes_raw": round(p_yes_raw, 4),
            "model_prob_yes": round(p_yes, 4),
            "implied_prob_yes": round(yes_price, 4),
            "execution_cost_prob": round(total_cost_prob, 4),
            "edge_yes": round(ev_yes, 4),
            "edge_no": round(ev_no, 4),
            "best_side": chosen_side,
            "best_side_price": round(chosen_price, 4),
            "best_side_edge": round(edge, 4),
            "fractional_kelly": round(fractional_kelly, 4),
            "suggested_max_alloc_usdc": round(max(0.0, suggested_alloc), 2),
        })

    edges.sort(key=lambda x: x.get("best_side_edge", -1.0), reverse=True)

    top_edges = [x for x in edges if x.get("best_side_edge", 0.0) > edge_entry_threshold][:8]

    return {
        "objective": "maximize_expected_value_under_risk_budget",
        "risk_budget": {
            "usdc_balance": round(balance, 2),
            "risk_budget_ratio": risk_budget_ratio,
            "total_risk_budget_usdc": round(total_risk_budget, 2),
            "single_market_cap_ratio": single_market_cap_ratio,
            "kelly_fraction": kelly_fraction,
        },
        "execution_costs": {
            "fee_bps": round(fee_bps, 4),
            "slippage_bps": round(slippage_bps, 4),
            "impact_bps": round(impact_bps, 4),
            "total_cost_prob_default": round(total_cost_prob_default, 4),
            "total_cost_prob": round(total_cost_prob, 4),
        },
        "model_adjustments": {
            "asset": asset_name,
            "model_name": model_name,
            "minimal_model_loaded": bool(minimal_model),
            "probability_calibration_applied_count": calibration_applied_count,
            "cost_model_applied": cost_model_applied,
            "cost_model_sample_count": int(
                _to_float((minimal_model.get("cost_model") if isinstance(minimal_model, dict) else {}).get("sample_count"), 0)
            ),
            "probability_label_sample_count": int(
                _to_float(
                    (minimal_model.get("probability_calibration") if isinstance(minimal_model, dict) else {}).get(
                        "sample_count"
                    ),
                    0,
                )
            ),
        },
        "entry_thresholds": {
            "edge_entry_threshold": edge_entry_threshold,
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
