"""
日线波动率与自适应离场参数计算。
"""
from __future__ import annotations

from math import sqrt


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _percentile_rank(values: list[float], target: float) -> float:
    """返回 target 在 values 中的百分位(0-100)。"""
    if not values:
        return 0.0
    sorted_values = sorted(values)
    le_count = sum(1 for v in sorted_values if v <= target)
    return round((le_count / len(sorted_values)) * 100.0, 2)


def build_daily_volatility_profile(btc_1d_k_data: list, atr_period: int = 14) -> dict:
    """
    基于 Binance 1d K 线计算 ATR、波动分位和自适应止盈止损模板。
    输入 K 线格式: [open_time, open, high, low, close, ...]
    """
    if not btc_1d_k_data or len(btc_1d_k_data) < 2:
        return {
            "market_regime": "unknown",
            "atr": None,
            "atr_pct": None,
            "tr_percentile_30d": None,
            "adaptive_exit_template": None,
            "note": "1d K线样本不足，无法计算ATR与波动分位",
        }

    highs = [_to_float(k[2]) for k in btc_1d_k_data if len(k) > 4]
    lows = [_to_float(k[3]) for k in btc_1d_k_data if len(k) > 4]
    closes = [_to_float(k[4]) for k in btc_1d_k_data if len(k) > 4]

    n = min(len(highs), len(lows), len(closes))
    highs = highs[-n:]
    lows = lows[-n:]
    closes = closes[-n:]

    if n < 2:
        return {
            "market_regime": "unknown",
            "atr": None,
            "atr_pct": None,
            "tr_percentile_30d": None,
            "adaptive_exit_template": None,
            "note": "1d K线样本不足，无法计算ATR与波动分位",
        }

    tr_values: list[float] = []
    tr_pct_values: list[float] = []
    daily_returns: list[float] = []

    for i in range(n):
        high = highs[i]
        low = lows[i]
        prev_close = closes[i - 1] if i > 0 else closes[i]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_values.append(tr)
        base = closes[i] if closes[i] > 0 else 1.0
        tr_pct_values.append((tr / base) * 100.0)
        if i > 0 and closes[i - 1] > 0:
            daily_returns.append((closes[i] / closes[i - 1]) - 1.0)

    atr_window = tr_values[-atr_period:] if len(tr_values) >= atr_period else tr_values
    atr = sum(atr_window) / max(len(atr_window), 1)
    last_close = closes[-1]
    atr_pct = (atr / last_close) * 100.0 if last_close > 0 else 0.0

    latest_tr_pct = tr_pct_values[-1]
    tr_percentile_30d = _percentile_rank(tr_pct_values, latest_tr_pct)

    lookback = min(8, len(closes))
    lookback_start = closes[-lookback]
    change_7d_pct = ((last_close / lookback_start) - 1.0) * 100.0 if lookback_start > 0 else 0.0

    if daily_returns:
        avg_ret = sum(daily_returns) / len(daily_returns)
        var = sum((r - avg_ret) ** 2 for r in daily_returns) / len(daily_returns)
        realized_vol_daily_pct = sqrt(var) * 100.0
    else:
        realized_vol_daily_pct = 0.0

    trend_strength = abs(change_7d_pct) / max(atr_pct, 0.01)
    if trend_strength >= 1.8:
        market_regime = "trend_up" if change_7d_pct >= 0 else "trend_down"
        tp_atr_mult = 1.8
        sl_atr_mult = 1.0
    else:
        market_regime = "range"
        tp_atr_mult = 1.2
        sl_atr_mult = 0.8

    tp_pct = tp_atr_mult * atr_pct
    sl_pct = sl_atr_mult * atr_pct

    adaptive_exit_template = {
        "tp_atr_multiple": round(tp_atr_mult, 2),
        "sl_atr_multiple": round(sl_atr_mult, 2),
        "tp_pct_of_btc": round(tp_pct, 2),
        "sl_pct_of_btc": round(sl_pct, 2),
        "upside_alert_price": round(last_close * (1.0 + tp_pct / 100.0), 2),
        "downside_alert_price": round(last_close * (1.0 - sl_pct / 100.0), 2),
    }

    return {
        "market_regime": market_regime,
        "market_regime_reason": (
            f"7d变化{change_7d_pct:.2f}% / ATR%{atr_pct:.2f} => trend_strength={trend_strength:.2f}"
        ),
        "atr": round(atr, 2),
        "atr_pct": round(atr_pct, 2),
        "latest_tr_pct": round(latest_tr_pct, 2),
        "tr_percentile_30d": tr_percentile_30d,
        "realized_vol_daily_pct": round(realized_vol_daily_pct, 2),
        "adaptive_exit_template": adaptive_exit_template,
    }
