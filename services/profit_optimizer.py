"""
面向 Polymarket 月度价格预测事件的收益优化上下文构建。
支持 BTC / 原油 (oil) / 黄金 (gold) 等资产。仅提供分析输入，不直接下单。
"""
from __future__ import annotations

import json
import re
from math import erf, sqrt
from datetime import datetime
from zoneinfo import ZoneInfo

ET_TIMEZONE = ZoneInfo("America/New_York")


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
    # Match dollar amounts: $80,000  $3,200  $75  $3200.50
    dollar_amounts = re.findall(r"\$\s*([\d,]+(?:\.\d+)?)", q)
    strike = None
    if dollar_amounts:
        strike_text = dollar_amounts[0].replace(",", "")
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
    tr_percentile = _to_float(daily_volatility_profile.get("tr_percentile_30d"), 50.0)

    p_down = 0.34
    p_reclaim = 0.33
    p_fast_rebound = 0.33

    # Regime adjustments (larger swings than before)
    if regime == "trend_down":
        p_down += 0.15
        p_fast_rebound -= 0.10
        p_reclaim -= 0.05
    elif regime == "trend_up":
        p_fast_rebound += 0.15
        p_down -= 0.10
        p_reclaim -= 0.05

    # Drawdown-based adjustments
    if drawdown <= -6.0 and days_left >= 10:
        p_reclaim += 0.10
        p_down -= 0.05
    if drawdown <= -10.0:
        p_fast_rebound += 0.06
        p_down -= 0.03

    # Volatility adjustments
    if atr_pct >= 3.0:
        p_fast_rebound += 0.05
        p_down += 0.03
        p_reclaim -= 0.08

    # TIME COMPRESSION: fewer days = less room for extreme moves
    if days_left <= 3:
        # Very little time: heavy bias toward range/status quo
        p_reclaim += 0.20
        p_down -= 0.10
        p_fast_rebound -= 0.10
    elif days_left <= 7:
        p_reclaim += 0.12
        p_down -= 0.06
        p_fast_rebound -= 0.06
    elif days_left <= 14:
        p_reclaim += 0.05
        p_down -= 0.02
        p_fast_rebound -= 0.03

    # Low volatility compression: if TR percentile very low, extreme moves less likely
    if tr_percentile <= 15:
        p_reclaim += 0.08
        p_down -= 0.04
        p_fast_rebound -= 0.04
    elif tr_percentile <= 30:
        p_reclaim += 0.04
        p_down -= 0.02
        p_fast_rebound -= 0.02

    p_down = max(0.05, p_down)
    p_reclaim = max(0.05, p_reclaim)
    p_fast_rebound = max(0.05, p_fast_rebound)
    total = p_down + p_reclaim + p_fast_rebound

    return {
        "downtrend_continuation": round(p_down / total, 4),
        "range_then_reclaim": round(p_reclaim / total, 4),
        "fast_rebound": round(p_fast_rebound / total, 4),
        "time_compression_note": (
            f"剩余{days_left}天，TR分位{tr_percentile}%"
            + ("，时间不足已大幅压缩极端路径概率" if days_left <= 7 else "")
        ),
    }


def _calculate_portfolio_value(positions: list, usdc_balance: float) -> dict:
    """计算总账户净值 = USDC余额 + 所有持仓市值。"""
    position_value = 0.0
    position_details = []
    for p in positions:
        if not isinstance(p, dict):
            continue
        try:
            size = float(p.get("size") or 0)
            cur_price = float(p.get("curPrice") or 0)
            avg_price = float(p.get("avgPrice") or 0)
            market_value = size * cur_price
            cost_basis = size * avg_price
            position_value += market_value
            position_details.append({
                "title": p.get("title", ""),
                "outcome": p.get("outcome", ""),
                "size": size,
                "avg_price": round(avg_price, 4),
                "cur_price": round(cur_price, 4),
                "market_value": round(market_value, 2),
                "cost_basis": round(cost_basis, 2),
                "unrealized_pnl": round(market_value - cost_basis, 2),
            })
        except (TypeError, ValueError):
            continue

    total_net_value = usdc_balance + position_value
    return {
        "usdc_balance": round(usdc_balance, 2),
        "total_position_value": round(position_value, 2),
        "total_net_value": round(total_net_value, 2),
        "cash_ratio": round(usdc_balance / total_net_value, 4) if total_net_value > 0 else 0.0,
        "position_details": position_details,
    }


def _build_position_safety_assessment(
    positions: list,
    future_possibility_context: dict,
    daily_volatility_profile: dict,
    asset: str = "btc",
) -> list:
    """对每个持仓评估安全度: safe_to_hold / monitor / at_risk。"""
    current_price = _get_current_price(future_possibility_context, asset)
    days_left = max(0, int(_to_float(future_possibility_context.get("days_left_in_month"), 0)))
    atr_pct = _to_float(daily_volatility_profile.get("atr_pct"), 2.0)

    assessments = []
    for p in positions:
        if not isinstance(p, dict):
            continue
        title = str(p.get("title") or "")
        outcome = str(p.get("outcome") or "").lower()
        size = _to_float(p.get("size"), 0.0)
        cur_price_contract = _to_float(p.get("curPrice"), 0.0)

        strike, direction = _extract_strike_and_direction(title)
        if strike is None or current_price <= 0:
            assessments.append({
                "title": title, "safety_level": "unknown",
                "reason": "无法解析 strike price",
            })
            continue

        safety_margin_pct = abs(strike - current_price) / current_price * 100.0
        atr_distance = safety_margin_pct / max(atr_pct, 0.1)
        max_expected_move_pct = atr_pct * sqrt(max(days_left, 1))

        is_no = outcome == "no"
        if is_no:
            if direction == "above":
                buffer_favorable = (strike - current_price) / current_price * 100.0
            else:
                buffer_favorable = (current_price - strike) / current_price * 100.0
        else:
            buffer_favorable = -safety_margin_pct

        near_atr_warning = atr_distance < 1.0

        if buffer_favorable > 0 and buffer_favorable > max_expected_move_pct * 1.5 and days_left <= 10:
            safety_level = "safe_to_hold"
            reason = f"安全垫{buffer_favorable:.1f}%远超剩余{days_left}天最大预期波动{max_expected_move_pct:.1f}%，持有到期胜率极高"
        elif buffer_favorable > 0 and buffer_favorable > max_expected_move_pct * 0.8:
            safety_level = "monitor"
            reason = f"安全垫{buffer_favorable:.1f}%尚可，与预期波动{max_expected_move_pct:.1f}%接近，需持续监控"
        elif buffer_favorable > 0:
            safety_level = "at_risk"
            reason = f"安全垫{buffer_favorable:.1f}%不足，低于预期波动{max_expected_move_pct:.1f}%，存在被击穿风险"
        else:
            safety_level = "at_risk"
            reason = f"当前处于不利方向（缓冲{buffer_favorable:.1f}%），需紧密关注"

        if near_atr_warning:
            reason = (
                f"{reason}；⚠ 距离关键价仅 {atr_distance:.2f} ATR（<1 ATR），"
                "属于易触发区，建议提高监控频率并准备应急减仓/对冲。"
            )

        hold_to_expiry_return_pct = None
        if cur_price_contract > 0 and cur_price_contract < 1.0:
            hold_to_expiry_return_pct = round((1.0 - cur_price_contract) / cur_price_contract * 100.0, 2)

        assessments.append({
            "title": title,
            "outcome": p.get("outcome", ""),
            "size": size,
            "cur_price": cur_price_contract,
            "strike": strike,
            "direction": direction,
            "safety_margin_pct": round(safety_margin_pct, 2),
            "atr_distance": round(atr_distance, 2),
            "within_one_atr_warning": near_atr_warning,
            "buffer_favorable_pct": round(buffer_favorable, 2),
            "max_expected_move_pct": round(max_expected_move_pct, 2),
            "days_left": days_left,
            "safety_level": safety_level,
            "reason": reason,
            "hold_to_expiry_return_pct": hold_to_expiry_return_pct,
        })

    return assessments


def _build_theta_income(position_assessments: list, days_left: int) -> list:
    """计算每个持仓的 Theta 日收益。"""
    theta_data = []
    for pa in position_assessments:
        size = _to_float(pa.get("size"), 0.0)
        cur_price = _to_float(pa.get("cur_price"), 0.0)
        safety_level = pa.get("safety_level", "unknown")

        if size <= 0 or cur_price <= 0 or cur_price >= 1.0:
            continue

        theta_to_expiry = size * (1.0 - cur_price)
        daily_theta = theta_to_expiry / max(days_left, 1)

        theta_data.append({
            "title": pa.get("title", ""),
            "outcome": pa.get("outcome", ""),
            "size": size,
            "cur_price": cur_price,
            "theta_to_expiry_usdc": round(theta_to_expiry, 2),
            "daily_theta_income_usdc": round(daily_theta, 2),
            "safety_level": safety_level,
            "hold_to_expiry_return_pct": pa.get("hold_to_expiry_return_pct"),
        })

    theta_data.sort(key=lambda x: x.get("daily_theta_income_usdc", 0), reverse=True)
    total_daily = sum(t["daily_theta_income_usdc"] for t in theta_data)
    total_to_expiry = sum(t["theta_to_expiry_usdc"] for t in theta_data)
    return {
        "positions": theta_data,
        "total_daily_theta_usdc": round(total_daily, 2),
        "total_theta_to_expiry_usdc": round(total_to_expiry, 2),
    }


def _build_rotation_opportunities(
    position_assessments: list,
    edges: list,
) -> list:
    """识别轮动机会：从低收益率持仓转向高收益率未建仓标的。"""
    held_questions = set()
    for pa in position_assessments:
        strike = pa.get("strike")
        direction = pa.get("direction", "")
        if strike:
            held_questions.add((strike, direction))

    held_yields = []
    for pa in position_assessments:
        cur_price = _to_float(pa.get("cur_price"), 0.0)
        if cur_price > 0 and cur_price < 1.0:
            yield_pct = (1.0 - cur_price) / cur_price * 100.0
        else:
            yield_pct = 0.0
        held_yields.append({
            "title": pa.get("title", ""),
            "outcome": pa.get("outcome", ""),
            "cur_price": cur_price,
            "yield_to_expiry_pct": round(yield_pct, 2),
            "safety_level": pa.get("safety_level", "unknown"),
            "safety_margin_pct": pa.get("safety_margin_pct", 0),
            "buffer_favorable_pct": pa.get("buffer_favorable_pct", 0),
            "size": pa.get("size", 0),
            "market_value": round(_to_float(pa.get("size"), 0) * cur_price, 2),
        })

    rotation_opps = []
    for edge in edges:
        question = edge.get("question", "")
        best_side = edge.get("best_side", "")
        best_price = _to_float(edge.get("best_side_price"), 0.0)
        best_edge = _to_float(edge.get("best_side_edge"), 0.0)
        strike = _to_float(edge.get("strike"), 0.0)
        direction = edge.get("direction_in_question", "")

        if (strike, direction) in held_questions:
            continue
        if best_price <= 0 or best_price >= 1.0:
            continue

        target_yield_pct = (1.0 - best_price) / best_price * 100.0

        for hy in held_yields:
            if hy["yield_to_expiry_pct"] <= 0:
                continue
            yield_improvement = target_yield_pct - hy["yield_to_expiry_pct"]
            if yield_improvement > 3.0 and best_edge > 0.01:
                rotation_opps.append({
                    "from_position": hy["title"],
                    "from_outcome": hy["outcome"],
                    "from_yield_pct": hy["yield_to_expiry_pct"],
                    "from_safety": hy["safety_level"],
                    "from_market_value": hy["market_value"],
                    "to_market": question,
                    "to_side": best_side,
                    "to_price": round(best_price, 4),
                    "to_yield_pct": round(target_yield_pct, 2),
                    "to_edge": round(best_edge, 4),
                    "yield_improvement_pct": round(yield_improvement, 2),
                    "rotation_rationale": (
                        f"卖出{hy['title']}(收益率{hy['yield_to_expiry_pct']:.1f}%)"
                        f"转投{question}(收益率{target_yield_pct:.1f}%)，"
                        f"收益率提升{yield_improvement:.1f}个百分点"
                    ),
                })

    rotation_opps.sort(key=lambda x: x.get("yield_improvement_pct", 0), reverse=True)
    return rotation_opps[:5]


def _build_portfolio_analysis(
    position_assessments: list,
    current_asset_price: float,
    usdc_balance: float,
    asset: str = "btc",
) -> dict:
    """组合级关联风险分析：情景矩阵、对冲结构识别。"""
    if current_asset_price <= 0:
        return {"note": f"{asset}价格无效，无法计算组合分析"}

    total_position_value = sum(
        _to_float(pa.get("size"), 0) * _to_float(pa.get("cur_price"), 0)
        for pa in position_assessments
    )
    total_net_value = usdc_balance + total_position_value

    scenarios = {}
    for move_pct in [-10, -5, -3, 3, 5, 10]:
        scenario_asset = current_asset_price * (1.0 + move_pct / 100.0)
        scenario_pnl = 0.0

        for pa in position_assessments:
            strike = _to_float(pa.get("strike"), 0.0)
            direction = pa.get("direction", "unknown")
            outcome = str(pa.get("outcome", "")).lower()
            size = _to_float(pa.get("size"), 0.0)
            cur_price = _to_float(pa.get("cur_price"), 0.0)

            if strike <= 0 or size <= 0:
                continue

            is_no = outcome == "no"

            if direction == "above":
                if scenario_asset >= strike:
                    new_price = 0.01 if is_no else 0.99
                else:
                    dist_now = max(strike - current_asset_price, 1.0)
                    dist_new = max(strike - scenario_asset, 0.0)
                    ratio = min(dist_new / dist_now, 2.0)
                    if is_no:
                        new_price = min(0.99, cur_price + (1.0 - cur_price) * max(0, 1.0 - 1.0 / max(ratio, 0.01)) * 0.4)
                    else:
                        new_price = max(0.01, cur_price * ratio)
            elif direction == "below":
                if scenario_asset <= strike:
                    new_price = 0.01 if is_no else 0.99
                else:
                    dist_now = max(current_asset_price - strike, 1.0)
                    dist_new = max(scenario_asset - strike, 0.0)
                    ratio = min(dist_new / dist_now, 2.0)
                    if is_no:
                        new_price = min(0.99, cur_price + (1.0 - cur_price) * max(0, 1.0 - 1.0 / max(ratio, 0.01)) * 0.4)
                    else:
                        new_price = max(0.01, cur_price * ratio)
            else:
                new_price = cur_price

            scenario_pnl += size * (new_price - cur_price)

        scenarios[f"{asset}_{move_pct:+d}pct"] = {
            f"{asset}_price": round(scenario_asset, 2),
            "portfolio_pnl_usdc": round(scenario_pnl, 2),
            "portfolio_pnl_pct": round(scenario_pnl / total_net_value * 100.0, 2) if total_net_value > 0 else 0.0,
        }

    has_upside_no = any(
        pa.get("direction") == "above" and str(pa.get("outcome", "")).lower() == "no"
        for pa in position_assessments
    )
    has_downside_no = any(
        pa.get("direction") == "below" and str(pa.get("outcome", "")).lower() == "no"
        for pa in position_assessments
    )

    if has_upside_no and has_downside_no:
        structure = "short_strangle"
        structure_note = f"持有上下两方向 No 仓位（类 Short Strangle），{asset.upper()} 区间震荡时两端均盈利，具有天然对冲效果"
    elif has_upside_no:
        structure = "bearish_range"
        structure_note = "仅持有上方 No 仓位，看空或看区间震荡"
    elif has_downside_no:
        structure = "bullish_range"
        structure_note = "仅持有下方 No 仓位，看多或看区间震荡"
    else:
        structure = "mixed"
        structure_note = "混合持仓结构"

    return {
        "total_net_value": round(total_net_value, 2),
        "portfolio_structure": structure,
        "portfolio_structure_note": structure_note,
        "scenario_analysis": scenarios,
    }


def _build_prediction_review(
    previous_report: dict | None,
    current_asset_price: float,
    asset: str = "btc",
) -> dict | None:
    """对比上期报告预测 vs 实际走势，生成回顾。"""
    if not previous_report or not isinstance(previous_report, dict):
        return None

    asset_label = asset.upper()
    review: dict = {"has_previous": True, "findings": []}

    overall = previous_report.get("整体分析", "")
    if overall:
        review["previous_overall_summary"] = overall[:300] + "..." if len(overall) > 300 else overall

    warnings = previous_report.get("预警信号", [])
    if warnings and current_asset_price > 0:
        for w in warnings:
            direction = w.get("预警方向", "")
            price = _to_float(w.get("价格"), 0.0)
            if price <= 0:
                continue
            if direction == "up_to":
                triggered = current_asset_price >= price
            elif direction == "down_to":
                triggered = current_asset_price <= price
            else:
                continue
            review["findings"].append({
                "type": "warning_check",
                "warning": f"{'上行' if direction == 'up_to' else '下行'}预警 ${price:,.0f}",
                "triggered": triggered,
                f"actual_{asset}": round(current_asset_price, 2),
                "note": f"{asset_label} {'已触及' if triggered else '未触及'}该预警位（当前${current_asset_price:,.0f}）",
            })

    appendix = previous_report.get("报告解读附录", [])
    immediate_actions = []
    for item in appendix:
        if item.get("执行优先级") == "立即执行":
            conclusion = item.get("一句话结论", "")
            if conclusion:
                immediate_actions.append(conclusion)
    if immediate_actions:
        review["previous_immediate_actions"] = immediate_actions
        review["findings"].append({
            "type": "action_review",
            "note": (
                f"上期有{len(immediate_actions)}条'立即执行'建议。"
                "请评估这些建议在当前市场下是否仍合理，若用户未执行需分析可能原因并调整策略。"
            ),
        })

    review["anti_repetition_reminder"] = (
        "若本期判断与上期相同，必须简述新的支撑证据而非完整重复。"
        "若上期建议未被执行，需分析原因（流动性不足？价格不合理？用户判断不同？）并给出调整方案。"
    )

    return review


def _get_current_price(future_possibility_context: dict, asset: str = "btc") -> float:
    """从 future_possibility_context 中提取当前资产价格。"""
    key_map = {
        "btc": "current_btc_price",
        "oil": "current_oil_price",
        "gold": "current_gold_price",
    }
    key = key_map.get(asset, f"current_{asset}_price")
    price = _to_float(future_possibility_context.get(key), 0.0)
    if price <= 0:
        price = _to_float(future_possibility_context.get("current_price"), 0.0)
    return price


def build_profit_optimization_context(
    polymarket_event_situation: dict,
    future_possibility_context: dict,
    daily_volatility_profile: dict,
    usdc_balance: str | float | int,
    positions: list | None = None,
    previous_report: dict | None = None,
    asset: str = "btc",
) -> dict:
    """构建"期望收益最大化"所需的结构化先验、安全度评估、轮动机会和组合分析。"""
    asset = asset.strip().lower()

    scenario_probs = _build_scenario_probs(future_possibility_context, daily_volatility_profile)

    drift_daily = _to_float(future_possibility_context.get("drawdown_from_month_high_pct"), 0.0) / 100.0 / 14.0
    drift_daily = max(-0.01, min(0.01, -drift_daily * 0.25))

    sigma_daily = _to_float(daily_volatility_profile.get("realized_vol_daily_pct"), 0.0) / 100.0
    if sigma_daily <= 0:
        sigma_daily = max(0.008, _to_float(daily_volatility_profile.get("atr_pct"), 1.8) / 100.0 * 0.6)

    days_left = max(0, int(_to_float(future_possibility_context.get("days_left_in_month"), 0)))
    current_price = _get_current_price(future_possibility_context, asset)

    mu_ret = drift_daily * days_left
    sigma_ret = max(0.01, sigma_daily * sqrt(days_left))

    balance = _parse_usdc_balance(usdc_balance)

    # --- Portfolio value & risk budget based on TOTAL NET VALUE ---
    positions = positions or []
    portfolio = _calculate_portfolio_value(positions, balance)
    total_net_value = portfolio["total_net_value"]
    risk_budget_ratio = 0.35
    total_risk_budget = total_net_value * risk_budget_ratio

    # --- Position safety assessment ---
    position_assessments = _build_position_safety_assessment(
        positions, future_possibility_context, daily_volatility_profile, asset=asset,
    )

    # --- Theta daily income ---
    theta_income = _build_theta_income(position_assessments, days_left)

    # --- Edge calculation ---
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
            kelly = max(0.0, (chosen_prob - chosen_price) / max(1e-6, 1.0 - chosen_price))

        fractional_kelly = 0.25 * kelly
        suggested_alloc = min(
            total_risk_budget * 0.4,
            total_net_value * 0.2,
            total_net_value * fractional_kelly,
        )

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

    # --- Rotation opportunities ---
    rotation_opportunities = _build_rotation_opportunities(position_assessments, edges)

    # --- Portfolio-level analysis ---
    portfolio_analysis = _build_portfolio_analysis(
        position_assessments, current_price, balance, asset=asset,
    )

    # --- Prediction review ---
    prediction_review = _build_prediction_review(previous_report, current_price, asset=asset)

    return {
        "objective": "maximize_expected_profit_under_risk_budget",
        "portfolio_summary": portfolio,
        "risk_budget": {
            "basis": "total_net_value",
            "total_net_value": round(total_net_value, 2),
            "usdc_balance": round(balance, 2),
            "risk_budget_ratio": risk_budget_ratio,
            "total_risk_budget_usdc": round(total_risk_budget, 2),
            "single_market_cap_ratio": 0.2,
            "kelly_fraction": 0.25,
        },
        "scenario_probabilities": scenario_probs,
        "distribution_assumption": {
            "asset": asset,
            "days_left": days_left,
            "mu_return": round(mu_ret, 4),
            "sigma_return": round(sigma_ret, 4),
            "current_price": round(current_price, 2),
        },
        "position_safety_assessment": position_assessments,
        "theta_income": theta_income,
        "portfolio_analysis": portfolio_analysis,
        "rotation_opportunities": rotation_opportunities,
        "prediction_review": prediction_review,
        "top_edge_opportunities": top_edges,
        "all_edge_count": len(edges),
    }
